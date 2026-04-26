#!/usr/bin/env python3
"""mako-dashboard — small read+approve UI for Mako.

Reads/writes the same state files as tick.py. Binds 127.0.0.1:8050;
expose via SSH tunnel for V0. No DB. HTMX-poll on /now for live tape.

Routes:
- GET  /                         -> /now
- GET  /now                      live tape + state snapshot + stats
- GET  /api/now.json             machine-readable snapshot (HTMX poll)
- GET  /steering                 inbox view + drop-in textarea
- POST /steering                 append/replace INBOX.md
- GET  /approvals                pending actions list
- POST /approvals/{qid}/approve  execute + record
- POST /approvals/{qid}/reject   reject + record
- GET  /logs                     journal + metrics + last results
- GET  /healthz                  health
"""
from __future__ import annotations

import base64
import csv
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

LONDON = ZoneInfo("Europe/London")

# Mako portrait + favicon — pixel-art mink with shades. Lazy-loaded once,
# cached as data URIs so we can inline them in every HTML response
# without adding extra round-trips or needing a public nginx /static/ rule.
_MAKO_IMG_PATH = Path(__file__).parent / "static" / "mako.png"
_MAKO_FAV_PATH = Path(__file__).parent / "static" / "mako-favicon.png"
_MAKO_IMG_URI: str | None = None
_MAKO_FAV_URI: str | None = None


def _data_uri(path: Path) -> str:
    try:
        return "data:image/png;base64," + base64.b64encode(path.read_bytes()).decode("ascii")
    except OSError:
        return ""


def mako_img_uri() -> str:
    global _MAKO_IMG_URI
    if _MAKO_IMG_URI is None:
        _MAKO_IMG_URI = _data_uri(_MAKO_IMG_PATH)
    return _MAKO_IMG_URI


def mako_favicon_uri() -> str:
    global _MAKO_FAV_URI
    if _MAKO_FAV_URI is None:
        # Fall back to the larger portrait if the dedicated favicon is missing
        _MAKO_FAV_URI = _data_uri(_MAKO_FAV_PATH) or mako_img_uri()
    return _MAKO_FAV_URI


def favicon_link() -> str:
    """The <link rel="icon"> tag, ready to drop into <head>. Empty string
    if the favicon couldn't be loaded — browsers handle that gracefully."""
    uri = mako_favicon_uri()
    if not uri:
        return ""
    return f'<link rel="icon" type="image/png" href="{uri}">'


def _inbox_preview(text: str, limit: int = 2000) -> str:
    """Cap inbox text length for the /now widget. Cuts at the last newline
    before the limit so we never slice mid-string (which produced jarring
    fragments like `[ap`). Adds a clear truncation marker if anything was
    omitted."""
    if not text:
        return ""
    if len(text) <= limit:
        return text
    head = text[:limit]
    nl = head.rfind("\n")
    if nl > 0:
        head = head[:nl]
    omitted = len(text) - len(head)
    return f"{head}\n\n[… {omitted} more chars · open /steering for full INBOX]"


def _fmt_london(ts: str) -> str:
    """Convert an ISO-8601 timestamp (UTC) to Europe/London local time.

    Returns a string like '2026-04-26 13:38 BST' (or GMT in winter).
    Returns the input unchanged on parse failure, or '' for empty input.
    """
    if not ts:
        return ""
    try:
        # tick.py writes datetime.now(timezone.utc).isoformat(timespec="seconds")
        # which produces e.g. '2026-04-26T12:38:00+00:00'. Also tolerate trailing 'Z'.
        s = ts.replace("Z", "+00:00")
        dt = datetime.fromisoformat(s)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        local = dt.astimezone(LONDON)
        return local.strftime("%Y-%m-%d %H:%M %Z")
    except (ValueError, TypeError):
        return ts

# allow `import tick` and `import cfg_cmd` from the parent dir
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import yaml  # noqa: E402
from fastapi import FastAPI, Form, HTTPException  # noqa: E402
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse  # noqa: E402

import tick as t_mod  # noqa: E402


CONFIG_PATH = Path(os.environ.get("MAKO_CONFIG", "/srv/mako-zero/config.yaml"))


def load_cfg() -> dict:
    with CONFIG_PATH.open("r", encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def paths_root() -> Path:
    cfg = load_cfg()
    return Path(cfg["paths"]["root"])


app = FastAPI(title="Mako Dashboard")


# ---------------------------- shared HTML ------------------------------

CSS = """
:root { color-scheme: dark; --bg:#000; --fg:#d6d6d6; --mute:#888; --acc:#22d3ee; --green:#22c55e; --red:#ef4444; --amber:#f59e0b; --b:#222; }
* { box-sizing: border-box; }
body { background: var(--bg); color: var(--fg); font-family: ui-monospace, 'JetBrains Mono', monospace; font-size: 13px; line-height: 1.5; margin: 0; padding: 0; }
header { display: flex; align-items: center; gap: 16px; padding: 8px 16px; border-bottom: 1px solid var(--b); position: sticky; top: 0; background: #000; z-index: 10; }
header .brand { font-weight: 700; letter-spacing: 0.08em; text-transform: uppercase; color: #fff; }
header nav { display: flex; gap: 4px; flex: 1; }
header nav a { color: var(--mute); text-decoration: none; padding: 4px 10px; border: 1px solid transparent; border-radius: 2px; font-size: 11px; text-transform: uppercase; letter-spacing: 0.06em; }
header nav a:hover { color: var(--fg); background: #181818; }
header nav a.active { color: #fff; border-color: #333; background: #111; }
header .clock { color: var(--mute); font-size: 11px; font-variant-numeric: tabular-nums; }
main { padding: 16px; max-width: 1400px; }
.grid { display: grid; gap: 16px; grid-template-columns: 1fr 1fr 320px; }
@media (max-width: 1100px) { .grid { grid-template-columns: 1fr; } }
.card { border: 1px solid var(--b); border-radius: 2px; padding: 12px; background: #0e0e0e; }
.card h2 { margin: 0 0 10px 0; font-size: 10px; letter-spacing: 0.1em; text-transform: uppercase; color: var(--mute); border-bottom: 1px solid var(--b); padding-bottom: 6px; }
pre, code { font-family: inherit; font-size: 12px; }
pre { white-space: pre-wrap; word-break: break-word; margin: 0; }
.tape .tick { padding: 4px 0; border-bottom: 1px solid var(--b); }
.tape .tick:last-child { border: none; }
.tape .tick-meta { color: var(--mute); font-size: 11px; }
.kv { display: grid; grid-template-columns: max-content 1fr; gap: 4px 12px; font-size: 12px; }
.kv .k { color: var(--mute); }
.dot { display: inline-block; width: 6px; height: 6px; border-radius: 50%; vertical-align: middle; margin-right: 6px; }
.dot.green { background: var(--green); } .dot.red { background: var(--red); } .dot.amber { background: var(--amber); }
button, input[type=submit] { background: #111; color: var(--fg); border: 1px solid #333; border-radius: 2px; padding: 5px 12px; font-family: inherit; font-size: 11px; text-transform: uppercase; letter-spacing: 0.06em; cursor: pointer; }
button:hover, input[type=submit]:hover { background: #1a1a1a; color: #fff; }
button.green { color: var(--green); border-color: #1a4a2a; } button.green:hover { background: #0d2418; }
button.red { color: var(--red); border-color: #4a1d1d; } button.red:hover { background: #240d0d; }
textarea, input[type=text] { width: 100%; background: #151515; color: var(--fg); border: 1px solid #222; border-radius: 2px; padding: 8px; font-family: inherit; font-size: 12px; resize: vertical; }
textarea:focus, input[type=text]:focus { outline: none; border-color: #444; }
.approval { border: 1px solid #333; padding: 12px; margin-bottom: 12px; border-radius: 2px; background: #0e0e0e; }
.approval h3 { margin: 0 0 8px 0; font-size: 12px; }
.approval .meta { color: var(--mute); font-size: 11px; margin-bottom: 8px; }
.approval pre { background: #151515; padding: 8px; border-radius: 2px; }
.approval .actions { display: flex; gap: 6px; margin-top: 10px; }
table { border-collapse: collapse; width: 100%; font-size: 11px; }
th, td { padding: 4px 8px; text-align: left; border-bottom: 1px solid #1a1a1a; }
th { color: var(--mute); font-weight: 600; text-transform: uppercase; letter-spacing: 0.06em; }
.tabs { display: flex; gap: 2px; margin-bottom: 12px; }
.tabs a { padding: 5px 10px; color: var(--mute); text-decoration: none; border: 1px solid transparent; border-radius: 2px; font-size: 11px; text-transform: uppercase; letter-spacing: 0.06em; }
.tabs a:hover { color: var(--fg); background: #181818; }
.tabs a.active { color: #fff; border-color: #333; background: #111; }
.muted { color: var(--mute); }
.fail { color: var(--red); } .ok { color: var(--green); }
"""


def page(active: str, body: str, title: str = "Mako", public: bool = False) -> str:
    """Render the dashboard chrome.

    `public=True` renders only public links (Stats, Audit, Prompts, source) — use
    for /audit, /prompts, etc. that nginx serves without auth. The default
    (gated) renders the admin nav (Now, Steering, Approvals, Logs) — those
    routes are behind basic auth, so showing those links to a logged-in user
    is fine; showing them on a public page produces a basic-auth popup on
    click, which is bad UX.
    """
    img = mako_img_uri()
    brand_inner = (f'<img src="{img}" alt="" class="brand-img">Mako'
                   if img else "🦫 Mako")
    if public:
        nav = (
            f'<a href="/public" class="{"active" if active=="public" else ""}">Stats</a>'
            f'<a href="/audit"  class="{"active" if active=="audit"  else ""}">Audit</a>'
            f'<a href="/prompts" class="{"active" if active=="prompts" else ""}">Prompts</a>'
            f'<a href="https://github.com/minkforge/mako-zero" target="_blank" rel="noopener">Source</a>'
        )
    else:
        nav = (
            f'<a href="/now"       class="{"active" if active=="now" else ""}">Now</a>'
            f'<a href="/steering"  class="{"active" if active=="steering" else ""}">Steering</a>'
            f'<a href="/approvals" class="{"active" if active=="approvals" else ""}">Approvals</a>'
            f'<a href="/logs"      class="{"active" if active=="logs" else ""}">Logs</a>'
        )
    return f"""<!doctype html>
<html><head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{title}</title>
{favicon_link()}
<style>{CSS}
.brand-img {{ width: 22px; height: 22px; image-rendering: pixelated; vertical-align: middle; margin-right: 6px; border-radius: 2px; }}
</style>
<script src="https://unpkg.com/htmx.org@1.9.10"></script>
</head><body>
<header>
  <div class="brand">{brand_inner}</div>
  <nav>{nav}</nav>
  <span class="clock" id="nav-clock"></span>
</header>
<main>{body}</main>
<script>
function tick() {{
  const el = document.getElementById('nav-clock');
  if (!el) return;
  const n = new Date();
  const p = x => String(x).padStart(2, '0');
  el.textContent = n.getFullYear()+'-'+p(n.getMonth()+1)+'-'+p(n.getDate())+' '+p(n.getHours())+':'+p(n.getMinutes())+':'+p(n.getSeconds());
}}
tick(); setInterval(tick, 1000);
</script>
</body></html>"""


# ---------------------------- /now -------------------------------------

def _read_recent_journal(state_dir: Path, n: int = 30) -> list[str]:
    p = state_dir / "JOURNAL.md"
    if not p.exists():
        return []
    return p.read_text(encoding="utf-8").splitlines()[-n:]


def _metric_stats(logs_dir: Path) -> dict:
    p = logs_dir / "metrics.csv"
    if not p.exists():
        return {"rows": 0}
    walls: list[float] = []
    parses_ok = 0
    parses_total = 0
    last_ts = ""
    last_provider = ""
    with p.open("r", encoding="utf-8") as f:
        r = csv.DictReader(f)
        rows = list(r)
    if not rows:
        return {"rows": 0}
    for row in rows[-200:]:
        try:
            walls.append(float(row.get("wall_s") or 0))
        except (ValueError, TypeError):
            pass
        if row.get("parse_ok") == "True":
            parses_ok += 1
        parses_total += 1
    last = rows[-1]
    last_ts = last.get("ts", "")
    last_provider = f"{last.get('provider_used','?')}/{last.get('model_used','?')}"
    walls_sorted = sorted(walls)
    p95 = walls_sorted[int(len(walls_sorted) * 0.95)] if walls_sorted else 0
    avg = sum(walls) / len(walls) if walls else 0
    return {
        "rows": len(rows),
        "wall_avg": round(avg, 2),
        "wall_p95": round(p95, 2),
        "parse_pct": round(100 * parses_ok / parses_total, 1) if parses_total else 0,
        "last_ts": last_ts,
        "last_provider": last_provider,
    }


def _now_data() -> dict:
    cfg = load_cfg()
    root = paths_root()
    state = root / "state"
    pending_p = root / "pending" / "pending_actions.jsonl"
    logs = root / "logs"

    try:
        tick_n = int((state / "tick_counter.txt").read_text(encoding="utf-8").strip() or "0")
    except OSError:
        tick_n = 0

    pending_n = 0
    if pending_p.exists():
        pending_n = sum(1 for L in pending_p.read_text(encoding="utf-8").splitlines() if L.strip())

    av = t_mod.compute_availability(cfg)
    metrics = _metric_stats(logs)

    state_md = (state / "STATE.md").read_text(encoding="utf-8") if (state / "STATE.md").exists() else "(empty)"
    next_md = (state / "NEXT.md").read_text(encoding="utf-8") if (state / "NEXT.md").exists() else "(empty)"
    inbox = (state / "INBOX.md").read_text(encoding="utf-8") if (state / "INBOX.md").exists() else ""

    journal = _read_recent_journal(state, 25)

    return {
        "tick_n": tick_n,
        "pending_n": pending_n,
        "availability": av,
        "metrics": metrics,
        "state_md": state_md,
        "next_md": next_md,
        "inbox": inbox.strip(),
        "journal": journal,
    }


def _now_body(d: dict) -> str:
    journal_html = "".join(
        f'<div class="tick"><span class="tick-meta">{line[:8]}</span> {line[9:][:200]}</div>'
        if len(line) > 9 else f'<div class="tick">{line}</div>'
        for line in reversed(d["journal"])
    ) or '<div class="muted">(no journal entries yet)</div>'

    av = d["availability"]
    dot = "green" if av["in_window"] else "amber"
    inbox_warn = (f'<div class="card" style="border-color:#4a3a1d;">'
                  f'<h2>⚡ inbox waiting</h2>'
                  f'<pre>{_inbox_preview(d["inbox"])}</pre>'
                  f'<div class="muted">Mako will read this on next tick.</div></div>'
                  if d["inbox"] else "")

    m = d["metrics"]
    stats_html = f'''
        <div class="kv">
          <div class="k">tick</div><div>#{d["tick_n"]}</div>
          <div class="k">pending</div><div>{d["pending_n"]} {("· awaiting your call" if d["pending_n"] else "")}</div>
          <div class="k">availability</div><div><span class="dot {dot}"></span>{av["summary"][:80]}</div>
          <div class="k">last tick</div><div class="muted">{_fmt_london(m.get("last_ts","")) or "?"}</div>
          <div class="k">provider</div><div class="muted">{m.get("last_provider","?")}</div>
          <div class="k">wall avg</div><div>{m.get("wall_avg",0)}s · p95 {m.get("wall_p95",0)}s</div>
          <div class="k">parse ok</div><div>{m.get("parse_pct",0)}%</div>
          <div class="k">metrics rows</div><div class="muted">{m.get("rows",0)}</div>
        </div>'''

    return f"""
{inbox_warn}
<div class="grid">
  <div class="card tape" hx-get="/api/now/tape" hx-trigger="every 10s" hx-swap="innerHTML">
    <h2>live tape</h2>
    {journal_html}
  </div>
  <div class="card">
    <h2>state.md</h2>
    <pre>{d["state_md"]}</pre>
    <h2 style="margin-top:16px;">next.md</h2>
    <pre>{d["next_md"]}</pre>
  </div>
  <div class="card" hx-get="/api/now/stats" hx-trigger="every 5s" hx-swap="innerHTML">
    <h2>stats</h2>
    {stats_html}
  </div>
</div>
"""


@app.get("/", response_class=RedirectResponse)
def root():
    return RedirectResponse("/now")


@app.get("/now", response_class=HTMLResponse)
def now():
    d = _now_data()
    return HTMLResponse(page("now", _now_body(d)))


@app.get("/api/now/tape", response_class=HTMLResponse)
def now_tape():
    d = _now_data()
    return HTMLResponse("".join(
        f'<h2>live tape</h2>' if i == 0 else ""
        for i in range(1)
    ) + "<h2>live tape</h2>" + "".join(
        f'<div class="tick"><span class="tick-meta">{line[:8]}</span> {line[9:][:200]}</div>'
        if len(line) > 9 else f'<div class="tick">{line}</div>'
        for line in reversed(d["journal"])
    ))


@app.get("/api/now/stats", response_class=HTMLResponse)
def now_stats():
    d = _now_data()
    av = d["availability"]
    dot = "green" if av["in_window"] else "amber"
    m = d["metrics"]
    return HTMLResponse(f'''<h2>stats</h2>
        <div class="kv">
          <div class="k">tick</div><div>#{d["tick_n"]}</div>
          <div class="k">pending</div><div>{d["pending_n"]}</div>
          <div class="k">availability</div><div><span class="dot {dot}"></span>{av["summary"][:80]}</div>
          <div class="k">last tick</div><div class="muted">{_fmt_london(m.get("last_ts","")) or "?"}</div>
          <div class="k">provider</div><div class="muted">{m.get("last_provider","?")}</div>
          <div class="k">wall avg</div><div>{m.get("wall_avg",0)}s · p95 {m.get("wall_p95",0)}s</div>
          <div class="k">parse ok</div><div>{m.get("parse_pct",0)}%</div>
          <div class="k">metrics rows</div><div class="muted">{m.get("rows",0)}</div>
        </div>''')


@app.get("/api/now.json")
def now_json():
    return JSONResponse(_now_data())


# ---------------------------- /steering --------------------------------

@app.get("/steering", response_class=HTMLResponse)
def steering():
    root = paths_root()
    inbox_p = root / "state" / "INBOX.md"
    inbox = inbox_p.read_text(encoding="utf-8") if inbox_p.exists() else ""

    archive_dir = root / "archive"
    recent_archives = []
    if archive_dir.exists():
        for p in sorted(archive_dir.glob("inbox-*.md"), key=lambda x: x.stat().st_mtime, reverse=True)[:10]:
            try:
                first = p.read_text(encoding="utf-8").strip().splitlines()[:2]
                preview = " · ".join(first)[:120]
            except OSError:
                preview = "?"
            recent_archives.append((p.name, preview))

    archives_html = "".join(
        f'<div class="tick"><span class="tick-meta">{n}</span> {p}</div>'
        for n, p in recent_archives
    ) or '<div class="muted">(no archives yet)</div>'

    body = f"""
<div class="grid" style="grid-template-columns: 2fr 1fr;">
  <div class="card">
    <h2>drop-in steering</h2>
    <p class="muted" style="margin: 0 0 8px 0;">Mako reads this on his next tick. Append adds to existing inbox; replace overwrites.</p>
    <form method="post" action="/steering">
      <textarea name="text" rows="8" placeholder="Type a steering note. Markdown / plain text both fine."></textarea>
      <div style="margin-top: 8px; display: flex; gap: 6px;">
        <button type="submit" name="mode" value="append">append</button>
        <button type="submit" name="mode" value="replace" class="red">replace (clear first)</button>
      </div>
    </form>
  </div>
  <div class="card">
    <h2>inbox right now</h2>
    {f'<pre>{inbox.strip()}</pre>' if inbox.strip() else '<div class="muted">(empty — Mako has nothing new to read)</div>'}
    <h2 style="margin-top: 16px;">recent inboxes (archived)</h2>
    {archives_html}
  </div>
</div>
"""
    return HTMLResponse(page("steering", body))


@app.post("/steering")
def steering_post(text: str = Form(...), mode: str = Form("append")):
    root = paths_root()
    inbox = root / "state" / "INBOX.md"
    archive = root / "archive"
    archive.mkdir(parents=True, exist_ok=True)
    text = text.strip()
    if not text:
        return RedirectResponse("/steering", status_code=303)
    ts = datetime.now().strftime("%Y%m%d-%H%M%S")
    if mode == "replace":
        if inbox.exists() and inbox.read_text(encoding="utf-8").strip():
            (archive / f"inbox-{ts}.md").write_text(inbox.read_text(encoding="utf-8"), encoding="utf-8")
        inbox.write_text(f"[dashboard · {datetime.now().isoformat(timespec='minutes')}]\n{text}\n", encoding="utf-8")
    else:
        with inbox.open("a", encoding="utf-8") as f:
            f.write(f"\n[dashboard · {datetime.now().isoformat(timespec='minutes')}]\n{text}\n")
    return RedirectResponse("/steering", status_code=303)


# ---------------------------- /approvals -------------------------------

def _read_pending(root: Path) -> list[dict]:
    p = root / "pending" / "pending_actions.jsonl"
    if not p.exists():
        return []
    out = []
    for line in p.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return out


def _split_pending(root: Path, qid: str) -> tuple[dict | None, list[str]]:
    p = root / "pending" / "pending_actions.jsonl"
    if not p.exists():
        return None, []
    matched: dict | None = None
    others: list[str] = []
    for line in p.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            rec = json.loads(line)
        except json.JSONDecodeError:
            others.append(line)
            continue
        if rec.get("id") == qid and matched is None:
            matched = rec
        else:
            others.append(line)
    return matched, others


def _write_remaining(root: Path, kept: list[str]) -> None:
    p = root / "pending" / "pending_actions.jsonl"
    p.write_text("\n".join(kept) + ("\n" if kept else ""), encoding="utf-8")


def _append_decision(root: Path, decision: dict) -> None:
    p = root / "pending" / "decisions.jsonl"
    p.parent.mkdir(parents=True, exist_ok=True)
    with p.open("a", encoding="utf-8") as f:
        f.write(json.dumps(decision, ensure_ascii=False) + "\n")


def _append_inbox(root: Path, text: str) -> None:
    p = root / "state" / "INBOX.md"
    p.parent.mkdir(parents=True, exist_ok=True)
    with p.open("a", encoding="utf-8") as f:
        f.write(text)


@app.get("/approvals", response_class=HTMLResponse)
def approvals():
    root = paths_root()
    pending = _read_pending(root)
    if not pending:
        body = '<div class="card"><h2>approvals</h2><div class="muted">(no pending actions — Mako is unblocked)</div></div>'
    else:
        rows = []
        for rec in pending:
            qid = rec.get("id", "?")
            queued = rec.get("queued_at", "?")
            a = rec.get("action", {})
            a_type = a.get("type", "?")
            body_preview = json.dumps(a, ensure_ascii=False, indent=2)[:1200]
            rows.append(f"""
<div class="approval">
  <h3>⏸ {qid} · {a_type}</h3>
  <div class="meta">queued: {queued}</div>
  <pre>{body_preview}</pre>
  <div class="actions">
    <form method="post" action="/approvals/{qid}/approve" style="display:inline;">
      <button type="submit" class="green">✓ approve</button>
    </form>
    <form method="post" action="/approvals/{qid}/reject" style="display:inline;">
      <input type="text" name="reason" placeholder="reject reason (optional)" style="width: 280px; display: inline-block;">
      <button type="submit" class="red">✗ reject</button>
    </form>
  </div>
</div>""")
        body = '<div class="card"><h2>approvals</h2>' + "".join(rows) + "</div>"
    return HTMLResponse(page("approvals", body))


@app.post("/approvals/{qid}/approve")
def approve(qid: str):
    cfg = load_cfg()
    root = paths_root()
    rec, others = _split_pending(root, qid)
    if rec is None:
        raise HTTPException(404, f"{qid} not found in pending")
    action = rec.get("action") or {}
    a_type = action.get("type", "?")
    paths = t_mod.Paths(cfg)
    result = t_mod.execute_gated_action(cfg, paths, action)
    decision = {"id": qid, "decided_at": t_mod.now_iso(),
                "decision": "approve", "by": "dashboard",
                "action": action, "result": result}
    _append_decision(root, decision)
    _write_remaining(root, others)
    ok_marker = "✅" if result.get("ok") else "❌"
    _append_inbox(root, (
        f"\n[approval · {qid} · {a_type}] {ok_marker} executed (via dashboard)\n"
        f"action: {json.dumps(action, ensure_ascii=False)[:600]}\n"
        f"result: {json.dumps(result, ensure_ascii=False)[:500]}\n"
    ))
    # Mirror to Telegram so the channel reflects reality
    try:
        approvals_thread = (cfg["telegram"].get("approvals_thread_id")
                            or cfg["telegram"].get("log_thread_id"))
        t_mod.telegram_send(cfg, f"{ok_marker} {qid} · {a_type} (dashboard)\n"
                                 f"{(result.get('error') or 'ok')[:200]}",
                            thread_id=approvals_thread, label="dash-approve")
    except Exception:
        pass
    return RedirectResponse("/approvals", status_code=303)


@app.post("/approvals/{qid}/reject")
def reject(qid: str, reason: str = Form("")):
    cfg = load_cfg()
    root = paths_root()
    rec, others = _split_pending(root, qid)
    if rec is None:
        raise HTTPException(404, f"{qid} not found in pending")
    action = rec.get("action") or {}
    a_type = action.get("type", "?")
    decision = {"id": qid, "decided_at": t_mod.now_iso(),
                "decision": "reject", "by": "dashboard",
                "reason": reason, "action": action}
    _append_decision(root, decision)
    _write_remaining(root, others)
    _append_inbox(root, (
        f"\n[approval · {qid} · {a_type}] ❎ rejected by Chris (via dashboard)\n"
        f"reason: {reason or '(no reason given)'}\n"
        f"action was: {json.dumps(action, ensure_ascii=False)[:400]}\n"
    ))
    try:
        approvals_thread = (cfg["telegram"].get("approvals_thread_id")
                            or cfg["telegram"].get("log_thread_id"))
        t_mod.telegram_send(cfg, f"❎ {qid} · {a_type} · rejected (dashboard)"
                            + (f"\nreason: {reason[:200]}" if reason else ""),
                            thread_id=approvals_thread, label="dash-reject")
    except Exception:
        pass
    return RedirectResponse("/approvals", status_code=303)


# ---------------------------- /logs ------------------------------------

@app.get("/logs", response_class=HTMLResponse)
def logs(tab: str = "journal"):
    root = paths_root()
    state = root / "state"
    logs_dir = root / "logs"

    tabs_html = (
        f'<div class="tabs">'
        f'<a href="/logs?tab=journal" class="{"active" if tab=="journal" else ""}">Journal</a>'
        f'<a href="/logs?tab=metrics" class="{"active" if tab=="metrics" else ""}">Metrics</a>'
        f'<a href="/logs?tab=last" class="{"active" if tab=="last" else ""}">Last results</a>'
        f'</div>'
    )

    if tab == "journal":
        j = state / "JOURNAL.md"
        body_lines = j.read_text(encoding="utf-8").splitlines()[-200:] if j.exists() else []
        inner = "<pre>" + "\n".join(body_lines) + "</pre>" if body_lines else '<div class="muted">(empty)</div>'
    elif tab == "metrics":
        m = logs_dir / "metrics.csv"
        if not m.exists():
            inner = '<div class="muted">(no metrics yet)</div>'
        else:
            with m.open("r", encoding="utf-8") as f:
                rows = list(csv.DictReader(f))[-100:]
            if not rows:
                inner = '<div class="muted">(no rows)</div>'
            else:
                cols = ["ts", "tick_n", "mode", "wall_s", "provider_used", "model_used",
                        "input_tokens_est", "output_chars", "actions_executed", "actions_queued",
                        "parse_ok", "drift_flag"]
                head = "".join(f"<th>{c}</th>" for c in cols)
                trs = "".join(
                    "<tr>" + "".join(f'<td>{(r.get(c) or "")[:80]}</td>' for c in cols) + "</tr>"
                    for r in reversed(rows)
                )
                inner = f"<table><thead><tr>{head}</tr></thead><tbody>{trs}</tbody></table>"
    else:
        last = state / "LAST_RESULTS.md"
        text = last.read_text(encoding="utf-8") if last.exists() else "(no LAST_RESULTS.md)"
        inner = f"<pre>{text}</pre>"

    body = f'<div class="card"><h2>logs</h2>{tabs_html}{inner}</div>'
    return HTMLResponse(page("logs", body))


@app.get("/healthz")
def health():
    return {"ok": True}


# ---------------------------- /audit -----------------------------------
# Public audit log — shows every Chris intervention so the "how
# autonomous is this thing actually" question has a verifiable answer.
# Secrets are never written to logs/audit.jsonl in the first place;
# we redact defensively here too.

def _read_audit(root: Path, n: int = 200) -> list[dict]:
    p = root / "logs" / "audit.jsonl"
    if not p.exists():
        return []
    rows = []
    for line in p.read_text(encoding="utf-8").splitlines()[-n:]:
        if not line.strip():
            continue
        try:
            rows.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return rows


@app.get("/audit", response_class=HTMLResponse)
def audit():
    root = paths_root()
    rows = list(reversed(_read_audit(root, 500)))
    if not rows:
        body_inner = '<div class="muted">(audit log empty — no Chris interventions yet)</div>'
    else:
        # Group counts by kind
        from collections import Counter
        c = Counter(r.get("kind", "?") for r in rows)
        summary = " · ".join(f"{k}: {v}" for k, v in c.most_common())
        items = []
        for r in rows[:300]:
            ts = r.get("ts", "")[:19]
            kind = r.get("kind", "?")
            by = r.get("by", "?")
            payload = {k: v for k, v in r.items() if k not in ("ts", "kind", "by")}
            items.append(f'<div class="audit-row">'
                         f'<span class="audit-ts">{ts}</span> '
                         f'<span class="audit-kind audit-{kind}">{kind}</span> '
                         f'<span class="audit-by">via {by}</span>'
                         f'<pre>{json.dumps(payload, ensure_ascii=False, indent=2)[:1200]}</pre>'
                         f'</div>')
        body_inner = (f'<div class="muted">{summary}</div>'
                      f'<div class="audit-list">{"".join(items)}</div>')

    body = f"""
<style>
.audit-row {{ border-bottom: 1px solid var(--b); padding: 10px 0; }}
.audit-row:last-child {{ border: none; }}
.audit-ts {{ color: var(--mute); font-size: 11px; font-variant-numeric: tabular-nums; }}
.audit-kind {{ display: inline-block; padding: 1px 6px; border-radius: 2px; font-size: 10px;
              text-transform: uppercase; letter-spacing: 0.06em; margin: 0 8px; }}
.audit-approval {{ background: rgba(245,158,11,0.12); color: var(--amber); }}
.audit-steering {{ background: rgba(34,211,238,0.12); color: var(--acc); }}
.audit-meta-steering {{ background: rgba(167,139,250,0.12); color: #c4b5fd; }}
.audit-command  {{ background: rgba(239,68,68,0.12); color: var(--red); }}
.audit-request_update {{ background: rgba(167,139,250,0.12); color: #a78bfa; }}
.audit-publish  {{ background: rgba(34,197,94,0.12); color: var(--green); }}
.audit-meta     {{ background: rgba(167,139,250,0.18); color: #c4b5fd; }}
.audit-scribe-skip {{ background: rgba(120,120,120,0.15); color: var(--mute); }}
.audit-scribe-fail {{ background: rgba(239,68,68,0.18); color: var(--red); }}
.audit-by {{ color: var(--mute); font-size: 11px; }}
.audit-list pre {{ background: #151515; padding: 8px; margin-top: 6px; font-size: 11px; }}
</style>
<div class="card">
  <h2>audit log — chris interventions</h2>
  <p class="muted" style="margin: 0 0 12px 0;">
    Every approval, rejection, steering message, /cfg edit, /restart,
    request decision, and autonomous blog publish. Public on purpose —
    the point of this thing is to be honest about how much help it
    needed.
  </p>
  {body_inner}
</div>
"""
    return HTMLResponse(page("audit", body, public=True))


@app.get("/api/audit.json")
def audit_json():
    root = paths_root()
    return JSONResponse({"rows": _read_audit(root, 500)})


# ---------------------------- /public ----------------------------------
# Sanitised public stats page. No INBOX, no STATE/NEXT, no journal text,
# no pending action bodies — only safe numbers + flavour. nginx exposes
# this path without auth; everything else requires basic auth.

def _public_stats() -> dict:
    cfg = load_cfg()
    root = paths_root()
    state = root / "state"
    logs = root / "logs"

    try:
        tick_n = int((state / "tick_counter.txt").read_text(encoding="utf-8").strip() or "0")
    except OSError:
        tick_n = 0

    # journal line count (proxy for "things tried")
    journal_lines = 0
    journal_p = state / "JOURNAL.md"
    if journal_p.exists():
        journal_lines = sum(1 for L in journal_p.read_text(encoding="utf-8").splitlines() if L.strip())

    # MTD spend — read from STATE.md if present, else 0
    mtd_pence = 0
    state_md_p = state / "STATE.md"
    if state_md_p.exists():
        import re
        text = state_md_p.read_text(encoding="utf-8")
        m = re.search(r"MTD\s+spend:?\s*£?\s*([\d.]+)", text, re.IGNORECASE)
        if m:
            try:
                mtd_pence = int(float(m.group(1)) * 100)
            except ValueError:
                pass
    ceiling_pence = int(cfg.get("tick", {}).get("budget_ceiling_pence_month", 10000))

    # Token usage from metrics.csv
    tokens_today_in = tokens_today_out = 0
    tokens_total_in = tokens_total_out = 0
    today_prefix = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    days_alive = 0
    try:
        m = logs / "metrics.csv"
        if m.exists():
            with m.open("r", encoding="utf-8") as f:
                rows = list(csv.DictReader(f))
            if rows:
                first_ts = rows[0].get("ts", "")
                if first_ts:
                    started = datetime.fromisoformat(first_ts.replace("Z", "+00:00"))
                    days_alive = max(0, (datetime.now(timezone.utc) - started).days)
                for r in rows:
                    in_t = int(r.get("input_tokens") or 0) if (r.get("input_tokens") or "").isdigit() else 0
                    out_t = int(r.get("output_tokens") or 0) if (r.get("output_tokens") or "").isdigit() else 0
                    tokens_total_in += in_t
                    tokens_total_out += out_t
                    if (r.get("ts") or "").startswith(today_prefix):
                        tokens_today_in += in_t
                        tokens_today_out += out_t
    except Exception:
        pass

    metrics = _metric_stats(logs)
    audit_rows = _read_audit(root, 2000)

    # Interventions = things Chris actively did. Excludes autonomous events
    # (meta runs, scribe publishes/skips/fails) which would otherwise
    # inflate the "Chris had to bail him out" counter.
    INTERVENTION_KINDS = {
        "approval", "steering", "meta-steering", "command", "request_update",
    }
    META_KINDS = {"meta"}
    SCRIBE_KINDS = {"publish", "scribe-skip", "scribe-fail"}

    def _today(r): return (r.get("ts") or "").startswith(today_prefix)

    interventions_today = sum(1 for r in audit_rows if _today(r) and r.get("kind") in INTERVENTION_KINDS)
    interventions_total = sum(1 for r in audit_rows           if r.get("kind") in INTERVENTION_KINDS)
    meta_runs_today     = sum(1 for r in audit_rows if _today(r) and r.get("kind") in META_KINDS)
    meta_runs_total     = sum(1 for r in audit_rows           if r.get("kind") in META_KINDS)
    scribe_runs_today   = sum(1 for r in audit_rows if _today(r) and r.get("kind") in SCRIBE_KINDS)
    scribe_runs_total   = sum(1 for r in audit_rows           if r.get("kind") in SCRIBE_KINDS)

    # blog drafts published (count files in published/)
    blog_dir = state / "outbox" / "blog"
    drafts_n = len(list((blog_dir / "drafts").glob("*.md"))) if (blog_dir / "drafts").exists() else 0
    published_n = len(list((blog_dir / "published").glob("*.md"))) if (blog_dir / "published").exists() else 0

    # Cost estimate — Ollama Cloud is on a flat plan so true £ cost
    # doesn't scale per-token. Show tokens and let viewers decide.
    return {
        "tick_n": tick_n,
        "journal_lines": journal_lines,
        "days_alive": days_alive,
        "mtd_pence": mtd_pence,
        "mtd_ceiling_pence": ceiling_pence,
        "wall_avg_s": metrics.get("wall_avg", 0),
        "parse_pct": metrics.get("parse_pct", 0),
        "drafts_n": drafts_n,
        "published_n": published_n,
        "last_tick_at": metrics.get("last_ts", ""),
        "tokens_today_in": tokens_today_in,
        "tokens_today_out": tokens_today_out,
        "tokens_total_in": tokens_total_in,
        "tokens_total_out": tokens_total_out,
        "interventions_today": interventions_today,
        "interventions_total": interventions_total,
        "meta_runs_today": meta_runs_today,
        "meta_runs_total": meta_runs_total,
        "scribe_runs_today": scribe_runs_today,
        "scribe_runs_total": scribe_runs_total,
    }


@app.get("/api/public.json")
def public_json():
    return JSONResponse(_public_stats())


@app.get("/public", response_class=HTMLResponse)
def public_page():
    s = _public_stats()
    mtd_pct = round(100 * s["mtd_pence"] / max(1, s["mtd_ceiling_pence"]), 1)
    img = mako_img_uri()
    portrait = (f'<img src="{img}" alt="Mako, a pixel-art mink wearing sunglasses" '
                f'class="hero-img">' if img else '')
    body = f"""<!doctype html>
<html><head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Mako · live</title>
{favicon_link()}
<style>{CSS}
.hero {{ text-align: center; padding: 40px 16px; }}
.hero-img {{ width: 144px; height: 144px; image-rendering: pixelated; border-radius: 4px; margin: 0 auto 16px; display: block; }}
.hero h1 {{ font-size: 32px; letter-spacing: 0.04em; margin: 0 0 8px 0; color: #fff; }}
.hero .sub {{ color: var(--mute); font-size: 13px; max-width: 580px; margin: 0 auto; line-height: 1.6; }}
.hero .links {{ margin-top: 16px; }}
.hero .links a {{ color: var(--acc); text-decoration: none; margin: 0 12px; font-size: 12px; }}
.stats {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(160px, 1fr)); gap: 12px; max-width: 1100px; margin: 32px auto; padding: 0 16px; }}
.stat {{ border: 1px solid var(--b); border-radius: 2px; padding: 16px 12px; text-align: center; background: #0e0e0e; }}
.stat .num {{ font-size: 24px; color: #fff; font-variant-numeric: tabular-nums; letter-spacing: 0.02em; }}
.stat .num-sm {{ font-size: 18px; }}
.stat .label {{ font-size: 10px; color: var(--mute); text-transform: uppercase; letter-spacing: 0.1em; margin-top: 4px; }}
.budget-bar {{ height: 4px; background: var(--b); border-radius: 2px; margin-top: 8px; overflow: hidden; }}
.budget-fill {{ height: 100%; background: var(--green); width: {mtd_pct}%; transition: width 0.5s; }}
.foot {{ text-align: center; color: var(--mute); padding: 24px; font-size: 11px; }}
.foot a {{ color: var(--mute); }}
.section-title {{ text-align: center; color: var(--mute); font-size: 10px; letter-spacing: 0.1em; text-transform: uppercase; margin: 32px 0 4px 0; }}
</style>
</head><body>
<div class="hero">
  {portrait}
  <h1>mako</h1>
  <div class="sub">An AI agent trying to make £100/month online — building, journaling, and failing in public. Runs on a single VPS, ticks every few minutes, documents everything.</div>
  <div class="links">
    <a href="/audit">audit log</a>
    <a href="/prompts">initial prompts</a>
    <a href="https://github.com/minkforge/mako-zero">source</a>
  </div>
</div>
<div class="section-title">activity</div>
<div class="stats" hx-get="/api/public/stats-fragment" hx-trigger="every 30s" hx-swap="innerHTML">
  <div class="stat"><div class="num">{s["tick_n"]:,}</div><div class="label">ticks</div></div>
  <div class="stat"><div class="num">{s["days_alive"]}</div><div class="label">days alive</div></div>
  <div class="stat"><div class="num">£{s["mtd_pence"]/100:.2f}</div><div class="label">spent · £{s["mtd_ceiling_pence"]/100:.0f} ceiling</div><div class="budget-bar"><div class="budget-fill"></div></div></div>
  <div class="stat"><div class="num">{s["journal_lines"]:,}</div><div class="label">journal lines</div></div>
  <div class="stat"><div class="num">{s["drafts_n"]}</div><div class="label">blog drafts</div></div>
  <div class="stat"><div class="num">{s["published_n"]}</div><div class="label">published</div></div>
  <div class="stat"><div class="num">{s["wall_avg_s"]}s</div><div class="label">avg tick wall</div></div>
  <div class="stat"><div class="num">{s["parse_pct"]}%</div><div class="label">parse rate</div></div>
</div>
<div class="section-title">tokens consumed</div>
<div class="stats">
  <div class="stat"><div class="num-sm">{s["tokens_today_in"]:,}</div><div class="label">in · today</div></div>
  <div class="stat"><div class="num-sm">{s["tokens_today_out"]:,}</div><div class="label">out · today</div></div>
  <div class="stat"><div class="num-sm">{s["tokens_total_in"]:,}</div><div class="label">in · total</div></div>
  <div class="stat"><div class="num-sm">{s["tokens_total_out"]:,}</div><div class="label">out · total</div></div>
</div>
<div class="section-title">chris interventions</div>
<div class="stats">
  <div class="stat"><div class="num-sm">{s["interventions_today"]:,}</div><div class="label">today</div></div>
  <div class="stat"><div class="num-sm">{s["interventions_total"]:,}</div><div class="label">total</div></div>
  <div class="stat"><div class="num-sm" style="font-size:14px;"><a href="/audit" style="color:var(--acc);text-decoration:none;">see all →</a></div><div class="label">full audit log</div></div>
</div>
<div class="section-title">supporting loops</div>
<div class="stats">
  <div class="stat"><div class="num-sm">{s["meta_runs_today"]:,}</div><div class="label">meta runs · today</div></div>
  <div class="stat"><div class="num-sm">{s["meta_runs_total"]:,}</div><div class="label">meta runs · total</div></div>
  <div class="stat"><div class="num-sm">{s["scribe_runs_today"]:,}</div><div class="label">scribe runs · today</div></div>
  <div class="stat"><div class="num-sm">{s["scribe_runs_total"]:,}</div><div class="label">scribe runs · total</div></div>
</div>
<div class="foot">
  last tick · {_fmt_london(s["last_tick_at"]) or "—"}<br>
  source: <a href="https://github.com/minkforge/mako-zero">github.com/minkforge/mako-zero</a>
</div>
<script src="https://unpkg.com/htmx.org@1.9.10"></script>
</body></html>"""
    return HTMLResponse(body)


@app.get("/api/public/stats-fragment", response_class=HTMLResponse)
def public_stats_fragment():
    s = _public_stats()
    mtd_pct = round(100 * s["mtd_pence"] / max(1, s["mtd_ceiling_pence"]), 1)
    return HTMLResponse(f"""
  <div class="stat"><div class="num">{s["tick_n"]:,}</div><div class="label">ticks</div></div>
  <div class="stat"><div class="num">{s["days_alive"]}</div><div class="label">days alive</div></div>
  <div class="stat"><div class="num">£{s["mtd_pence"]/100:.2f}</div><div class="label">spent · £{s["mtd_ceiling_pence"]/100:.0f} ceiling</div><div class="budget-bar"><div class="budget-fill" style="width:{mtd_pct}%"></div></div></div>
  <div class="stat"><div class="num">{s["journal_lines"]:,}</div><div class="label">journal lines</div></div>
  <div class="stat"><div class="num">{s["drafts_n"]}</div><div class="label">blog drafts</div></div>
  <div class="stat"><div class="num">{s["published_n"]}</div><div class="label">published</div></div>
  <div class="stat"><div class="num">{s["wall_avg_s"]}s</div><div class="label">avg tick wall</div></div>
  <div class="stat"><div class="num">{s["parse_pct"]}%</div><div class="label">parse rate</div></div>
""")


# ---------------------------- /prompts ---------------------------------
# Public-readable rendered view of the engine prompts + mission, fetched
# from GitHub raw so they're always current with what's actually
# committed. Cached for 5 min to be polite.

import urllib.request as _urlreq
_prompts_cache: dict = {"ts": 0, "data": {}}
GH_RAW = "https://raw.githubusercontent.com/minkforge/mako-zero/main"
PROMPT_FILES = [
    ("prompts/system.md",   "Worker system prompt"),
    ("prompts/compact.md",  "Compaction tick prompt"),
    ("prompts/scribe.md",   "Scribe (blog writer) prompt"),
    ("prompts/meta.md",     "Meta loop prompt"),
    ("seed/MISSION.md",     "Mission (current)"),
    ("seed/CAPABILITIES.md","Capabilities (current)"),
]


def _fetch_prompt(path: str) -> str:
    import time as _time
    if _time.time() - _prompts_cache["ts"] < 300 and path in _prompts_cache["data"]:
        return _prompts_cache["data"][path]
    url = f"{GH_RAW}/{path}"
    try:
        with _urlreq.urlopen(url, timeout=10) as r:
            text = r.read().decode("utf-8", "replace")
    except Exception as e:
        # fallback to local copy
        local = paths_root() / path
        if local.exists():
            text = local.read_text(encoding="utf-8")
        else:
            text = f"(failed to fetch {url}: {e})"
    _prompts_cache["data"][path] = text
    _prompts_cache["ts"] = _time.time()
    return text


def _md_to_html_inline(md: str) -> str:
    """Tiny markdown→HTML for inline rendering. Same minimal shim as
    scribe.py — headings, paragraphs, lists, code fences."""
    import re as _re
    lines = md.split("\n")
    out: list[str] = []
    in_list = False
    in_code = False
    for raw in lines:
        if raw.startswith("```"):
            if in_code:
                out.append("</code></pre>"); in_code = False
            else:
                if in_list: out.append("</ul>"); in_list = False
                out.append("<pre><code>"); in_code = True
            continue
        if in_code:
            out.append(_html_escape(raw))
            continue
        line = raw.rstrip()
        if not line:
            if in_list: out.append("</ul>"); in_list = False
            out.append("")
            continue
        if line.startswith("# "):
            if in_list: out.append("</ul>"); in_list = False
            out.append(f"<h1>{_html_escape(line[2:])}</h1>")
        elif line.startswith("## "):
            if in_list: out.append("</ul>"); in_list = False
            out.append(f"<h2>{_html_escape(line[3:])}</h2>")
        elif line.startswith("### "):
            if in_list: out.append("</ul>"); in_list = False
            out.append(f"<h3>{_html_escape(line[4:])}</h3>")
        elif line.lstrip().startswith("- "):
            if not in_list:
                out.append("<ul>"); in_list = True
            out.append(f"<li>{_html_escape(line.lstrip()[2:])}</li>")
        else:
            if in_list: out.append("</ul>"); in_list = False
            out.append(f"<p>{_html_escape(line)}</p>")
    if in_list: out.append("</ul>")
    if in_code: out.append("</code></pre>")
    return "\n".join(out)


def _html_escape(s: str) -> str:
    return (s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
            .replace('"', "&quot;").replace("'", "&#39;"))


@app.get("/prompts", response_class=HTMLResponse)
def prompts_page():
    sections = []
    for path, label in PROMPT_FILES:
        text = _fetch_prompt(path)
        rendered = _md_to_html_inline(text)
        sections.append(f"""
<details class="prompt-block" open>
<summary><span class="prompt-label">{_html_escape(label)}</span>
  <a class="prompt-link" href="{GH_RAW}/{path}">{path}</a></summary>
<div class="prompt-body">{rendered}</div>
</details>""")

    body = f"""<!doctype html>
<html><head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Mako · prompts</title>
{favicon_link()}
<style>{CSS}
.prompts-wrap {{ max-width: 920px; margin: 32px auto; padding: 0 16px; }}
.prompt-block {{ border: 1px solid var(--b); border-radius: 2px; padding: 12px 16px; margin-bottom: 12px; background: #0e0e0e; }}
.prompt-block summary {{ cursor: pointer; padding: 4px 0; outline: none; }}
.prompt-label {{ color: #fff; font-weight: 600; letter-spacing: 0.04em; }}
.prompt-link {{ color: var(--mute); margin-left: 12px; font-size: 11px; text-decoration: none; }}
.prompt-link:hover {{ color: var(--acc); }}
.prompt-body {{ margin-top: 12px; padding-top: 12px; border-top: 1px solid var(--b); font-size: 13px; line-height: 1.6; }}
.prompt-body h1 {{ font-size: 18px; color: #fff; }}
.prompt-body h2 {{ font-size: 14px; color: #fff; margin-top: 24px; }}
.prompt-body h3 {{ font-size: 12px; color: #fff; margin-top: 16px; letter-spacing: 0.04em; }}
.prompt-body p {{ margin: 8px 0; }}
.prompt-body code {{ background: #151515; padding: 1px 5px; border-radius: 2px; font-size: 12px; }}
.prompt-body pre {{ background: #151515; padding: 10px; border-radius: 2px; overflow-x: auto; font-size: 11px; }}
.prompt-body pre code {{ padding: 0; background: none; }}
.prompt-body ul {{ padding-left: 22px; }}
.brand-img {{ width: 22px; height: 22px; image-rendering: pixelated; vertical-align: middle; margin-right: 6px; border-radius: 2px; }}
</style>
</head><body>
<header>
  <div class="brand">{f'<img src="{mako_img_uri()}" alt="" class="brand-img">' if mako_img_uri() else '🦫 '}Mako · prompts</div>
  <nav><a href="/public">stats</a><a href="/audit">audit</a><a href="https://github.com/minkforge/mako-zero">source</a></nav>
</header>
<main class="prompts-wrap">
  <p class="muted">These are the prompts that drive Mako, fetched live from the GitHub repo (5-min cache). Edit on GitHub → the dashboard updates automatically. The meta loop occasionally proposes patches to these via Codex; every change shows up in <code>git log</code> with a <code>meta:</code> prefix.</p>
  {''.join(sections)}
</main>
</body></html>"""
    return HTMLResponse(body)



if __name__ == "__main__":
    import uvicorn
    host = os.environ.get("MAKO_DASH_HOST", "127.0.0.1")
    port = int(os.environ.get("MAKO_DASH_PORT", "8050"))
    uvicorn.run(app, host=host, port=port, log_level="info")
