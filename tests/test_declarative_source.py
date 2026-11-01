"""Declarative sources, which load_config turns into rclone connection strings."""

import shutil
import socket
import subprocess
import threading

import pytest

import backup_on_sight as bos

needs_rclone = pytest.mark.skipif(not shutil.which("rclone"), reason="needs rclone")


def load_job(tmp_path, job: str) -> dict:
    path = tmp_path / "config.yaml"
    path.write_text(f"restic: {{repository: r, password: p}}\njobs: {{t: {job}}}")
    return bos.load_config(path)["jobs"]["t"]


def reveal(obscured: str) -> str:
    return subprocess.run(["rclone", "reveal", obscured], capture_output=True, text=True, check=True).stdout.strip()


@needs_rclone
def test_ftp_defaults_to_anonymous_login_folder_and_derives_the_probe(tmp_path):
    job = load_job(tmp_path, "{source: {type: ftp, host: switch.lan, port: 5000}, every: 1h}")

    assert job["probe"] == {"host": "switch.lan", "port": 5000}
    assert bos.source_host(job["source"]) == "switch.lan"
    assert bos.source_param(job["source"], "port") == "5000"
    assert bos.source_param(job["source"], "user") == "anonymous"
    assert reveal(bos.source_param(job["source"], "pass")) == "anonymous"
    assert job["source"].endswith(":")  # the login folder, not /: the Switch answers only for that one


@needs_rclone
def test_ftp_stays_gentle_by_default_and_options_can_override_it(tmp_path):
    gentle = load_job(tmp_path, "{source: {type: ftp, host: h}, every: 1h}")["source"]
    assert ',concurrency="1",disable_epsv="true",disable_mlsd="true"' in gentle

    tuned = load_job(tmp_path, "{source: {type: ftp, host: h, options: {concurrency: 2}}, every: 1h}")["source"]
    assert bos.source_param(tuned, "concurrency") == "2"
    assert tuned.count("concurrency=") == 1


@needs_rclone
def test_path_with_a_share_like_the_psp_and_default_port(tmp_path):
    job = load_job(tmp_path, "{source: {type: ftp, host: psp.lan, path: /ms0:/PSP/SAVEDATA}, every: 1h}")

    assert job["probe"] == {"host": "psp.lan", "port": 21}
    assert job["source"].endswith(":/ms0:/PSP/SAVEDATA")
    assert bos.job_endpoint({"jobs": {"t": job}}, "t") == "psp.lan"


def test_other_backends_and_options_pass_through(tmp_path):
    job = load_job(tmp_path, '{source: {type: sftp, host: nas.lan, user: me, path: /srv, options: {key_file: /k, use_insecure_cipher: true}}, every: 1h}')

    assert job["source"] == ':sftp,host="nas.lan",user="me",key_file="/k",use_insecure_cipher="true":/srv'
    assert job["probe"] == {"host": "nas.lan", "port": 22}


@needs_rclone
def test_probe_settings_override_the_source(tmp_path):
    job = load_job(tmp_path, "{source: {type: ftp, host: h}, probe: {port: 80, interval: 10s}, every: 1h}")
    assert job["probe"] == {"host": "h", "port": 80, "interval": "10s"}


def test_rclone_string_source_is_untouched_and_still_needs_a_probe(tmp_path):
    job = load_job(tmp_path, '{source: ":ftp,host=h:/", probe: {host: h, port: 21}, every: 1h}')
    assert job["source"] == ":ftp,host=h:/"
    with pytest.raises(ValueError, match="missing probe"):
        load_job(tmp_path, '{source: ":ftp,host=h:/", every: 1h}')


@pytest.mark.parametrize(("source", "message"), [
    ("{host: h}", "source needs a type"),
    ("{type: 'a,b', host: h}", "source needs a type"),
    ("{type: sftp, host: h, passwd: x}", "unknown source settings: passwd"),
    ("{type: sftp, host: h, options: nope}", "source options must map"),
    ("{type: sftp, host: h, options: {'a,b': 1}}", "source options must map"),
    ("{type: webdav}", "can't provide a probe"),
])
def test_rejects_bad_sources(tmp_path, source, message):
    with pytest.raises(ValueError, match=message):
        load_job(tmp_path, f"{{source: {source}, every: 1h}}")


@needs_rclone
def test_awkward_login_reaches_the_server_exactly(tmp_path):
    """Real rclone against a server that records the login and rejects it."""
    logins: list[str] = []

    def serve(listener: socket.socket) -> None:
        conn, _ = listener.accept()
        with conn, conn.makefile("rwb") as stream:
            stream.write(b"220 ready\r\n")
            stream.flush()
            for raw in stream:
                command, _, argument = raw.decode().rstrip("\r\n").partition(" ")
                logins.append(argument) if command in ("USER", "PASS") else None
                stream.write(b"331 go on\r\n" if command == "USER" else b"530 no\r\n")
                stream.flush()
                if command == "PASS":
                    return

    with socket.create_server(("127.0.0.1", 0)) as listener:
        threading.Thread(target=serve, args=(listener,), daemon=True).start()
        source = bos.rclone_source("t", {
            "type": "ftp", "host": "127.0.0.1", "port": listener.getsockname()[1],
            "user": 'me,"x', "password": 'pa"ss w\\or,d:$x',
        })
        result = subprocess.run(["rclone", "lsf", "--retries", "1", "--low-level-retries", "1", source], capture_output=True, text=True)

    assert result.returncode != 0
    assert logins == ['me,"x', 'pa"ss w\\or,d:$x']
