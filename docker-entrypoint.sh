#!/bin/sh
# POSIX sh only: EUID is read-only in bash, which would ignore the value passed in.
set -eu

# No colon: an empty value must fail below, not silently run as root.
uid="${EUID-0}"
gid="${EGID-$uid}"
for id in "$uid" "$gid"; do
    case "$id" in
        '' | *[!0-9]*)
            echo "EUID and EGID must be numeric, got '$uid' and '$gid'" >&2
            exit 1
            ;;
    esac
done

# Started with `--user`, or asked to stay root: nothing to fix and nothing to drop.
if [ "$(id -u)" -ne 0 ] || [ "$uid:$gid" = "0:0" ]; then
    exec "$@"
fi

mkdir -p /state /restic
# Only touch what is wrong: a recursive chown would rewrite every mirror file on each start.
find /state /restic \( ! -user "$uid" -o ! -group "$gid" \) -exec chown -h "$uid:$gid" {} +

exec setpriv --reuid="$uid" --regid="$gid" --clear-groups "$@"
