#!/usr/bin/env python3
"""Daily digest — summarises last N hours of ticks and posts to Telegram.

Reuses helpers from tick.py rather than duplicating. Run via cron once
per day (suggested: 0 8 * * *).
"""
from __future__ import annotations

import argparse
import csv
import json
import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import tick as t  # noqa: E402


def load_metric_rows(paths: t.Paths, hours: int) -> list[dict]:
    p = paths.logs / "metrics.csv"
    if not p.exists():
        return []
    cutoff = datetime.now(timezone.utc) - timedelta(hours=hours)
    rows = []
    with p.open("r", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            ts_raw = row.get("ts", "")
            try:
                ts = datetime.fromisoformat(ts_raw.replace("Z", "+00:00"))
            except ValueError:
                continue
            if ts.tzinfo is None:
                ts = ts.replace(tzinfo=timezone.utc)
            if ts >= cutoff:
                rows.append(row)
    return rows


def count_pending(paths: t.Paths) -> int:
    p = paths.pending / "pending_actions.jsonl"
    if not p.exists():
        return 0
    n = 0
    with p.open("r", encoding="utf-8") as f:
        for line in f:
            if line.strip():
                n += 1
    return n


def percentile(values: list[float], q: float) -> float:
    if not values:
        return 0.0
    s = sorted(values)
    k = max(0, min(len(s) - 1, int(round(q * (len(s) - 1)))))
    return s[k]


def first_paragraph(text: str, limit: int) -> str:
    text = (text or "").strip()
    if not text:
        return "(empty)"
    head = text.split("\n\n", 1)[0]
    return head[:limit] + ("…" if len(head) > limit else "")


def build_digest(cfg: dict, paths: t.Paths) -> str:
    hours = int(cfg.get("digest", {}).get("hours_window", 24))
    rows = load_metric_rows(paths, hours)

    if not rows:
        return f"🦫 daily digest — no ticks in the last {hours}h"

    n = len(rows)
    failures = sum(1 for r in rows if str(r.get("parse_ok")).lower() not in ("true", "1"))
    compactions = sum(1 for r in rows if r.get("mode") == "compact")
    walls = []
    for r in rows:
        try:
            walls.append(float(r.get("wall_s") or 0))
        except ValueError:
            pass
    in_toks = sum(int(r.get("input_tokens_est") or 0) for r in rows if str(r.get("input_tokens_est") or "").isdigit())
    out_chars = sum(int(r.get("output_chars") or 0) for r in rows if str(r.get("output_chars") or "").isdigit())
    actions_exec = sum(int(r.get("actions_executed") or 0) for r in rows if str(r.get("actions_executed") or "").isdigit())
    actions_q = sum(int(r.get("actions_queued") or 0) for r in rows if str(r.get("actions_queued") or "").isdigit())
    drifts = [r for r in rows if (r.get("drift_flag") or "").strip()]

    pending = count_pending(paths)

    state_md = t.read_text(paths.state / "STATE.md", "(no STATE.md)")
    focus = first_paragraph(state_md, 400)

    journal = t.read_text(paths.state / "JOURNAL.md", "")
    j_lines = [ln for ln in journal.splitlines() if ln.strip()]
    sample = j_lines[-5:] if j_lines else ["(empty)"]

    avg_wall = sum(walls) / len(walls) if walls else 0
    p95 = percentile(walls, 0.95)

    lines = [
        f"🦫 daily digest — {datetime.now().strftime('%Y-%m-%d %H:%M')} ({hours}h window)",
        f"ticks: {n} · failures: {failures} ({(failures/n*100):.1f}%) · compactions: {compactions}",
        f"actions: {actions_exec}✓ executed · {actions_q}⏸ queued · {pending} total awaiting approval",
        f"in-tokens: ~{in_toks/1000:.0f}k · out-chars: ~{out_chars/1000:.0f}k",
        f"wall: avg {avg_wall:.1f}s · p95 {p95:.1f}s",
    ]
    if drifts:
        latest = drifts[-1].get("drift_flag", "")[:120]
        lines.append(f"drifts in window: {len(drifts)} · latest: {latest}")
    lines.append("")
    lines.append("current focus:")
    lines.append(focus)
    lines.append("")
    lines.append("last 5 journal lines:")
    lines.extend(sample)
    return "\n".join(lines)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", required=True)
    args = ap.parse_args()

    cfg = t.load_config(args.config)
    paths = t.Paths(cfg)
    paths.ensure()

    if not cfg.get("digest", {}).get("enabled", True):
        return 0

    msg = build_digest(cfg, paths)
    thread = (cfg["telegram"].get("digest_thread_id")
              or cfg["telegram"].get("log_thread_id"))
    res = t.telegram_send(cfg, msg, thread_id=thread, label="digest")
    if not res.get("ok"):
        sys.stderr.write(f"digest send failed: {json.dumps(res)}\n")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
