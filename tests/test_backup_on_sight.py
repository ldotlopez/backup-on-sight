import http.server
import json
import shutil
import subprocess
import threading
import time
from datetime import UTC, datetime

import pytest

import backup_on_sight as bos


def test_expand_substitutes_nested_strings(monkeypatch):
    monkeypatch.setenv("PASS", "a:b#c")
    assert bos.expand({"x": ["${PASS}", 1], "y": "$$literal"}) == {"x": ["a:b#c", 1], "y": "$literal"}


def test_expand_missing_var_fails(monkeypatch):
    monkeypatch.delenv("NOPE", raising=False)
    with pytest.raises(KeyError):
        bos.expand({"x": "${NOPE}"})


def test_duration():
    assert bos.duration("90s") == 90
    assert bos.duration("24h") == 86400
    assert bos.duration(30) == 30


@pytest.mark.parametrize(("source", "host"), [
    (":ftp,host=192.168.1.104,port=1337,user=anonymous:/ms0:/", "192.168.1.104"),
    (":sftp,user=backup,pass=abc,host=nas.lan:/srv/photos", "nas.lan"),
    (""":sftp,host="[::1]",user=backup:/srv""", "[::1]"),
    ("named,host=nas.lan:/srv", "nas.lan"),
    ("named:/srv", None),
    (":ftp,user=a:/dir,host=fake:/x", None),
    ("/tmp/a,host=fake", None),
])
def test_source_host(source, host):
    assert bos.source_host(source) == host


@pytest.mark.skipif(not (shutil.which("rclone") and shutil.which("restic")), reason="needs rclone and restic")
def test_run_job_mirrors_with_delete_and_snapshots(tmp_path):
    src = tmp_path / "src"
    src.mkdir()
    (src / "keep").write_text("1")
    (src / "gone").write_text("2")
    cfg = {
        "restic": {"repository": str(tmp_path / "repo"), "password": "pw"},
        "state_dir": str(tmp_path / "state"),
        "jobs": {"t": {"probe": {"host": "x", "port": 1}, "source": str(src), "every": "1h", "tags": ["a", "b"]}},
    }

    assert bos.run_job(cfg, "t")
    (src / "gone").unlink()
    assert bos.run_job(cfg, "t")

    mirror = tmp_path / "state" / "t" / "mirror"
    assert sorted(p.name for p in mirror.iterdir()) == ["keep"]
    assert bos.last_ok(cfg, "t") > 0
    snaps = subprocess.run(["restic", "snapshots", "--host", "t", "--json"], env=bos.restic_env(cfg, "t"),
                           capture_output=True, text=True, check=True).stdout
    assert snaps.count('"id"') == 2
    assert sorted(json.loads(snaps)[0]["tags"]) == ["a", "b", "backup-on-sight", "t"]


@pytest.mark.skipif(not (shutil.which("rclone") and shutil.which("restic")), reason="needs rclone and restic")
def test_empty_source_fails_without_snapshot(tmp_path):
    src = tmp_path / "src"
    src.mkdir()
    cfg = {
        "restic": {"repository": str(tmp_path / "repo"), "password": "pw"},
        "state_dir": str(tmp_path / "state"),
        "jobs": {"t": {"probe": {"host": "x", "port": 1}, "source": str(src), "every": "1h"}},
    }

    def snapshot_count() -> int:
        out = subprocess.run(["restic", "snapshots", "--json"], env=bos.restic_env(cfg, "t"),
                             capture_output=True, text=True, check=True).stdout
        return len(json.loads(out))

    assert not bos.run_job(cfg, "t")  # empty on the first run
    assert bos.last_ok(cfg, "t") == 0

    (src / "file").write_text("1")
    assert bos.run_job(cfg, "t")
    good = bos.last_ok(cfg, "t")

    (src / "file").unlink()
    assert not bos.run_job(cfg, "t")  # emptied after a good backup
    assert bos.last_ok(cfg, "t") == good
    assert snapshot_count() == 1


@pytest.mark.skipif(not (shutil.which("rclone") and shutil.which("restic")), reason="needs rclone and restic")
def test_jobs_use_shared_or_own_repo_and_are_tagged_by_name(tmp_path):
    src = tmp_path / "src"
    src.mkdir()
    (src / "file").write_text("1")
    job = {"probe": {"host": "x", "port": 1}, "source": str(src), "every": "1h"}
    cfg = {
        "restic": {"repository": str(tmp_path / "shared"), "password": "pw"},
        "state_dir": str(tmp_path / "state"),
        "jobs": {
            "a": job,
            "b": job,
            "own": job | {"restic": {"repository": str(tmp_path / "own"), "password": "other"}},
        },
    }
    for name in cfg["jobs"]:
        assert bos.run_job(cfg, name)

    def tag_lists(name: str, *args: str) -> list[list[str]]:
        out = subprocess.run(["restic", "snapshots", "--json", *args], env=bos.restic_env(cfg, name),
                             capture_output=True, text=True, check=True).stdout
        return [sorted(snap["tags"]) for snap in json.loads(out)]

    assert tag_lists("a", "--tag", "a") == [["a", "backup-on-sight"]]
    assert tag_lists("a", "--tag", "b") == [["b", "backup-on-sight"]]
    assert tag_lists("a", "--tag", "own") == []
    assert tag_lists("own") == [["backup-on-sight", "own"]]


@pytest.fixture
def webhook():
    """A local HTTP server; yields (url, received) where received collects (path, json body) per POST."""
    received = []

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_POST(self):
            assert self.headers["Content-Type"] == "application/json"
            received.append((self.path, json.loads(self.rfile.read(int(self.headers["Content-Length"])))))
            self.send_response(204)
            self.end_headers()

        def log_message(self, *args):
            pass

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{server.server_port}", received
    server.shutdown()


needs_tools = pytest.mark.skipif(not (shutil.which("rclone") and shutil.which("restic")), reason="needs rclone and restic")


def webhook_cfg(tmp_path, source, **job_fields):
    return {
        "restic": {"repository": str(tmp_path / "repo"), "password": "s3cret-repo"},
        "state_dir": str(tmp_path / "state"),
        "jobs": {"t": {"probe": {"host": "x", "port": 1}, "source": source, "every": "1h", "tags": ["a"], **job_fields}},
    }


@needs_tools
def test_webhook_reports_start_and_successful_end(tmp_path, webhook):
    url, received = webhook
    src = tmp_path / "src"
    src.mkdir()
    (src / "file").write_text("1")
    cfg = webhook_cfg(tmp_path, str(src)) | {"webhook": url}

    assert bos.run_job(cfg, "t")

    (_, start), (_, end) = received
    assert (start["job"], start["phase"], start["host"], start["tags"]) == ("t", "start", "t", ["backup-on-sight", "t", "a"])
    assert "success" not in start
    assert (end["phase"], end["success"], end["error"]) == ("end", True, None)
    assert end["duration_seconds"] >= 0
    assert start["timestamp"].endswith("+00:00")
    assert "s3cret-repo" not in json.dumps(received) and str(src) not in json.dumps(received)


@needs_tools
def test_webhook_reports_failed_end(tmp_path, webhook):
    url, received = webhook
    cfg = webhook_cfg(tmp_path, str(tmp_path / "missing"), webhook=url)

    assert not bos.run_job(cfg, "t")

    assert [body["phase"] for _, body in received] == ["start", "end"]
    assert (received[1][1]["success"], received[1][1]["error"]) == (False, "sync failed, no snapshot taken")


@needs_tools
def test_source_credentials_are_not_logged(tmp_path, caplog, capfd):
    caplog.set_level("DEBUG", logger="backup_on_sight")  # debug also adds rclone -v and the most log lines
    obscured = subprocess.run(["rclone", "obscure", "pw"], capture_output=True, text=True, check=True).stdout.strip()
    source = f":ftp,host=127.0.0.1,port=1,user=a,pass={obscured}:/x"

    assert not bos.run_job(webhook_cfg(tmp_path, source), "t")

    captured = capfd.readouterr()
    assert "t: syncing from 127.0.0.1:1\n" in caplog.text
    assert "t: rclone exited with 1 after" in caplog.text
    assert "Failed to create file system" in captured.err  # rclone's error still reaches the log
    assert obscured not in caplog.text + captured.err + captured.out


@pytest.mark.parametrize(("source", "endpoint"), [
    (":ftp,host=192.168.1.104,port=1337,user=a:/ms0:/", "192.168.1.104:1337"),
    (":sftp,host=nas.lan,user=a:/srv", "nas.lan"),
    ("/local/path", "t"),
])
def test_job_endpoint(tmp_path, source, endpoint):
    assert bos.job_endpoint(webhook_cfg(tmp_path, source), "t") == endpoint


class StopWatching(Exception):
    pass


def watch_for(cfg, monkeypatch, polls, on_poll=lambda poll: None):
    """Run the watcher for a number of polls, without waiting between them."""
    count = iter(range(polls))

    def fake_sleep(_seconds):
        poll = next(count, None)
        if poll is None:
            raise StopWatching
        on_poll(poll)

    monkeypatch.setattr(bos.time, "sleep", fake_sleep)
    with pytest.raises(StopWatching):
        bos.watch(cfg, "t")


def watcher_cfg(tmp_path):
    cfg = webhook_cfg(tmp_path, "s")
    cfg["jobs"]["t"]["probe"] = {"host": "127.0.0.1", "port": 1}
    return cfg


def test_watch_logs_next_due_time_once_until_it_changes(tmp_path, monkeypatch, caplog):
    caplog.set_level("DEBUG", logger="backup_on_sight")
    cfg = watcher_cfg(tmp_path)
    last_ok = bos.job_dir(cfg, "t") / "last_ok"
    now = time.time()
    last_ok.write_text(str(now))

    def on_poll(poll):
        if poll == 2:  # a manual run elsewhere moves the due time
            last_ok.write_text(str(now + 60))

    watch_for(cfg, monkeypatch, 5, on_poll)

    lines = [message for message in caplog.messages if "next backup due at" in message]
    expected = [datetime.fromtimestamp(now + 3600 + extra, UTC).astimezone().strftime("%Y-%m-%d %H:%M:%S") for extra in (0, 60)]
    assert len(lines) == 2
    assert expected[0] in lines[0] and expected[1] in lines[1]


def test_watch_logs_host_down_once(tmp_path, monkeypatch, caplog):
    caplog.set_level("DEBUG", logger="backup_on_sight")
    watch_for(watcher_cfg(tmp_path), monkeypatch, 3)
    assert sum("is down" in message for message in caplog.messages) == 1


@needs_tools
def test_run_skips_recent_backup_unless_forced(tmp_path, monkeypatch, caplog):
    caplog.set_level("INFO", logger="backup_on_sight")
    src = tmp_path / "src"
    src.mkdir()
    (src / "file").write_text("1")
    path = tmp_path / "config.yaml"
    path.write_text(f"""
restic: {{repository: "{tmp_path / 'repo'}", password: pw}}
state_dir: "{tmp_path / 'state'}"
jobs:
  t: {{probe: {{host: 127.0.0.1, port: 1}}, source: "{src}", every: 1h}}
""")

    def run_cli(*args: str) -> None:
        monkeypatch.setattr("sys.argv", ["backup-on-sight", "-c", str(path), "run", *args, "t"])
        with pytest.raises(SystemExit) as exit_info:
            bos.main()
        assert exit_info.value.code == 0

    def snapshot_count() -> int:
        cfg = bos.load_config(path)
        out = subprocess.run(["restic", "snapshots", "--json"], env=bos.restic_env(cfg, "t"),
                             capture_output=True, text=True, check=True).stdout
        return len(json.loads(out))

    run_cli()  # never backed up, so it's due even though the probe is down
    assert snapshot_count() == 1

    run_cli()
    assert snapshot_count() == 1
    assert "too recent" in caplog.text

    run_cli("--force")
    assert snapshot_count() == 2


def interval_config(job_interval: str = "", top: str = "") -> str:
    return (f"{top}\nrestic: {{repository: r, password: p}}\n"
            f"jobs: {{t: {{probe: {{host: x, port: 1{job_interval}}}, source: s, every: 1h}}}}")


@pytest.mark.parametrize(("job_interval", "top", "seconds"), [
    ("", "", 30),
    ("", "poll_interval: 1m", 60),
    (", interval: 10s", "poll_interval: 1m", 10),
    (", interval: 5", "", 5),
])
def test_probe_interval_prefers_job_over_global(tmp_path, job_interval, top, seconds):
    path = tmp_path / "config.yaml"
    path.write_text(interval_config(job_interval, top))
    assert bos.job_interval(bos.load_config(path), "t") == seconds


@pytest.mark.parametrize("value", ["0", "-5", "0s", "abc", "10x", "''", "[]"])
def test_load_config_rejects_bad_probe_interval(tmp_path, value):
    for config in (interval_config(f", interval: {value}"), interval_config(top=f"poll_interval: {value}")):
        path = tmp_path / "config.yaml"
        path.write_text(config)
        with pytest.raises(ValueError, match="interval"):
            bos.load_config(path)


def test_job_webhook_replaces_global(tmp_path, webhook):
    url, received = webhook
    cfg = webhook_cfg(tmp_path, "s", webhook=f"{url}/job") | {"webhook": f"{url}/global"}
    bos.notify(cfg, "t", "start")
    assert [path for path, _ in received] == ["/job"]


def test_job_without_webhook_uses_global(tmp_path, webhook):
    url, received = webhook
    bos.notify(webhook_cfg(tmp_path, "s") | {"webhook": f"{url}/global"}, "t", "start")
    assert [path for path, _ in received] == ["/global"]


def test_null_job_webhook_opts_out_of_global(tmp_path, webhook):
    url, received = webhook
    path = tmp_path / "config.yaml"
    path.write_text(f"webhook: {url}\n{job_config(', webhook: null')}")
    bos.notify(bos.load_config(path), "t", "start")
    assert received == []


def test_failing_webhook_is_only_logged_and_hides_url(tmp_path, caplog):
    cfg = webhook_cfg(tmp_path, "s", webhook="http://127.0.0.1:1/token-abc")
    bos.notify(cfg, "t", "start")
    assert "webhook failed" in caplog.text and "token-abc" not in caplog.text


def test_end_webhook_still_fires_when_backup_raises(tmp_path, webhook, monkeypatch):
    url, received = webhook
    monkeypatch.setattr(bos, "back_up", lambda *args: 1 / 0)
    with pytest.raises(ZeroDivisionError):
        bos.run_job(webhook_cfg(tmp_path, "s", webhook=url), "t")
    assert (received[1][1]["phase"], received[1][1]["success"], received[1][1]["error"]) == ("end", False, "unexpected error")


@pytest.mark.parametrize("value", ["file:///etc/passwd", "ftp://x/y", "''", "not a url", "'http://a b'", "[]"])
def test_load_config_rejects_bad_webhook(tmp_path, value):
    for config in (job_config(f", webhook: {value}"), f"webhook: {value}\n{job_config()}"):
        path = tmp_path / "config.yaml"
        path.write_text(config)
        with pytest.raises(ValueError, match="webhook"):
            bos.load_config(path)


def test_load_config_accepts_webhooks(tmp_path):
    path = tmp_path / "config.yaml"
    path.write_text(f"webhook: https://a.lan/x\n{job_config(', webhook: "http://b.lan:8080/y?token=z"')}")
    assert bos.load_config(path)["webhook"] == "https://a.lan/x"


def job_config(job_fields: str = "", top: str = "restic: {repository: r, password: p}") -> str:
    return f"{top}\njobs: {{t: {{probe: {{host: x, port: 1}}, source: s, every: 1h{job_fields}}}, other: {{probe: {{host: x, port: 1}}, source: s, every: 1h}}}}"


@pytest.mark.parametrize("tags", ["oops", "['']", "[2024]", "['a,b']", "[other]"])
def test_load_config_rejects_bad_tags(tmp_path, tags):
    path = tmp_path / "config.yaml"
    path.write_text(job_config(f", tags: {tags}"))
    with pytest.raises(ValueError, match="tags"):
        bos.load_config(path)


@pytest.mark.parametrize("name", ["a,b", "backup-on-sight"])
def test_load_config_rejects_names_unusable_as_tags(tmp_path, name):
    path = tmp_path / "config.yaml"
    path.write_text(f"restic: {{repository: r, password: p}}\njobs: {{'{name}': {{probe: {{host: x, port: 1}}, source: s, every: 1h}}}}")
    with pytest.raises(ValueError, match="name"):
        bos.load_config(path)


@pytest.mark.parametrize("fields", ["", ", tags: [a]", ", tags: [t]", ", tags: []"])
def test_load_config_accepts_optional_tags(tmp_path, fields):
    path = tmp_path / "config.yaml"
    path.write_text(job_config(fields))
    assert bos.load_config(path)["jobs"]["t"]


def test_load_config_requires_a_repo(tmp_path):
    path = tmp_path / "config.yaml"
    path.write_text(job_config(top=""))
    with pytest.raises(ValueError, match="repository"):
        bos.load_config(path)


def test_load_config_accepts_own_repo_without_shared(tmp_path):
    path = tmp_path / "config.yaml"
    own = "restic: {repository: r, password: p}"
    path.write_text(f"jobs: {{t: {{probe: {{host: x, port: 1}}, source: s, every: 1h, {own}}}}}")
    assert bos.job_restic(bos.load_config(path), "t") == {"repository": "r", "password": "p"}


def test_failed_sync_takes_no_snapshot(tmp_path):
    cfg = {
        "restic": {"repository": str(tmp_path / "repo"), "password": "pw"},
        "state_dir": str(tmp_path / "state"),
        "jobs": {"t": {"probe": {"host": "x", "port": 1}, "source": str(tmp_path / "missing"), "every": "1h", "tags": ["a"]}},
    }
    if not (shutil.which("rclone") and shutil.which("restic")):
        pytest.skip("needs rclone and restic")
    assert not bos.run_job(cfg, "t")
    assert bos.last_ok(cfg, "t") == 0
