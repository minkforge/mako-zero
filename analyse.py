#!/usr/bin/env python3
"""Soak analysis — reads logs/metrics.csv and prints a one-page summary.

Run after a 24h or 48h soak to decide cadence / cap tuning. Pure stdlib.
"""
from __future__ import annotations

import argparse
import csv
import sys
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import tick as t  # noqa: E402


def percentile(values: list[float], q: float) -> float:
    if not values:
        return 0.0
    s = sorted(values)
    k = max(0, min(len(s) - 1, int(round(q * (len(s) - 1)))))
    return s[k]


def histo(values: list[float], buckets: int = 10, width: int = 40) -> str:
    if not values:
        return "(no data)"
    lo, hi = min(values), max(values)
    if lo == hi:
        return f"  all {lo:.2f}  ({len(values)} samples)"
    step = (hi - lo) / buckets
    counts = [0] * buckets
    for v in values:
        idx = min(buckets - 1, int((v - lo) / step))
        counts[idx] += 1
    peak = max(counts)
    lines = []
    for i, c in enumerate(counts):
        bar = "█" * int(width * c / peak) if peak else ""
        lines.append(f"  {lo + i*step:7.2f} – {lo + (i+1)*step:7.2f}  {bar} {c}")
    return "\n".join(lines)


def parse_int(x) -> int | None:
    try:
        return int(x)
    except (TypeError, ValueError):
        return None


def parse_float(x) -> float | None:
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


def fmt_count(n: int, total: int) -> str:
    pct = (n / total * 100) if total else 0
    return f"{n} ({pct:.1f}%)"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", required=True)
    ap.add_argument("--metrics", help="path to metrics.csv (default from config)")
    args = ap.parse_args()

    cfg = t.load_config(args.config)
    paths = t.Paths(cfg)
    metrics_path = Path(args.metrics) if args.metrics else paths.logs / "metrics.csv"
    if not metrics_path.exists():
        print(f"no metrics.csv at {metrics_path}", file=sys.stderr)
        return 1

    rows: list[dict] = []
    with metrics_path.open("r", encoding="utf-8") as f:
        rows = list(csv.DictReader(f))

    if not rows:
        print("no rows yet")
        return 0

    n = len(rows)
    first_ts = rows[0].get("ts", "?")
    last_ts = rows[-1].get("ts", "?")

    # parse timestamps for span
    def ts(r):
        try:
            return datetime.fromisoformat(r["ts"].replace("Z", "+00:00"))
        except Exception:
            return None
    span_h = None
    a, b = ts(rows[0]), ts(rows[-1])
    if a and b:
        span_h = (b - a).total_seconds() / 3600

    # collect
    walls = [v for v in (parse_float(r.get("wall_s")) for r in rows) if v is not None]
    in_toks = [v for v in (parse_int(r.get("input_tokens_est")) for r in rows) if v is not None]
    out_chars = [v for v in (parse_int(r.get("output_chars")) for r in rows) if v is not None]
    parse_ok = sum(1 for r in rows if str(r.get("parse_ok")).lower() in ("true", "1"))
    parse_fail = n - parse_ok
    modes = Counter(r.get("mode", "?") for r in rows)
    providers = Counter(r.get("provider_used", "?") for r in rows)
    models = Counter(r.get("model_used", "?") for r in rows)
    drifts = sum(1 for r in rows if (r.get("drift_flag") or "").strip())
    compact_flag = sum(1 for r in rows if str(r.get("compact_now")).lower() in ("true", "1"))
    actions_exec = sum((parse_int(r.get("actions_executed")) or 0) for r in rows)
    actions_q = sum((parse_int(r.get("actions_queued")) or 0) for r in rows)
    actions_total = sum((parse_int(r.get("actions_count")) or 0) for r in rows)

    # failure types from `failures` column
    fail_samples = [r["failures"] for r in rows if r.get("failures")][-10:]

    # output
    print(f"mako-zero soak analysis")
    print(f"  metrics: {metrics_path}")
    print(f"  span:    {first_ts}  →  {last_ts}" + (f"  ({span_h:.1f}h)" if span_h else ""))
    print(f"  ticks:   {n}")
    if span_h:
        print(f"  rate:    {n/span_h:.1f} ticks/hr  (cadence target: {3600/cfg['supervisor']['tick_interval_s']:.1f}/hr)" if "supervisor" in cfg else f"  rate:    {n/span_h:.1f} ticks/hr")
    print()

    print("parse")
    print(f"  ok:      {fmt_count(parse_ok, n)}")
    print(f"  fail:    {fmt_count(parse_fail, n)}")
    print()

    print("modes")
    for k, v in modes.most_common():
        print(f"  {k:<10} {fmt_count(v, n)}")
    print()

    print("providers used")
    for k, v in providers.most_common():
        print(f"  {k:<10} {fmt_count(v, n)}")
    print()

    print("models used")
    for k, v in models.most_common():
        print(f"  {k:<30} {fmt_count(v, n)}")
    print()

    print("wall clock (s)")
    if walls:
        print(f"  count:   {len(walls)}")
        print(f"  mean:    {sum(walls)/len(walls):.2f}")
        print(f"  p50:     {percentile(walls, 0.50):.2f}")
        print(f"  p95:     {percentile(walls, 0.95):.2f}")
        print(f"  p99:     {percentile(walls, 0.99):.2f}")
        print(f"  max:     {max(walls):.2f}")
        print(histo(walls, buckets=10))
    print()

    print("input tokens (estimated)")
    if in_toks:
        print(f"  total:   ~{sum(in_toks)/1000:.0f}k")
        print(f"  mean:    {sum(in_toks)/len(in_toks):.0f}")
        print(f"  p50:     {percentile([float(v) for v in in_toks], 0.50):.0f}")
        print(f"  p95:     {percentile([float(v) for v in in_toks], 0.95):.0f}")
        print(f"  max:     {max(in_toks)}")
    print()

    print("output chars")
    if out_chars:
        print(f"  total:   ~{sum(out_chars)/1000:.0f}k")
        print(f"  mean:    {sum(out_chars)/len(out_chars):.0f}")
        print(f"  p50:     {percentile([float(v) for v in out_chars], 0.50):.0f}")
        print(f"  p95:     {percentile([float(v) for v in out_chars], 0.95):.0f}")
        print(f"  max:     {max(out_chars)}")
    print()

    print("actions")
    print(f"  emitted:   {actions_total}")
    print(f"  executed:  {actions_exec}")
    print(f"  queued:    {actions_q}")
    print()

    print("flags")
    print(f"  drift:        {drifts}")
    print(f"  compact_now:  {compact_flag}")
    print()

    if fail_samples:
        print("recent failures (up to 10):")
        for s in fail_samples:
            print(f"  - {s[:200]}")
        print()

    return 0


if __name__ == "__main__":
    sys.exit(main())
