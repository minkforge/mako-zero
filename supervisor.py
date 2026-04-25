#!/usr/bin/env python3
"""Mako-zero supervisor — single-process scheduler for Docker.

Runs `tick.py` on a fixed cadence and `digest.py` once a day at the
configured local hour. One subprocess at a time (so no flock needed).
Handles SIGTERM cleanly so `docker stop` exits in <10s.
"""
from __future__ import annotations

import argparse
import os
import shutil
import signal
import subprocess
import sys
import threading
import time
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import tick as t  # noqa: E402


SHUTDOWN = threading.Event()
APP_DIR = Path(__file__).resolve().parent
SEED_DIR = APP_DIR / "seed"


def bootstrap(cfg_path: str) -> bool:
    """Idempotent first-boot setup. Returns True if config exists and the
    supervisor can continue, False if config was just seeded and the user
    needs to edit it before next start."""
    cfg_p = Path(cfg_path)
    if not cfg_p.exists():
        seed = APP_DIR / "config.example.yaml"
        if seed.exists():
            cfg_p.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy(seed, cfg_p)
            try:
                os.chmod(cfg_p, 0o600)
            except OSError:
                pass
            print(f"[bootstrap] seeded {cfg_path} from {seed}", flush=True)
            print("[bootstrap] EDIT IT (api keys, telegram bot/threads) then restart the service", flush=True)
            return False
        print(f"[bootstrap] no config at {cfg_path} and no config.example.yaml in {APP_DIR}", flush=True)
        return False

    cfg = t.load_config(cfg_path)
    state_dir = Path(cfg["paths"]["state"])
    notes_dir = Path(cfg["paths"]["notes"])
    state_dir.mkdir(parents=True, exist_ok=True)
    notes_dir.mkdir(parents=True, exist_ok=True)
    Path(cfg["paths"]["workdir"]).mkdir(parents=True, exist_ok=True)
    Path(cfg["paths"]["archive"]).mkdir(parents=True, exist_ok=True)
    Path(cfg["paths"]["logs"]).mkdir(parents=True, exist_ok=True)
    Path(cfg["paths"]["pending"]).mkdir(parents=True, exist_ok=True)

    seed_pairs = [
        (SEED_DIR / "MISSION.md",         state_dir / "MISSION.md"),
        (SEED_DIR / "CAPABILITIES.md",    state_dir / "CAPABILITIES.md"),
        (SEED_DIR / "STATE.md",           state_dir / "STATE.md"),
        (SEED_DIR / "NEXT.md",            state_dir / "NEXT.md"),
        (SEED_DIR / "PERSONA.md",         state_dir / "PERSONA.md"),
        (SEED_DIR / "notes" / "INDEX.md", notes_dir / "INDEX.md"),
    ]
    for src, dst in seed_pairs:
        if dst.exists() or not src.exists():
            continue
        shutil.copy(src, dst)
        print(f"[bootstrap] seeded {dst}", flush=True)

    journal = state_dir / "JOURNAL.md"
    if not journal.exists():
        journal.write_text("", encoding="utf-8")
    last = state_dir / "LAST_RESULTS.md"
    if not last.exists():
        last.write_text("_first tick — no prior results_\n", encoding="utf-8")
    return True


def handle_signal(signum, frame):
    print(f"[supervisor] caught signal {signum}, shutting down", flush=True)
    SHUTDOWN.set()


def run_subprocess(label: str, args: list[str], timeout_s: int) -> None:
    print(f"[supervisor] {label}: start {' '.join(args)}", flush=True)
    t0 = time.time()
    try:
        proc = subprocess.run(args, timeout=timeout_s, check=False)
        rc = proc.returncode
    except subprocess.TimeoutExpired:
        rc = -1
        print(f"[supervisor] {label}: TIMEOUT after {timeout_s}s", flush=True)
    dt = time.time() - t0
    print(f"[supervisor] {label}: done rc={rc} in {dt:.1f}s", flush=True)


def run_tick(cfg: dict, cfg_path: str) -> None:
    state_dir = Path(cfg["paths"]["state"])
    mode = "compact" if (state_dir / "compact_pending.flag").exists() else "normal"
    timeout_s = cfg.get("supervisor", {}).get("tick_timeout_s", 300)
    run_subprocess(f"tick({mode})",
                   [sys.executable, str(Path(__file__).parent / "tick.py"),
                    "--config", cfg_path, "--mode", mode],
                   timeout_s)


def run_digest(cfg: dict, cfg_path: str) -> None:
    timeout_s = cfg.get("supervisor", {}).get("digest_timeout_s", 60)
    run_subprocess("digest",
                   [sys.executable, str(Path(__file__).parent / "digest.py"),
                    "--config", cfg_path],
                   timeout_s)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", required=True)
    args = ap.parse_args()

    if not bootstrap(args.config):
        # Config was just seeded; user needs to edit it before we run.
        # Sleep forever rather than crash-looping under systemd.
        print("[supervisor] waiting for config edit + service restart", flush=True)
        signal.signal(signal.SIGTERM, handle_signal)
        signal.signal(signal.SIGINT, handle_signal)
        SHUTDOWN.wait()
        return 0

    cfg = t.load_config(args.config)
    paths = t.Paths(cfg)
    paths.ensure()

    sup = cfg.get("supervisor", {})
    tick_interval = int(sup.get("tick_interval_s", 120))
    digest_hour = int(sup.get("digest_hour_local", 8))
    poll_s = 5

    signal.signal(signal.SIGTERM, handle_signal)
    signal.signal(signal.SIGINT, handle_signal)

    print(f"[supervisor] starting (tick every {tick_interval}s, digest at {digest_hour:02d}:00 local)", flush=True)
    print(f"[supervisor] config: {args.config}", flush=True)
    print(f"[supervisor] TZ: {os.environ.get('TZ', '(unset)')} · local now: {datetime.now().isoformat(timespec='seconds')}", flush=True)

    next_tick = time.time()  # run one immediately on boot
    last_digest_date = None

    while not SHUTDOWN.is_set():
        now = time.time()

        if now >= next_tick:
            try:
                run_tick(cfg, args.config)
            except Exception as e:
                print(f"[supervisor] tick error: {e!r}", flush=True)
            next_tick = time.time() + tick_interval

        local = datetime.now()
        if local.hour == digest_hour and last_digest_date != local.date():
            try:
                run_digest(cfg, args.config)
            except Exception as e:
                print(f"[supervisor] digest error: {e!r}", flush=True)
            last_digest_date = local.date()

        SHUTDOWN.wait(timeout=poll_s)

    print("[supervisor] clean exit", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
