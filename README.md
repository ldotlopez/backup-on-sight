# hbw — home backup watch

Watches hosts on the network. When one is reachable (TCP `host:port`) and its last good backup is older
than `every`, it `rclone sync`s the source into a per-job mirror and snapshots that mirror with restic.

- An interrupted copy leaves the mirror half-updated; it is never snapshotted and the next run resumes from it.
- `rclone sync` deletes files gone from the source once a pass completes, like `rsync --delete`.
- The mirror is kept between runs, so each sync only transfers changes.
- A source with no files fails the run without a snapshot, because it more likely means a broken device or path. The mirror may already be emptied by then and is copied again once the files return.
- Snapshots are tagged `hbw`, the job name and the job's optional `tags`. Restic's host is the `host=` in the rclone source, or the job name if it has none.

## Repositories

Jobs use the top-level `restic` repository, told apart by their job name tag: `restic snapshots --tag psp`.
A job with its own `restic` block (`repository` and `password`) uses that repository instead, which isolates it
from the other targets. Both can be mixed, and the top-level block is optional if every job has its own.

## Setup

```sh
cp config/config.example.yaml config/config.yaml
cp config/.env.example config/.env      # fill in secrets; rclone passwords via `rclone obscure`
docker compose up -d --build
```

Config strings accept `${VAR}`, resolved from the environment or `config/.env` (the environment wins).
Write a literal `$` as `$$`, and quote values inside `{...}` flow mappings: `password: "${VAR}"`.

`poll_interval` sets how often jobs check if a backup is due and probe their host (default 30s). A job's
`probe: {interval: 10s}` replaces it for that job.

## Debugging

```sh
uv run hbw -v -c config/config.yaml daemon
```

`-v` adds when the next backup is due and whether the host is down (logged when that changes), exit codes and timings, webhook payloads and responses, and rclone's own `-v` output.
Sources and repository URLs stay out of the logs, because they hold credentials.

## Webhooks

Set `webhook` at the top level for all jobs. A job's own `webhook` replaces it, and `webhook: null` turns it off for
that job. The URL gets a JSON `POST` when a backup starts and ends. A run skipped because the job is already running sends nothing.

```json
{"job": "psp", "phase": "start", "host": "192.168.1.104", "tags": ["hbw", "psp"], "timestamp": "2026-11-01T12:00:00+00:00"}
{"job": "psp", "phase": "end", "host": "192.168.1.104", "tags": ["hbw", "psp"], "timestamp": "2026-11-01T12:05:00+00:00",
 "success": false, "error": "sync failed, no snapshot taken", "duration_seconds": 300.0}
```

`success` and `duration_seconds` are only on `end`, where `error` is `null` on success. A failing webhook is logged
and never affects the backup; it is not retried and times out after 10 seconds. Webhook URLs must be `http(s)`,
and can use `${VAR}` to keep a token in `config/.env`. Payloads never include the source or the repository, which hold credentials.

## On demand

```sh
uv run hbw -c config/config.yaml run psp            # skips if the last backup is too recent
uv run hbw -c config/config.yaml run --force psp    # backs up regardless
```

Both ignore the probe. Skipping exits with status 0. Neither starts if that job is already running.

## Browse backups

```sh
docker exec -it hbw sh -c 'RESTIC_REPOSITORY=/restic restic snapshots --tag psp'
```

## Development

```sh
uv run pytest      # integration tests need rclone and restic on PATH
```
