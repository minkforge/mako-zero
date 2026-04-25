#!/usr/bin/env bash
# mako-zero cron entrypoint.
# Acquires a non-blocking lock and runs one tick. Exits silently if the
# previous tick is still running. Designed to be called every 2 minutes.
set -u

ROOT="${MAKO_ROOT:-/srv/mako-zero}"
LOCK="${MAKO_LOCK:-/var/lock/mako-zero.lock}"
PYTHON="${MAKO_PYTHON:-/usr/bin/python3}"
CONFIG="${MAKO_CONFIG:-$ROOT/config.yaml}"

# Open lock fd, non-blocking. Exit 0 (silently) if already held.
exec 200>"$LOCK"
if ! flock -n 200; then
    exit 0
fi

cd "$ROOT" || exit 1

# Compaction takes priority if a previous tick scheduled one.
MODE="normal"
if [ -f "$ROOT/state/compact_pending.flag" ]; then
    MODE="compact"
fi

exec "$PYTHON" "$ROOT/tick.py" --config "$CONFIG" --mode "$MODE"
