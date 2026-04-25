#!/usr/bin/env bash
# mako-zero install / first-time setup. Run once on the Hetzner box.
# Idempotent: safe to re-run; will not clobber an existing config or state.
set -euo pipefail

ROOT="${MAKO_ROOT:-/srv/mako-zero}"

echo "==> ensuring directories under $ROOT"
mkdir -p "$ROOT"/{state,notes,workdir,archive,logs,pending,prompts}
chmod 750 "$ROOT"

echo "==> copying source files"
SRC="$(cd "$(dirname "$0")" && pwd)"
install -m 0755 "$SRC/mako-tick.sh"   "$ROOT/mako-tick.sh"
install -m 0755 "$SRC/mako-digest.sh" "$ROOT/mako-digest.sh"
install -m 0644 "$SRC/tick.py"        "$ROOT/tick.py"
install -m 0644 "$SRC/digest.py"      "$ROOT/digest.py"
install -m 0644 "$SRC/requirements.txt" "$ROOT/requirements.txt"
install -m 0644 "$SRC/prompts/system.md"  "$ROOT/prompts/system.md"
install -m 0644 "$SRC/prompts/compact.md" "$ROOT/prompts/compact.md"

echo "==> seeding state files (won't overwrite existing)"
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

echo "==> config"
if [ ! -f "$ROOT/config.yaml" ]; then
    install -m 0600 "$SRC/config.example.yaml" "$ROOT/config.yaml"
    echo "    seeded config.yaml — EDIT IT before enabling cron"
else
    echo "    kept config.yaml"
fi

echo "==> python deps (system pip; switch to a venv if you prefer)"
python3 -m pip install --quiet -r "$ROOT/requirements.txt" || {
    echo "    pip install failed — install requests + pyyaml manually"
}

echo "==> done."
echo
echo "Next steps:"
echo "  1. \$EDITOR $ROOT/config.yaml   # fill in api keys, telegram bot, thread ids"
echo "  2. chmod 600 $ROOT/config.yaml"
echo "  3. test one tick:    sudo -u mako-zero $ROOT/mako-tick.sh"
echo "  4. test the digest:  sudo -u mako-zero $ROOT/mako-digest.sh"
echo "  5. check the run:    tail $ROOT/logs/metrics.csv ; cat $ROOT/state/LAST_RESULTS.md"
echo "  6. add to crontab:"
echo "     */2 * * * * $ROOT/mako-tick.sh"
echo "     0 8 * * *   $ROOT/mako-digest.sh"
