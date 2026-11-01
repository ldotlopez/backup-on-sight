# Device compatibility notes

What we learned testing real consoles, on 2026-11-01, with lftp 4.9.2 and rclone 1.60. Read this before changing how
FTP sources are synced, or before testing against a real device.

## Conclusions

- `type: ftp` uses lftp. rclone fails on both consoles, for different reasons.
- Other backends and raw source strings stay on rclone.
- One set of lftp defaults works on both consoles, so there are no per-device profiles. See `FTP_DEFAULT_OPTIONS` in
  `backup_on_sight.py`.
- A missing `path` means `/`. That was a deliberate compromise: it suits the Switch, and the PSP must set its own path.

## PSP (`FTPPSP` server, port 1337)

| Request | Answer |
|---|---|
| bare `LIST` | `550 Invalid directory.` |
| `LIST /some/path` | works |
| `CWD /some/path`, then `LIST .` | works |
| `LIST /` | works, shows one entry, `ms0:` |
| `CWD /`, then `LIST .` | `550`, so a job with `path: /` fails with an error |
| `FEAT` | an empty list |
| `OPTS UTF8 ON` | `501 bad OPTS` |
| `MDTM .` | `502`, which lftp ignores |

- Use `path: /ms0:/`.
- lftp changes folder and sends a bare `LIST`, so it needs `ftp:list-options: .` to turn that into `LIST .`.
- Incremental runs work: a second `mirror` sends no `RETR`.
- Transfers are slow, about 8-10 MB per minute. That was measured with another device syncing at the same time.
- rclone can list it with `concurrency=1,disable_epsv=true,disable_mlsd=true`, but we no longer use it here.

### The crash: cause unknown

The server went down twice during testing, and needed restarting by hand:

1. During rclone tests, without the options above.
2. During a series of short lftp commands. The last command that got an answer was a listing with `FEAT` on. The next
   session failed before sending any command.

We then tried to reproduce the second crash one test at a time, with the user checking the console after each:

| Test | Result |
|---|---|
| Listing, `FEAT` off, one connection | survived |
| `mirror` of `/ms0:/PSP/SYSTEM/` (3 files) | survived |
| Same `mirror` again | survived |
| Listing, `FEAT` on | survived |
| Three listings on successive connections | survived |
| The session from before the crash (`cls -1 /`, `cls -1 /ms0:/`, `du -s`) | survived |
| `bos run` on `/ms0:/PSP/SYSTEM/` | survived |

Nothing reproduced it, so nothing is banned. `ftp:use-feat` is off by default only as a precaution. The PSP also leaves
the network on its own at times, so being unreachable doesn't prove a crash.

**A full-card backup has never finished.** One run was stopped on purpose after about 39M and 17 files, with no crash.

## Switch

### Current server (port 5000, shows the SD card)

- It answers `PASV` with `227 192,168,1,134,253,191`, without parentheses, and `EPSV` with `502`. rclone 1.60 fails with
  `invalid PASV response format`. lftp and curl both accept it.
- With `FEAT` on, lftp lists through `MLSD`. With `FEAT` off, it uses `LIST .`, which also works.
- `LIST` can show dates like `Jan 1 1970`, but a second `mirror` with `FEAT` off still recopied nothing.
- One connection was refused for no known reason, and the retry worked a minute later.
- `/switch/DBI/saves/` backed up fully: 71 files, 134.6 MiB.

### Previous server (saves view)

- Only `CWD` followed by a bare `LIST` worked. A `LIST` with a path looked like an empty folder.
- rclone never sends `CWD`, so it failed on subfolders with `dirExists: object not found`.
- lftp's `LIST .` default was never tried on this server.

## lftp behaviour the app relies on

- A failed `mirror` exits non-zero, which blocks the snapshot. A missing folder and an unreadable file were both tested.
- `cmd:fail-exit` isn't what makes that happen. It stops the script at the first failed command, like a mistyped setting.
- A file listed with size 0 is created without a `RETR`, so a fake server must give unreadable files a real size to test
  failures.
- `file:charset` is set to UTF-8, because the container's C locale would otherwise mangle non-ASCII filenames.
- The script goes through stdin, so the password stays out of the process list.

## Testing on real devices

- The PSP is fragile, and the user may be watching it. Run one test at a time, and wait for confirmation that it's alive
  before the next one.
- Change one thing per test, and start with the gentlest option.
- Stop any running daemon first, so it doesn't compete with the tests.
- Devices hold a lot of data, so test small folders and cap the output: `/ms0:/PSP/SYSTEM/` on the PSP, `/bootloader/` on
  the Switch.
- Don't assume a device still runs the same server. The Switch's changed during this work.
