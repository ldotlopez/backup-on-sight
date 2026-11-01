"""Declarative sources: FTP goes through lftp, every other backend through an rclone connection string."""

import shutil
import socket
import subprocess
import threading

import pytest

import backup_on_sight as bos

needs_rclone = pytest.mark.skipif(not shutil.which("rclone"), reason="needs rclone")
needs_lftp = pytest.mark.skipif(not shutil.which("lftp"), reason="needs lftp")


def load_job(tmp_path, job: str) -> dict:
    path = tmp_path / "config.yaml"
    path.write_text(f"restic: {{repository: r, password: p}}\njobs: {{t: {job}}}")
    return bos.load_config(path)["jobs"]["t"]


def reveal(obscured: str) -> str:
    return subprocess.run(["rclone", "reveal", "--", obscured], capture_output=True, text=True, check=True).stdout.strip()


class FakeFtp:
    """Just enough FTP for lftp: one flat folder, a bare LIST refused like on the PSP, and RETR failing for missing files."""

    def __init__(self, listing: dict[str, bytes | None]) -> None:
        self.listing = listing
        self.commands: list[str] = []
        self.listener = socket.create_server(("127.0.0.1", 0))
        self.port = self.listener.getsockname()[1]
        threading.Thread(target=self.serve, daemon=True).start()

    def serve(self) -> None:
        while True:
            conn, _ = self.listener.accept()
            threading.Thread(target=self.session, args=(conn,), daemon=True).start()

    def session(self, conn: socket.socket) -> None:
        data_listener = None
        with conn, conn.makefile("rwb") as stream:

            def reply(*lines: str) -> None:
                stream.write("".join(f"{line}\r\n" for line in lines).encode())
                stream.flush()

            def send_data(payload: bytes) -> None:
                reply("150 here")
                data, _ = data_listener.accept()
                with data:
                    data.sendall(payload)
                reply("226 done")

            reply("220 ready")
            for raw in stream:
                command = raw.decode().rstrip("\r\n")
                self.commands.append(command)
                verb, _, argument = command.partition(" ")
                if verb == "USER":
                    reply("331 go on")
                elif verb == "PASS":
                    reply("230 in")
                elif verb == "FEAT":
                    reply("211-", " UTF8", "211 End")
                elif verb == "PWD":
                    reply('257 "/"')
                elif verb in ("CWD", "TYPE", "OPTS"):
                    reply("250 ok")
                elif verb == "PASV":
                    data_listener = socket.create_server(("127.0.0.1", 0))
                    port = data_listener.getsockname()[1]
                    reply(f"227 127,0,0,1,{port >> 8},{port & 255}")  # no parentheses, like the Switch
                elif verb == "LIST" and argument:
                    # a missing body still gets a size: lftp creates empty files without asking for them
                    rows = (f"-rw-r--r-- 1 o g {len(body or b'x')} Jan  1  2020 {name}\r\n" for name, body in self.listing.items())
                    send_data("".join(rows).encode())
                elif verb == "LIST":
                    reply("550 Invalid directory.")
                elif verb == "RETR" and self.listing.get(argument.rsplit("/", 1)[-1]) is not None:
                    send_data(self.listing[argument.rsplit("/", 1)[-1]])
                elif verb == "QUIT":
                    reply("221 bye")
                    return
                else:
                    reply("550 no")


def run_lftp(source: dict, mirror, **env: str) -> subprocess.CompletedProcess:
    command, script = bos.sync_plan(source, mirror, False)
    return subprocess.run(command, input=script, capture_output=True, text=True, timeout=30, env=env or None)


def test_ftp_stays_as_settings_with_a_derived_probe(tmp_path):
    job = load_job(tmp_path, "{source: {type: ftp, host: switch.lan, port: 5000}, every: 1h}")
    cfg = {"jobs": {"t": job}}

    assert job["probe"] == {"host": "switch.lan", "port": 5000}
    assert bos.job_host(cfg, "t") == "switch.lan"
    assert bos.job_endpoint(cfg, "t") == "switch.lan:5000"


def test_ftp_default_port_and_probe_overrides(tmp_path):
    assert load_job(tmp_path, "{source: {type: ftp, host: h}, every: 1h}")["probe"] == {"host": "h", "port": 21}
    job = load_job(tmp_path, "{source: {type: ftp, host: h}, probe: {port: 80, interval: 10s}, every: 1h}")
    assert job["probe"] == {"host": "h", "port": 80, "interval": "10s"}


def test_lftp_script_defaults_to_root_and_options_override_settings(tmp_path):
    script = bos.lftp_script({"host": "h", "options": {"net:connection-limit": 2}}, tmp_path)

    assert f'mirror --delete --no-perms "/" "{tmp_path}"' in script
    assert 'set net:connection-limit "2"' in script
    assert 'set net:connection-limit "1"' not in script
    assert 'set ftp:ssl-allow "false"' in script


def test_password_goes_through_stdin_not_the_command_line(tmp_path):
    command, script = bos.sync_plan({"host": "h", "password": "s3cret"}, tmp_path, False)
    assert "s3cret" not in " ".join(command)
    assert "s3cret" in script


@needs_lftp
def test_mirror_copies_lists_with_an_argument_and_deletes_extras(tmp_path):
    server = FakeFtp({"save.bin": b"hello"})
    mirror = tmp_path / "mirror"
    mirror.mkdir()
    (mirror / "gone.bin").write_text("old")

    result = run_lftp({"host": "127.0.0.1", "port": server.port}, mirror)

    assert result.returncode == 0, result.stderr
    assert sorted(path.name for path in mirror.iterdir()) == ["save.bin"]
    assert (mirror / "save.bin").read_bytes() == b"hello"
    assert "LIST ." in server.commands


@needs_lftp
def test_one_unreadable_file_fails_the_sync(tmp_path):
    server = FakeFtp({"save.bin": b"hello", "broken.bin": None})
    assert run_lftp({"host": "127.0.0.1", "port": server.port}, tmp_path / "mirror").returncode != 0


@needs_lftp
def test_refused_folder_fails_the_sync(tmp_path):
    server = FakeFtp({})
    source = {"host": "127.0.0.1", "port": server.port, "options": {"ftp:list-options": ""}}
    assert run_lftp(source, tmp_path / "mirror").returncode != 0


@needs_lftp
def test_non_ascii_names_survive_a_c_locale(tmp_path):
    server = FakeFtp({"mañana.bin": b"hello"})
    mirror = tmp_path / "mirror"

    result = run_lftp({"host": "127.0.0.1", "port": server.port}, mirror, LC_ALL="C", PATH=bos.os.environ["PATH"])

    assert result.returncode == 0, result.stderr
    assert (mirror / "mañana.bin").read_bytes() == b"hello"


@needs_lftp
def test_awkward_login_reaches_the_server_exactly(tmp_path):
    server = FakeFtp({"save.bin": b"hello"})
    user, password = 'me,"x', 'pa"ss w\\or,d:$x#;'

    run_lftp({"host": "127.0.0.1", "port": server.port, "user": user, "password": password}, tmp_path / "mirror")

    assert f"USER {user}" in server.commands
    assert f"PASS {password}" in server.commands


def test_other_backends_and_options_pass_through(tmp_path):
    job = load_job(tmp_path, "{source: {type: sftp, host: nas.lan, user: me, path: /srv, options: {key_file: /k, use_insecure_cipher: true}}, every: 1h}")

    assert job["source"] == ':sftp,host="nas.lan",user="me",key_file="/k",use_insecure_cipher="true":/srv'
    assert job["probe"] == {"host": "nas.lan", "port": 22}


def test_other_backends_default_to_root(tmp_path):
    assert load_job(tmp_path, "{source: {type: sftp, host: h}, every: 1h}")["source"].endswith(":/")


@needs_rclone
def test_rclone_password_is_obscured(tmp_path):
    source = load_job(tmp_path, "{source: {type: sftp, host: h, password: 'pa ss'}, every: 1h}")["source"]
    assert reveal(bos.source_param(source, "pass")) == "pa ss"


def test_rclone_string_source_is_untouched_and_still_needs_a_probe(tmp_path):
    job = load_job(tmp_path, '{source: ":ftp,host=h:/", probe: {host: h, port: 21}, every: 1h}')
    assert job["source"] == ":ftp,host=h:/"
    with pytest.raises(ValueError, match="missing probe"):
        load_job(tmp_path, '{source: ":ftp,host=h:/", every: 1h}')


def device_tree(root) -> None:
    for path in ["PSP/SYSTEM/a.bin", "PSP/keep.bin", "ISO/sub/y.txt", "ISOfile", "other/ISO/z.txt", "other/PSP/SYSTEM/b.bin", "Game [USA] {1}.iso.bak"]:
        (root / path).parent.mkdir(parents=True, exist_ok=True)
        (root / path).write_text("x")


def sync_tree(tool: str, root, mirror, exclude: list[str]) -> list[str]:
    """Run the real tool over a local tree, standing in for a device, and list what reached the mirror."""
    source = {"type": "ftp", "host": "file:///", "path": str(root), "exclude": exclude}
    resolved = bos.source_excludes("t", source)
    command, script = bos.sync_plan(source if tool == "lftp" else str(root), mirror, False, resolved)
    result = subprocess.run(command, input=script, capture_output=True, text=True, timeout=30, check=False)
    assert result.returncode == 0, result.stderr
    return sorted(str(path.relative_to(mirror)) for path in mirror.rglob("*") if path.is_file())


both_tools = pytest.mark.parametrize("tool", [pytest.param("lftp", marks=needs_lftp), pytest.param("rclone", marks=needs_rclone)])


@both_tools
def test_absolute_exclude_is_one_exact_file_or_folder(tmp_path, tool):
    root = tmp_path / "device"
    device_tree(root)

    copied = sync_tree(tool, root, tmp_path / "mirror", [f"{root}/ISO", f"{root}/PSP/SYSTEM/", f"{root}/Game [USA] {{1}}.iso.bak"])

    # the same names deeper down and the longer ISOfile must survive
    assert copied == ["ISOfile", "PSP/keep.bin", "other/ISO/z.txt", "other/PSP/SYSTEM/b.bin"]


@both_tools
def test_glob_exclude_goes_to_the_tool(tmp_path, tool):
    root = tmp_path / "device"
    device_tree(root)

    copied = sync_tree(tool, root, tmp_path / "mirror", ["*.bin"])

    assert "PSP/keep.bin" not in copied
    assert "other/PSP/SYSTEM/b.bin" not in copied
    assert "ISOfile" in copied


@both_tools
def test_excluding_removes_what_the_mirror_already_holds(tmp_path, tool):
    root, mirror = tmp_path / "device", tmp_path / "mirror"
    device_tree(root)

    assert "ISO/sub/y.txt" in sync_tree(tool, root, mirror, [])
    assert "ISO/sub/y.txt" not in sync_tree(tool, root, mirror, [f"{root}/ISO"])


@needs_lftp
def test_excluded_unreadable_file_no_longer_fails_the_sync(tmp_path):
    server = FakeFtp({"save.bin": b"hello", "broken.bin": None})
    source = {"host": "127.0.0.1", "port": server.port}

    command, script = bos.sync_plan(source, tmp_path / "mirror", False, ["/broken.bin"])
    result = subprocess.run(command, input=script, capture_output=True, text=True, timeout=30, check=False)

    assert result.returncode == 0, result.stderr
    assert not any(sent.startswith("RETR") and "broken" in sent for sent in server.commands)


@needs_lftp
@pytest.mark.skipif(not shutil.which("restic"), reason="needs restic")
def test_run_job_applies_excludes_from_the_config(tmp_path):
    server = FakeFtp({"save.bin": b"hello", "broken.bin": None})
    path = tmp_path / "config.yaml"
    path.write_text(f"""
state_dir: {tmp_path}/state
restic: {{repository: {tmp_path}/repo, password: p}}
jobs: {{t: {{source: {{type: ftp, host: 127.0.0.1, port: {server.port}, exclude: [/broken.bin]}}, every: 1h}}}}
""")

    assert bos.run_job(bos.load_config(path), "t")
    assert [item.name for item in (tmp_path / "state/t/mirror").iterdir()] == ["save.bin"]


def test_absolute_excludes_resolve_against_the_source_path(tmp_path):
    job = load_job(tmp_path, "{source: {type: ftp, host: h, path: '/ms0:/', exclude: ['/ms0:/ISO/', '*.iso']}, every: 1h}")
    assert job["exclude"] == ["/ISO", "*.iso"]
    job = load_job(tmp_path, "{source: {type: sftp, host: h, path: /srv, exclude: [/srv/cache]}, every: 1h}")
    assert job["exclude"] == ["/cache"]
    assert "exclude" not in job["source"]


def test_no_excludes_leaves_the_commands_alone(tmp_path):
    assert "--delete-excluded" not in bos.sync_plan({"host": "h"}, tmp_path, False, [])[1]
    assert "--delete-excluded" not in bos.sync_plan(":ftp,host=h:/", tmp_path, False, [])[0]


@pytest.mark.parametrize(("source", "message"), [
    ("{type: ftp, host: h, exclude: nope}", "must be a list"),
    ("{type: ftp, host: h, exclude: ['']}", "must be a list"),
    ('{type: ftp, host: h, exclude: ["/a\\nb"]}', "must be a list"),
    ("{type: ftp, host: h, path: '/ms0:/', exclude: [/ISO]}", "must be inside the source path"),
    ("{type: ftp, host: h, path: '/ms0:/', exclude: ['/ms0:/']}", "must be inside the source path"),
    ("{type: sftp, host: h, path: /srv, exclude: [/srv2/x]}", "must be inside the source path"),
    ("{type: ftp, host: h, exclude: ['/a/*.iso']}", "absolute paths are literal"),
    ("{host: h}", "source needs a type"),
    ("{type: 'a,b', host: h}", "source needs a type"),
    ("{type: sftp, host: h, passwd: x}", "unknown source settings: passwd"),
    ("{type: sftp, host: h, options: nope}", "source options must map"),
    ("{type: sftp, host: h, options: {'a,b': 1}}", "source options must map"),
    ("{type: webdav}", "can't provide a probe"),
    ("{type: ftp}", "ftp source needs a host"),
    ("{type: ftp, host: h, options: nope}", "source options must map"),
    ("{type: ftp, host: h, options: {'a;b': 1}}", "source options must map"),
    ('{type: ftp, host: h, path: "/a\\nb"}', "line breaks"),
    ('{type: ftp, host: h, options: {"ftp:x": "a\\nb"}}', "line breaks"),
])
def test_rejects_bad_sources(tmp_path, source, message):
    with pytest.raises(ValueError, match=message):
        load_job(tmp_path, f"{{source: {source}, every: 1h}}")


def test_exclude_outside_the_source_block_is_rejected(tmp_path):
    with pytest.raises(ValueError, match="exclude is a source setting"):
        load_job(tmp_path, "{source: {type: ftp, host: h}, exclude: ['*.iso'], every: 1h}")
