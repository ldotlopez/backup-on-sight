#!/bin/sh
# POSIX sh only: EUID is read-only in bash, which would ignore the value passed in.
set -eu

: "${BACKUP_ON_SIGHT_STATE_DIR:=/state}"
: "${BACKUP_ON_SIGHT_CONFIG_FILE:=/config.yaml}"
# An arbitrary EUID has no home directory, and restic and rclone keep their cache and config there.
HOME="$BACKUP_ON_SIGHT_STATE_DIR/.home"
export BACKUP_ON_SIGHT_STATE_DIR BACKUP_ON_SIGHT_CONFIG_FILE HOME

# A relative path would land in the container's disposable layer, and "/" would be chowned whole below.
case "$BACKUP_ON_SIGHT_STATE_DIR" in
    / | [!/]*)
        echo "BACKUP_ON_SIGHT_STATE_DIR must be an absolute path other than /, got '$BACKUP_ON_SIGHT_STATE_DIR'" >&2
        exit 1
        ;;
esac

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

mkdir -p "$BACKUP_ON_SIGHT_STATE_DIR" /restic
# Only touch what is wrong: a recursive chown would rewrite every mirror file on each start.
find "$BACKUP_ON_SIGHT_STATE_DIR" /restic \( ! -user "$uid" -o ! -group "$gid" \) -exec chown -h "$uid:$gid" {} +

exec setpriv --reuid="$uid" --regid="$gid" --clear-groups "$@"
