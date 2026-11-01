# backup-on-sight

Back up network devices automatically when they show up.

Some devices are only online now and then: a handheld with its FTP server switched on, a laptop, a NAS that sleeps.
`backup-on-sight` watches for them. When a device is reachable and its last good backup is older than the
interval you set, it copies the device with [rclone](https://rclone.org) into a local mirror, then snapshots that
mirror with [restic](https://restic.net).

> [!IMPORTANT]
> **This project was developed with AI assistance.** Most of the code, tests and documentation, including this
> README, were written by Claude Code (Anthropic) under the direction of the maintainer. It has not had an independent
> audit. So far it has only run against one real target, a PSP over FTP, and the tests don't exercise restoring.
> Before you depend on it, read the code and **test a restore of your own data**.

## How it works

1. Each job has a watcher that checks whether a backup is due, and if so, tries a TCP connection to the job's probe.
2. If the probe answers, `rclone sync` updates the job's mirror from the source.
3. If the sync succeeds and the mirror holds files, `restic backup` takes a snapshot of the mirror.
4. If anything fails, no snapshot is taken and the job retries after 5 minutes.

The mirror is kept between runs, so each sync only transfers changes, at the cost of local disk space for a full copy
of every source. An interrupted sync leaves the mirror half-updated, but it is never snapshotted, and the next run
continues from it.

## Requirements

- Python 3.13 and [uv](https://docs.astral.sh/uv/)
- `rclone` and `restic` on your `PATH`

## Quick start

```sh
cp config/config.example.yaml config/config.yaml
cp config/.env.example config/.env      # edit both; rclone passwords come from `rclone obscure <password>`

uv run bos -c config/config.yaml daemon
```

A `Dockerfile` and `docker-compose.yml` are included (`docker compose up -d --build`), but they weren't run during
recent development, so expect to check them.

## Configuration

```yaml
restic:                            # shared repository, for jobs without their own
  repository: "${RESTIC_REPOSITORY}"
  password: "${RESTIC_PASSWORD}"
state_dir: "${BACKUP_ON_SIGHT_STATE_DIR}"   # one mirror, lock and last_ok per job
poll_interval: 30s                 # optional, default 30s
webhook: https://hooks.lan/backups # optional

jobs:
  psp:
    probe: {host: 192.168.1.50, port: 21, interval: 10s}
    source: ":ftp,host=192.168.1.50,user=anonymous,pass=${PSP_FTP_PASS}:/PSP/SAVEDATA"
    every: 24h
    tags: [console, saves]
```

| Job setting | Required | Meaning |
|---|---|---|
| `probe.host`, `probe.port` | Yes | The TCP address that shows the device is online |
| `probe.interval` | No | Replaces `poll_interval` for this job |
| `source` | Yes | Any rclone source, usually a [connection string](https://rclone.org/docs/#connection-strings) |
| `every` | Yes | Minimum time between backups: a number of seconds, or a value like `90s`, `30m`, `24h`, `7d` |
| `tags` | No | Extra restic tags |
| `restic` | No | The job's own `repository` and `password`, replacing the shared repository |
| `webhook` | No | Replaces the global webhook for this job; `null` turns it off |

Strings accept `${VAR}`, resolved from the environment or from a `.env` next to the config file (the environment
wins). A missing variable stops startup. Write a literal `$` as `$$`, and quote values inside `{...}`, such as URLs
containing `?`.

## Repositories: shared or isolated

Jobs without a `restic` block share the top-level repository. They deduplicate against each other, and every snapshot
is tagged with its job name, so you can filter with `restic snapshots --tag psp`.

A tag is only a label, though. Anyone with a shared repository's password can read or delete every job's snapshots.
Give a job its own `restic` block when it needs real separation. You can mix both, and the top-level block is optional
if every job has its own.

Each snapshot is tagged `backup-on-sight`, the job name and any custom `tags`. Restic's host is the `host=` value
from the rclone source, or the job name if there isn't one, so if a device's address changes, its snapshots
appear under a new host.

## Commands

```sh
bos [-c CONFIG] [-v] daemon              # watch all jobs
bos [-c CONFIG] [-v] run JOB             # back up now, unless the last backup is too recent
bos [-c CONFIG] [-v] run --force JOB     # back up now regardless
```

`run` ignores the probe and won't start if that job is already running. It exits with status 1 if the backup fails
or the job is already running, and 0 otherwise, **including when it skips a recent backup**.

The config defaults to `config.yaml`, or `$BACKUP_ON_SIGHT_CONFIG` if set. `-v` adds debug detail: the next due
time, exit codes and timings, webhook payloads and responses, and rclone's `-v` output.

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

- **Deletions follow the source.** Like `rsync --delete`, `rclone sync` removes mirror files that are gone from the
  source once a pass finishes without errors. Older snapshots keep them.
- **A source with no files fails the run,** because that more likely means a broken device or path. By then a
  previously full mirror may already be emptied, so it's copied again once the files return. A source that only
  loses *most* of its files is still snapshotted as a success.
- **One unreadable file blocks the job.** Any rclone error means no snapshot, and there's no way to exclude files yet.
- **Retention isn't handled.** `restic forget` and `prune` are never run, so snapshots accumulate until you run them.
- **rclone's output is delayed.** Its messages appear when the sync ends, not as it runs.
- **Failing backends are slow to report.** restic retries for several minutes before giving up.

## Security

- Sources and repository URLs are kept out of the logs and webhook payloads, because they hold credentials. rclone's
  error output is filtered for the exact source string, which has only been tested on connection failures.
- While a sync runs, the rclone source, including its obscured password, is visible to local users in the process
  list. rclone's obscuring is reversible, so it isn't encryption.
- The `.env` file and `config/config.yaml` are gitignored. Keep secrets there.
- Use `https://` for REST repositories, since `http://` sends the login unencrypted.
- All credentials live on the machine running this tool, so isolated repositories don't protect you if that machine
  is compromised. Devices being backed up never hold restic credentials.

## Development

```sh
uv run pytest      # some tests need rclone and restic on PATH, and skip without them
uv run ruff check .
```

## License

[GPL-3.0-or-later](LICENSE)
