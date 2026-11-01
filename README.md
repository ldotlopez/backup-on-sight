# backup-on-sight

[![Latest release](https://img.shields.io/github/v/release/ldotlopez/backup-on-sight)](https://github.com/ldotlopez/backup-on-sight/releases/latest)
[![Build and release status](https://github.com/ldotlopez/backup-on-sight/actions/workflows/release.yml/badge.svg)](https://github.com/ldotlopez/backup-on-sight/actions/workflows/release.yml)
[![Buy Me a Coffee](https://img.shields.io/badge/Buy%20Me%20a%20Coffee-zepolson-FFDD00?logo=buymeacoffee&logoColor=black)](https://www.buymeacoffee.com/zepolson)

Back up network devices automatically when they show up.

Some devices are only online now and then: a handheld with its FTP server switched on, a laptop, a NAS that sleeps.
`backup-on-sight` watches for them. When a device is reachable and its last good backup is older than the
interval you set, it copies the device into a local mirror, with [lftp](https://lftp.yar.ru) for FTP and
[rclone](https://rclone.org) for everything else, then snapshots that mirror with [restic](https://restic.net).

> [!IMPORTANT]
> **This project was developed with AI assistance.** Most of the code, tests and documentation, including this
> README, were written by Claude Code (Anthropic) under the direction of the maintainer. It has not had an independent
> audit. So far it has only run against a PSP and a Switch over FTP, and the tests don't exercise restoring.
> Before you depend on it, read the code and **test a restore of your own data**.

## How it works

1. Each job has a watcher that checks whether a backup is due, and if so, tries a TCP connection to the job's probe.
2. If the probe answers, `lftp mirror` or `rclone sync` updates the job's mirror from the source.
3. If the sync succeeds and the mirror holds files, `restic backup` takes a snapshot of the mirror.
4. If anything fails, no snapshot is taken and the job retries after 5 minutes.

The mirror is kept between runs, so each sync only transfers changes, at the cost of local disk space for a full copy
of every source. An interrupted sync leaves the mirror half-updated, but it is never snapshotted, and the next run
continues from it.

## Requirements

- Python 3.13 and [uv](https://docs.astral.sh/uv/)
- `restic` on your `PATH`, plus `lftp` for FTP sources and `rclone` for all others

## Quick start

```sh
cp config/config.example.yaml config/config.yaml
cp config/.env.example config/.env      # edit both

uv run bos daemon
```

A `Dockerfile` and `docker-compose.yml` are included (`docker compose up -d --build`). The image has been run with
podman, but `docker compose` itself hasn't been tested, so expect to check it.

The container runs backups as the numeric user and group in `EUID` and `EGID`, which need no account in the image.
`docker-compose.yml` sets both to 1000, and without them the container runs as root. On each start it gives that user
ownership of anything in its state directory and `/restic` that it doesn't already own, so the first start can be slow
with large mirrors. It doesn't change the config file, so make sure that user can read it.

In the container, `BACKUP_ON_SIGHT_STATE_DIR` defaults to `/state` and `BACKUP_ON_SIGHT_CONFIG_FILE` to `/config.yaml`.
A `state_dir` in the config still wins, but only the variable's path gets its ownership fixed, so keep the two the same.

- Set `EUID` and `EGID` in `docker-compose.yml`, not `config/.env`: bash reports an error when `. config/.env` assigns
  `EUID`, which is read-only there.
- Run on-demand backups through the entrypoint, so they use the same user:
  `docker exec backup-on-sight docker-entrypoint.sh bos run psp`. A plain `docker exec` skips the entrypoint, so it runs
  as root without the container defaults.

## Configuration

```yaml
restic:                            # shared repository, for jobs without their own
  repository: "${RESTIC_REPOSITORY}"
  password: "${RESTIC_PASSWORD}"
# state_dir: ./state               # optional, the root of the data: <state_dir>/<job>/ holds its mirror, lock and last_ok
poll_interval: 30s                 # optional, default 30s
webhook: https://hooks.lan/backups # optional

jobs:
  psp:
    source: {type: ftp, host: 192.168.1.50, port: 1337, path: "/ms0:/"}
    probe: {interval: 10s}
    every: 24h
    tags: [console, saves]
```

| Job setting | Required | Meaning |
|---|---|---|
| `source` | Yes | Where to copy from: the settings below, or a string holding any rclone source, usually a [connection string](https://rclone.org/docs/#connection-strings) |
| `probe.host`, `probe.port` | Unless the source provides them | The TCP address that shows the device is online. Defaults to the source's host and port. A source string always needs both |
| `probe.interval` | No | Replaces `poll_interval` for this job |
| `every` | Yes | Minimum time between backups: a number of seconds, or a value like `90s`, `30m`, `24h`, `7d` |
| `tags` | No | Extra restic tags |
| `restic` | No | The job's own `repository` and `password`, replacing the shared repository |
| `webhook` | No | Replaces the global webhook for this job; `null` turns it off |

| Source setting | Required | Meaning |
|---|---|---|
| `type` | Yes | `ftp`, which uses lftp, or any other [rclone backend](https://rclone.org/overview/), like `sftp` |
| `host`, `port` | Depends on the backend | The device's address. `ftp` defaults to port 21 and `sftp` to 22 |
| `path` | No | The folder to back up. Defaults to `/` |
| `user`, `password` | Depends on the backend | Write the plain password. `ftp` defaults to `anonymous` |
| `options` | No | For `ftp`, [lftp settings](https://lftp.yar.ru/lftp-man.html) like `{"net:connection-limit": 2}`. For others, rclone backend options like `{key_file: /keys/nas}` |
| `exclude` | No | A list of paths to leave out, see [Excluding paths](#excluding-paths) |

`type: ftp` is set up for the fragile FTP servers on consoles: one connection, no TLS, no `FEAT`, and listings sent as
`LIST .`. Change that through `options` for a server that can take more, or that needs TLS.

A PSP needs `path: /ms0:/`, because `/` answers `LIST .` with an error and the job fails.

For anything else rclone can do, `source` can be a string holding any rclone source. That includes FTP, though rclone
fails on the Switch's servers.

Strings accept `${VAR}`, resolved from the environment or from a `.env` next to the config file (the environment
wins). A missing variable stops startup. Write a literal `$` as `$$`, and quote values inside `{...}`, such as URLs
containing `?`.

## Excluding paths

```yaml
source:
  type: ftp
  host: 192.168.1.50
  path: /ms0:/
  exclude:
    - /ms0:/ISO              # an absolute path: this one file or folder, with everything inside it
    - /ms0:/Game [USA].iso   # brackets in an absolute path are just part of the name
    - "*.tmp"                # a glob: matched by the sync tool, at any depth
```

Every entry must be one of these two:

- **An absolute path** starts with `/` and is the full path on the device, not a path relative to `path`. It has to be
  inside `path`, and one that isn't stops startup, as does one containing `*` or `?`.
- **A glob** is passed to the sync tool as written, so it follows the rules of
  [lftp](https://lftp.yar.ru/lftp-man.html) (`--exclude-glob`) or [rclone](https://rclone.org/filtering/) (`--exclude`),
  which differ. For example, lftp only matches a folder with a trailing `/`. Quote globs in YAML, since `*` means
  something there.

Anything else, like a relative path such as `PSP/SYSTEM`, is **undefined**: it may match something or nothing, and may
change between versions.

Excluded files are also removed from the mirror, so they stop appearing in new snapshots. Older snapshots keep them.

`exclude` is only available in the source settings, not for a source string.

## Repositories: shared or isolated

Jobs without a `restic` block share the top-level repository. They deduplicate against each other, and every snapshot
is tagged with its job name, so you can filter with `restic snapshots --tag psp`.

A tag is only a label, though. Anyone with a shared repository's password can read or delete every job's snapshots.
Give a job its own `restic` block when it needs real separation. You can mix both, and the top-level block is optional
if every job has its own.

Each snapshot is tagged `backup-on-sight`, the job name and any custom `tags`. Restic's host is the source's host, or
the job name if it doesn't have one, so if a device's address changes, its snapshots appear under a new host.

## Commands

```sh
bos [-c CONFIG] [-v] daemon              # watch all jobs
bos [-c CONFIG] [-v] run JOB             # back up now, unless the last backup is too recent
bos [-c CONFIG] [-v] run --force JOB     # back up now regardless
```

`run` ignores the probe and won't start if that job is already running. It exits with status 1 if the backup fails
or the job is already running, and 0 otherwise, **including when it skips a recent backup**.

The config comes from `-c`, then `$BACKUP_ON_SIGHT_CONFIG_FILE`, then `./config/config.yaml`. The state directory comes
from `state_dir` in the config, then `$BACKUP_ON_SIGHT_STATE_DIR`, then `./state`. Relative paths follow the directory
you start `bos` from, and the log shows the full paths at startup. Starting from another directory with a relative state
directory means a new mirror and a separate lock, so a second copy could run at once. Use an absolute path for services. `-v` adds debug detail: the next due
time, exit codes and timings, webhook payloads and responses, and the sync tool's `-v` output.

## Webhooks

The webhook URL gets a JSON `POST` when a backup starts and ends:

```json
{"job": "psp", "phase": "start", "host": "192.168.1.50", "tags": ["backup-on-sight", "psp"], "timestamp": "2026-11-01T12:00:00+00:00"}
```
```json
{"job": "psp", "phase": "end", "host": "192.168.1.50", "tags": ["backup-on-sight", "psp"], "timestamp": "2026-11-01T12:05:00+00:00",
 "success": false, "error": "sync failed, no snapshot taken", "duration_seconds": 300.0}
```

On success, `error` is `null`. A failing webhook is logged and never affects the backup. It isn't retried, and it
times out after 10 seconds. URLs must be `http(s)`. Runs that are skipped send nothing.

## Browse and restore

Load your repository settings, then use restic directly:

```sh
set -a; . config/.env; set +a

restic snapshots --tag psp
restic restore latest --tag psp --target ./restored
```

## Behaviour to know about

- **Deletions follow the source.** Like `rsync --delete`, the sync removes mirror files that are gone from the
  source. Older snapshots keep them.
- **A source with no files fails the run,** because that more likely means a broken device or path. By then a
  previously full mirror may already be emptied, so it's copied again once the files return. A source that only
  loses *most* of its files is still snapshotted as a success.
- **One unreadable file blocks the job.** Any sync error means no snapshot, unless you [exclude](#excluding-paths) the file.
- **Retention isn't handled.** `restic forget` and `prune` are never run, so snapshots accumulate until you run them.
- **Sync errors are delayed.** They appear when the sync ends, not as it runs.
- **Failing backends are slow to report.** restic retries for several minutes before giving up.

## Security

- Sources and repository URLs are kept out of the logs and webhook payloads, because they hold credentials. The sync
  tool's error output is filtered for the password or rclone source string, which has only been tested on connection
  failures.
- lftp gets its password through stdin. While an rclone sync runs, its source, including the obscured password, is
  visible to local users in the process list. rclone's obscuring is reversible, so it isn't encryption.
- FTP sends passwords unencrypted, and `type: ftp` doesn't try TLS unless you turn on `ftp:ssl-allow`.
- The `.env` file and `config/config.yaml` are gitignored. Keep secrets there.
- Use `https://` for REST repositories, since `http://` sends the login unencrypted.
- All credentials live on the machine running this tool, so isolated repositories don't protect you if that machine
  is compromised. Devices being backed up never hold restic credentials.

## Development

```sh
uv run pytest      # some tests need lftp, rclone and restic on PATH, and skip without them
uv run ruff check .
```

## License

[GPL-3.0-or-later](LICENSE)
