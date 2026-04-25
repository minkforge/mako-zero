#!/usr/bin/env bash
# mako-zero daily digest cron entrypoint.
set -u

ROOT="${MAKO_ROOT:-/srv/mako-zero}"
PYTHON="${MAKO_PYTHON:-/usr/bin/python3}"
CONFIG="${MAKO_CONFIG:-$ROOT/config.yaml}"

cd "$ROOT" || exit 1
exec "$PYTHON" "$ROOT/digest.py" --config "$CONFIG"
