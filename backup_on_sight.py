"""Event-driven backups: when a host shows up, copy it to a mirror (lftp for FTP, rclone otherwise) and snapshot the mirror with restic."""

import argparse
import fcntl
import http.client
import json
import logging
import os
import re
import socket
import string
import subprocess
import sys
import threading
import time
import urllib.request
from datetime import UTC, datetime
from pathlib import Path

import yaml
from dotenv import load_dotenv

log = logging.getLogger(__name__)

APP_NAME = "backup-on-sight"
UNITS = {"s": 1, "m": 60, "h": 3600, "d": 86400}
RETRY_AFTER_FAILURE = 300  # shortcut: fixed backoff after a failed run, make it per-job if it ever matters
INIT_LOCK = threading.Lock()
WEBHOOK_TIMEOUT = 10
DEFAULT_PORTS = {"ftp": 21, "sftp": 22}  # lets these types skip the probe; any other type needs a port or a probe
FTP_DEFAULT_OPTIONS = {
    "cmd:fail-exit": True,  # stop at the first failure, like a mistyped setting, instead of mirroring anyway
    "file:charset": "UTF-8",  # the container's C locale would mangle non-ASCII filenames
    "ftp:list-options": ".",  # the PSP answers a bare LIST with 550 but accepts LIST .
    "ftp:ssl-allow": False,  # console servers don't speak TLS
    "ftp:use-feat": False,  # gentlest handshake: the PSP refuses what FEAT leads to, and the Switch works without it
    "net:connection-limit": 1,  # console FTP servers are fragile
    "net:max-retries": 1,  # a failed run is retried by the watcher
    "net:timeout": 60,
}


def expand(value):
    """Substitute ${VAR} in every string of the parsed config; missing vars raise KeyError."""
    if isinstance(value, dict):
        return {k: expand(v) for k, v in value.items()}
    if isinstance(value, list):
        return [expand(v) for v in value]
    if isinstance(value, str):
        return string.Template(value).substitute(os.environ)
    return value


def duration(value) -> float:
    if isinstance(value, (int, float)):
        return value
    return float(value[:-1]) * UNITS[value[-1]]


def job_restic(cfg: dict, name: str) -> dict | None:
    """The job's own repository if it has one, otherwise the shared top-level one."""
    return cfg["jobs"][name].get("restic", cfg.get("restic"))


def job_interval(cfg: dict, name: str) -> float:
    """Seconds between checks: the job's probe.interval, otherwise the global poll_interval."""
    return duration(cfg["jobs"][name]["probe"].get("interval", cfg.get("poll_interval", 30)))


def valid_webhook(url: object) -> bool:
    # urlopen also opens file:// and ftp://; printable ASCII keeps the URL out of http.client's error messages
    return isinstance(url, str) and re.fullmatch(r"https?://[\x21-\x7e]+", url) is not None


def rclone_obscure(password: str) -> str:
    # through stdin, so the plain password stays out of the process list
    return subprocess.run(["rclone", "obscure", "-"], input=password, capture_output=True, text=True, check=True).stdout.strip()


def rclone_quote(value: object) -> str:
    text = str(value).lower() if isinstance(value, bool) else str(value)  # rclone wants true, YAML's str gives True
    return '"' + text.replace('"', '""') + '"'


def check_source(name: str, source: dict) -> None:
    backend = source.get("type")
    if not (isinstance(backend, str) and re.fullmatch(r"[a-z0-9]+", backend)):
        raise ValueError(f"job {name}: source needs a type, like ftp or sftp")
    # a typo like "passwd" would otherwise silently log in with the defaults
    if unknown := source.keys() - {"type", "host", "port", "path", "user", "password", "options", "exclude"}:
        raise ValueError(f"job {name}: unknown source settings: {', '.join(sorted(unknown))}")


def source_excludes(name: str, source: dict) -> list[str]:
    """The source's excludes as the sync tools need them: an absolute path becomes "/" plus its path inside the source; globs stay as written."""
    excludes = source.get("exclude") or []
    if not (isinstance(excludes, list) and all(isinstance(item, str) and item and not re.search(r"[\r\n]", item) for item in excludes)):
        raise ValueError(f"job {name}: source exclude must be a list of globs or absolute paths, without line breaks")
    root = (source.get("path") or "/").rstrip("/") + "/"
    resolved = []
    for item in excludes:
        if not item.startswith("/"):
            resolved.append(item)
            continue
        # literal, so "[USA]" in a filename works; a wildcard here would silently match nothing
        if "*" in item or "?" in item:
            raise ValueError(f"job {name}: exclude {item} mixes an absolute path with a glob, absolute paths are literal")
        # the tools can't anchor outside the source, so this could never match
        inside = item.removeprefix(root).rstrip("/")
        if not item.startswith(root) or not inside:
            raise ValueError(f"job {name}: exclude {item} must be inside the source path {root}")
        resolved.append(f"/{inside}")
    return resolved


def rclone_excludes(excludes: list[str]) -> list[str]:
    """rclone flags for resolved excludes. Without /** a folder's contents would still be copied."""
    flags = ["--delete-excluded"] if excludes else []  # otherwise files already in the mirror stay in every snapshot
    for item in excludes:
        if item.startswith("/"):
            literal = re.sub(r"([\\*?\[\]{}])", r"\\\1", item)
            flags += [f"--exclude={literal}", f"--exclude={literal}/**"]
        else:
            flags.append(f"--exclude={item}")
    return flags


def lftp_excludes(excludes: list[str]) -> str:
    """lftp mirror flags for resolved excludes. Its globs can't anchor (a leading / matches nothing), so absolute paths use a regex."""
    flags = ["--delete-excluded"] if excludes else []  # otherwise files already in the mirror stay in every snapshot
    for item in excludes:
        if item.startswith("/"):
            flags.append(f"--exclude {lftp_quote(f'^{re.escape(item[1:])}(/|$)')}")
        else:
            flags.append(f"--exclude-glob {lftp_quote(item)}")
    return "".join(f"{flag} " for flag in flags)


def rclone_source(name: str, source: dict) -> str:
    """Turn a declarative source into an rclone connection string."""
    backend = source["type"]
    options = source.get("options") or {}
    if not (isinstance(options, dict) and all(re.fullmatch(r"[a-z0-9_]+", str(key)) for key in options)):
        raise ValueError(f"job {name}: source options must map rclone option names to values")

    params = {key: source[key] for key in ("host", "port", "user") if source.get(key) is not None}
    if source.get("password") is not None:
        params["pass"] = rclone_obscure(source["password"])
    params |= options
    joined = "".join(f",{key}={rclone_quote(value)}" for key, value in params.items())
    return f":{backend}{joined}:{source.get('path') or '/'}"


def check_ftp_source(name: str, source: dict) -> None:
    if not source.get("host"):
        raise ValueError(f"job {name}: ftp source needs a host")
    options = source.get("options") or {}
    if not (isinstance(options, dict) and all(re.fullmatch(r"[a-z0-9:_-]+", str(key)) for key in options)):
        raise ValueError(f"job {name}: source options must map lftp setting names to values")
    # these become lines of an lftp script, where a line break would start a new command
    if any(re.search(r"[\r\n]", str(value)) for value in [*source.values(), *options.values()]):
        raise ValueError(f"job {name}: source settings must not contain line breaks")


def lftp_quote(value: object) -> str:
    text = str(value).lower() if isinstance(value, bool) else str(value)  # lftp wants true, YAML's str gives True
    return '"' + text.replace("\\", "\\\\").replace('"', '\\"') + '"'


def lftp_script(source: dict, mirror: Path, verbose: bool = False, exclude: list[str] | None = None) -> str:
    """The lftp commands that make the mirror match an FTP source."""
    settings = FTP_DEFAULT_OPTIONS | (source.get("options") or {})
    port, user = source.get("port", DEFAULT_PORTS["ftp"]), source.get("user", "anonymous")
    password = source.get("password", "anonymous")
    return "\n".join([
        *(f"set {key} {lftp_quote(value)}" for key, value in settings.items()),
        f"open -p {lftp_quote(port)} {lftp_quote(source['host'])}",
        f"user {lftp_quote(user)} {lftp_quote(password)}",
        f"mirror --delete --no-perms {lftp_excludes(exclude or [])}{'-v ' if verbose else ''}{lftp_quote(source.get('path') or '/')} {lftp_quote(mirror)}",
        "",
    ])


def sync_plan(source: str | dict, mirror: Path, verbose: bool, exclude: list[str] | None = None) -> tuple[list[str], str | None]:
    """The command that syncs a source into the mirror and what to feed it: FTP goes through lftp, the rest through rclone."""
    if isinstance(source, dict):
        # through stdin, so the password stays out of the process list
        return ["lftp"], lftp_script(source, mirror, verbose, exclude)
    command = ["rclone", "sync", source, str(mirror), "--contimeout", "15s", "--timeout", "60s", "--retries", "1", *rclone_excludes(exclude or [])]
    return [*command, "-v"] if verbose else command, None


def declared_probe(name: str, source: dict, probe: dict | None) -> dict:
    """The job's probe, defaulting to the declarative source's own host and port."""
    declared = {"host": source.get("host"), "port": source.get("port", DEFAULT_PORTS.get(source.get("type")))}
    merged = {key: value for key, value in declared.items() if value is not None} | (probe or {})
    if not {"host", "port"} <= merged.keys():
        raise ValueError(f"job {name}: source can't provide a probe, set probe host and port")
    return merged


def load_config(path: Path) -> dict:
    # .env never overrides the real environment (docker env_file / environment win)
    load_dotenv(path.parent / ".env")
    cfg = expand(yaml.safe_load(path.read_text()))
    # absolute from here on: a relative path follows the working directory, and a new one means a fresh mirror and lock
    cfg["state_dir"] = str(Path(cfg.get("state_dir") or os.environ.get("BACKUP_ON_SIGHT_STATE_DIR") or "state").resolve())
    log.info("config %s, state directory %s", path.resolve(), cfg["state_dir"])
    if cfg.get("webhook") is not None and not valid_webhook(cfg["webhook"]):
        raise ValueError("webhook must be an http(s) URL")
    for name, job in cfg["jobs"].items():
        # null is allowed: it opts the job out of the global webhook
        if job.get("webhook") is not None and not valid_webhook(job["webhook"]):
            raise ValueError(f"job {name}: webhook must be an http(s) URL")
        # the resolved list lives here, so one in the config would be silently replaced or used unchecked
        if "exclude" in job:
            raise ValueError(f"job {name}: exclude is a source setting, put it under source")
        if isinstance(job.get("source"), dict):
            declared = job["source"]
            check_source(name, declared)
            job["exclude"] = source_excludes(name, declared)
            if declared["type"] == "ftp":
                check_ftp_source(name, declared)  # stays as settings: lftp has no connection strings
            else:
                job["source"] = rclone_source(name, declared)
            job["probe"] = declared_probe(name, declared, job.get("probe"))
        if missing := {"probe", "source", "every"} - job.keys():
            raise ValueError(f"job {name}: missing {', '.join(sorted(missing))}")
        # the job name is every snapshot's unique tag: it must not split, match all snapshots, or be forged by another job
        if "," in str(name) or name == APP_NAME:
            raise ValueError(f"job {name}: name must not contain commas or be {APP_NAME}, it becomes a snapshot tag")
        tags = job.get("tags", [])
        if not (isinstance(tags, list) and all(isinstance(tag, str) and tag and "," not in tag for tag in tags)):
            raise ValueError(f"job {name}: tags must be a list of non-empty strings without commas")
        if clash := (set(tags) & cfg["jobs"].keys()) - {name}:
            raise ValueError(f"job {name}: tags match other job names: {', '.join(sorted(clash))}")
        restic = job_restic(cfg, name)
        if not isinstance(restic, dict) or {"repository", "password"} - restic.keys():
            raise ValueError(f"job {name}: needs restic repository and password, in the job or at the top level")
        try:
            positive = job_interval(cfg, name) > 0
        except (ValueError, KeyError, IndexError, TypeError):
            positive = False
        if not positive:  # zero or negative would probe in a tight loop
            raise ValueError(f"job {name}: probe interval must be a positive duration like 30s")
    log.debug("loaded %s: jobs %s", path, ", ".join(cfg["jobs"]))
    return cfg


def source_param(source: str, key: str) -> str | None:
    """Read a parameter from an rclone connection string (":ftp,host=1.2.3.4,port=21:/path")."""
    # shortcut: a quoted value containing ":" before the key hides it, parse quoting properly if that ever occurs
    match = re.match(rf""":?[^:/]*,{key}=(["']?)(.+?)\1(?=[,:])""", source)
    return match[2] if match else None


def source_host(source: str) -> str | None:
    return source_param(source, "host")


def is_up(host: str, port: int, timeout: float = 3) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def restic_env(cfg: dict, name: str) -> dict:
    restic = job_restic(cfg, name)
    return os.environ | {
        "RESTIC_REPOSITORY": restic["repository"],
        "RESTIC_PASSWORD": restic["password"],
    }


def repo_exists(env: dict) -> bool:
    # stderr stays visible: restic retries a failing backend for minutes and would otherwise look hung
    return subprocess.run(["restic", "cat", "config"], env=env, stdout=subprocess.DEVNULL).returncode == 0


def ensure_repo(cfg: dict, name: str) -> bool:
    env = restic_env(cfg, name)
    log.debug("%s: checking restic repository (%s)", name, "own" if "restic" in cfg["jobs"][name] else "shared")
    if repo_exists(env):
        log.debug("%s: restic repository is ready", name)
        return True
    # jobs sharing a repo must not init it twice
    # shortcut: one global lock, so a dead backend delays other first-time inits; key it by repo if that hurts
    with INIT_LOCK:
        if repo_exists(env):
            return True
        log.info("%s: initializing restic repository", name)
        return subprocess.run(["restic", "init"], env=env).returncode == 0


def job_dir(cfg: dict, name: str) -> Path:
    d = Path(cfg["state_dir"]) / name
    d.mkdir(parents=True, exist_ok=True)
    return d


def last_ok(cfg: dict, name: str) -> float:
    try:
        return float((job_dir(cfg, name) / "last_ok").read_text())
    except FileNotFoundError:
        return 0


def job_host(cfg: dict, name: str) -> str:
    source = cfg["jobs"][name]["source"]
    if isinstance(source, dict):
        return source["host"]
    # sources without host= (local paths, named remotes) fall back to the job name
    return source_host(source) or name


def job_endpoint(cfg: dict, name: str) -> str:
    """Where a job syncs from, safe to log: host, plus the port if the source sets one."""
    source = cfg["jobs"][name]["source"]
    port = source.get("port") if isinstance(source, dict) else source_param(source, "port")
    return f"{job_host(cfg, name)}:{port}" if port else job_host(cfg, name)


def job_tags(cfg: dict, name: str) -> list[str]:
    return list(dict.fromkeys([APP_NAME, name, *cfg["jobs"][name].get("tags", [])]))


def post_json(url: str, payload: dict) -> int:
    request = urllib.request.Request(url, json.dumps(payload).encode(), {"Content-Type": "application/json"})
    with urllib.request.urlopen(request, timeout=WEBHOOK_TIMEOUT) as response:
        return response.status


def notify(cfg: dict, name: str, phase: str, **event: object) -> None:
    """Call the job's webhook, or the global one. A failing webhook is logged and never affects the backup."""
    url = cfg["jobs"][name].get("webhook", cfg.get("webhook"))
    if url is None:
        return
    # never add source or repository here: they embed credentials
    payload = {
        "job": name,
        "phase": phase,
        "host": job_host(cfg, name),
        "tags": job_tags(cfg, name),
        "timestamp": datetime.now(UTC).isoformat(),
        **event,
    }
    log.debug("%s: sending %s webhook: %s", name, phase, payload)
    try:
        log.debug("%s: %s webhook answered %d", name, phase, post_json(url, payload))
    except (OSError, http.client.HTTPException) as exc:
        # the exception only, not the URL: webhook URLs often embed a token
        log.warning("%s: %s webhook failed: %s", name, phase, exc)


def back_up(cfg: dict, name: str, d: Path) -> str | None:
    """Sync source into the mirror, then snapshot it. Returns what went wrong, or None on success."""
    # checked per run, not at startup: one unreachable repo must not stop the other targets
    if not ensure_repo(cfg, name):
        return "restic repository unavailable, nothing synced"

    job, mirror = cfg["jobs"][name], d / "mirror"
    # not the source itself: it holds credentials
    log.info("%s: syncing from %s", name, job_endpoint(cfg, name))
    # sync deletes extraneous files only after a complete pass; an interrupted run leaves the mirror
    # half-updated but never snapshotted, and the next run resumes from it
    source = job["source"]
    command, script = sync_plan(source, mirror, log.isEnabledFor(logging.DEBUG), job.get("exclude"))
    # rclone prints the connection string in its errors, so hide it; errors="replace" because device filenames may not be UTF-8
    # shortcut: its errors appear when the sync ends, not live; stream them line by line if that hurts on long syncs
    started = time.monotonic()
    result = subprocess.run(command, input=script, stderr=subprocess.PIPE, errors="replace")
    secret = source.get("password") if isinstance(source, dict) else source
    sys.stderr.write(result.stderr.replace(secret, job_endpoint(cfg, name)) if secret else result.stderr)
    log.debug("%s: %s exited with %d after %.1fs", name, command[0], result.returncode, time.monotonic() - started)
    if result.returncode != 0:
        return "sync failed, no snapshot taken"
    # a source that looks empty is more likely a broken device or path than a real state worth recording
    if not any(path.is_file() for path in mirror.rglob("*")):
        return "source has no files, no snapshot taken"

    log.info("%s: snapshotting mirror", name)
    backup = ["restic", "backup", str(mirror), "--host", job_host(cfg, name), "--tag", ",".join(job_tags(cfg, name))]
    log.debug("%s: running %s", name, " ".join(backup))  # safe: credentials go through the environment
    started = time.monotonic()
    returncode = subprocess.run(backup, env=restic_env(cfg, name)).returncode
    log.debug("%s: restic exited with %d after %.1fs", name, returncode, time.monotonic() - started)
    if returncode != 0:
        return "restic backup failed"

    (d / "last_ok").write_text(str(time.time()))
    return None


def run_job(cfg: dict, name: str) -> bool:
    """Run one backup under the job's lock, with webhooks around it. Returns False if skipped or failed."""
    d = job_dir(cfg, name)
    with open(d / "lock", "w") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            log.warning("%s: already running, skipped", name)
            return False

        started = time.monotonic()
        notify(cfg, name, "start")
        error = "unexpected error"  # stays set if back_up raises, so the end webhook still reports a failure
        try:
            error = back_up(cfg, name, d)
        finally:
            notify(cfg, name, "end", success=error is None, error=error, duration_seconds=round(time.monotonic() - started, 1))

        if error:
            log.error("%s: %s", name, error)
            return False
        log.info("%s: done", name)
        return True


def due_at(cfg: dict, name: str) -> float:
    return last_ok(cfg, name) + duration(cfg["jobs"][name]["every"])


def local_time(timestamp: float) -> str:
    return datetime.fromtimestamp(timestamp, UTC).astimezone().strftime("%Y-%m-%d %H:%M:%S")


def watch(cfg: dict, name: str) -> None:
    job, poll = cfg["jobs"][name], job_interval(cfg, name)
    every, host, port = duration(job["every"]), job["probe"]["host"], int(job["probe"]["port"])
    log.info("%s: watching %s:%d, backup every %gs, checking every %gs", name, host, port, every, poll)
    announced = None  # the last state logged, so each one is only logged when it changes
    while True:
        due = due_at(cfg, name)
        if time.time() < due:
            if announced != due:
                log.debug("%s: next backup due at %s, once %s:%d is up", name, local_time(due), host, port)
                announced = due
        elif is_up(host, port):
            log.info("%s: %s:%d is up and backup is due", name, host, port)
            announced = None
            if not run_job(cfg, name):
                log.info("%s: retrying in %ds", name, RETRY_AFTER_FAILURE)
                time.sleep(RETRY_AFTER_FAILURE)
                continue
        elif announced != "down":
            log.debug("%s: backup is due but %s:%d is down", name, host, port)
            announced = "down"
        time.sleep(poll)


def main() -> None:
    parser = argparse.ArgumentParser(prog="bos")
    parser.add_argument("-c", "--config", type=Path, default=Path(os.environ.get("BACKUP_ON_SIGHT_CONFIG_FILE", "config/config.yaml")))
    parser.add_argument("-v", "--verbose", action="store_true", help="log debug details, including the sync tool's")
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("daemon", help="watch all jobs")
    run = sub.add_parser("run", help="back up a job now if it is due, ignoring the probe")
    run.add_argument("-f", "--force", action="store_true", help="back up even if the last backup is too recent")
    run.add_argument("job")
    args = parser.parse_args()

    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    cfg = load_config(args.config)

    if args.cmd == "run":
        if args.job not in cfg["jobs"]:
            parser.error(f"unknown job {args.job!r}, known: {', '.join(cfg['jobs'])}")
        if not args.force and time.time() < due_at(cfg, args.job):
            log.info("%s: last backup too recent, next due at %s; use --force to run now", args.job, local_time(due_at(cfg, args.job)))
            sys.exit(0)
        sys.exit(0 if run_job(cfg, args.job) else 1)

    # a crashed watcher must take the whole process down so the container restarts, not go silent
    threading.excepthook = lambda a: (log.critical("watcher crashed", exc_info=(a.exc_type, a.exc_value, a.exc_traceback)), os._exit(1))
    threads = [threading.Thread(target=watch, args=(cfg, name), daemon=True) for name in cfg["jobs"]]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
