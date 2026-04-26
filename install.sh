#!/usr/bin/env bash
# mako-zero — host install with systemd. Idempotent.
#
# Run as root (or sudo) from the cloned repo. Installs code into
# /srv/mako-zero, seeds state and config, installs the systemd unit,
# and prints the next steps. On first run the supervisor seeds
# config.yaml from the example and parks; you edit it and
# `systemctl restart mako-zero`.

set -euo pipefail

ROOT="${MAKO_ROOT:-/srv/mako-zero}"
SERVICE_NAME="${MAKO_SERVICE:-mako-zero}"
SRC="$(cd "$(dirname "$0")" && pwd)"

if [ "$(id -u)" -ne 0 ]; then
    echo "ERROR: run as root (sudo $0). Mako runs as root by design on this single-purpose box."
    exit 1
fi

echo "==> ensuring directory tree at $ROOT"
mkdir -p "$ROOT"/{state,notes,workdir,archive,logs,pending,prompts}

echo "==> copying source files"
if [ "$(readlink -f "$SRC")" = "$(readlink -f "$ROOT")" ]; then
    echo "    SRC == ROOT — clone already lives at $ROOT, skipping copy"
else
    install -m 0644 "$SRC/tick.py"               "$ROOT/tick.py"
    install -m 0644 "$SRC/supervisor.py"         "$ROOT/supervisor.py"
    install -m 0644 "$SRC/digest.py"             "$ROOT/digest.py"
    install -m 0644 "$SRC/analyse.py"            "$ROOT/analyse.py"
    install -m 0644 "$SRC/scribe.py"             "$ROOT/scribe.py"
    install -m 0644 "$SRC/tg_listener.py"        "$ROOT/tg_listener.py"
    install -m 0644 "$SRC/cfg_cmd.py"            "$ROOT/cfg_cmd.py"
    install -m 0644 "$SRC/requirements.txt"      "$ROOT/requirements.txt"
    install -m 0644 "$SRC/config.example.yaml"   "$ROOT/config.example.yaml"
    install -m 0644 -d "$ROOT/prompts"
    install -m 0644 "$SRC/prompts/system.md"     "$ROOT/prompts/system.md"
    install -m 0644 "$SRC/prompts/compact.md"    "$ROOT/prompts/compact.md"
    install -m 0644 "$SRC/prompts/scribe.md"     "$ROOT/prompts/scribe.md"
fi

echo "==> seeding state files (idempotent — kept if already populated)"
seed() {
    local from="$1" to="$2"
    if [ ! -f "$to" ]; then
        install -m 0644 "$from" "$to"
        echo "    seeded $to"
    else
        echo "    kept   $to"
    fi
}
seed "$SRC/seed/MISSION.md"      "$ROOT/state/MISSION.md"
seed "$SRC/seed/CAPABILITIES.md" "$ROOT/state/CAPABILITIES.md"
seed "$SRC/seed/STATE.md"        "$ROOT/state/STATE.md"
seed "$SRC/seed/NEXT.md"         "$ROOT/state/NEXT.md"
seed "$SRC/seed/PERSONA.md"      "$ROOT/state/PERSONA.md"
seed "$SRC/seed/notes/INDEX.md"  "$ROOT/notes/INDEX.md"
[ -f "$ROOT/state/JOURNAL.md" ]      || : > "$ROOT/state/JOURNAL.md"
[ -f "$ROOT/state/LAST_RESULTS.md" ] || echo "_first tick — no prior results_" > "$ROOT/state/LAST_RESULTS.md"

echo "==> seeding config.yaml (only if missing)"
SEEDED_CONFIG=0
if [ ! -f "$ROOT/config.yaml" ]; then
    install -m 0600 "$SRC/config.example.yaml" "$ROOT/config.yaml"
    SEEDED_CONFIG=1
    echo "    seeded $ROOT/config.yaml — EDIT IT before starting the service"
else
    echo "    kept $ROOT/config.yaml"
fi

echo "==> Python deps"
if python3 -m pip install --quiet --break-system-packages -r "$ROOT/requirements.txt" 2>/dev/null; then
    :
elif python3 -m pip install --quiet -r "$ROOT/requirements.txt"; then
    :
else
    echo "    pip install failed — install requests + pyyaml manually before starting the service"
fi

echo "==> installing systemd unit"
install -m 0644 "$SRC/mako-zero.service" "/etc/systemd/system/$SERVICE_NAME.service"
systemctl daemon-reload

echo "==> done."
echo
if [ "$SEEDED_CONFIG" = "1" ]; then
    cat <<EOF
Next steps:

  1. \$EDITOR $ROOT/config.yaml
       fill in: llm.primary.api_key, llm.fallback.api_key,
                telegram.bot_token, telegram.{log,requests,approvals,digest}_thread_id

  2. systemctl enable --now $SERVICE_NAME

  3. journalctl -u $SERVICE_NAME -f
       you should see:
         [supervisor] starting (tick every 60s, digest at 08:00 local)
         [supervisor] tick(normal): start ...

Useful commands later:
  systemctl status   $SERVICE_NAME
  systemctl restart  $SERVICE_NAME
  systemctl stop     $SERVICE_NAME
  journalctl -u $SERVICE_NAME --since "1 hour ago"
  python3 $ROOT/digest.py  --config $ROOT/config.yaml    # fire a digest now
  python3 $ROOT/analyse.py --config $ROOT/config.yaml    # post-soak metrics
EOF
else
    cat <<EOF
Next steps:

  systemctl daemon-reload
  systemctl restart $SERVICE_NAME
  journalctl -u $SERVICE_NAME -f
EOF
fi
