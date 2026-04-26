#!/usr/bin/env python3
"""mako-zero scribe — separate from the worker tick.

Runs every ~30 min (configurable). Reads the journal + persona +
recent notes, decides whether there's a blog-worthy arc, and either:
 - drafts a post into state/outbox/blog/drafts/<date>-<slug>.md, or
 - explicitly skips with a reason.

Posts a single Telegram ping per run (approval thread on draft, log
thread on skip). Writes a full request/response log to logs/scribe/
when full_payload logging is enabled.

The scribe never runs actions, never modifies worker state, and never
publishes — Chris approves the draft separately.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import time
import traceback
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))
import tick as t  # noqa: E402  reuse helpers


SLUG_OK = re.compile(r"[^a-z0-9-]+")


def slugify(s: str) -> str:
    s = (s or "post").strip().lower().replace("_", "-").replace(" ", "-")
    s = SLUG_OK.sub("-", s).strip("-")
    return (s or "post")[:60]


def bump_scribe_counter(paths: t.Paths) -> int:
    p = paths.state / "scribe_counter.txt"
    n = int(t.read_text(p, "0") or "0") + 1
    t.write_text(p, str(n))
    return n


def recent_notes(paths: t.Paths, n: int = 3) -> list[Path]:
    if not paths.notes.exists():
        return []
    files = [p for p in paths.notes.glob("*.md") if p.name != "INDEX.md"]
    files.sort(key=lambda p: p.stat().st_mtime, reverse=True)
    return files[:n]


def assemble_scribe_context(cfg: dict, paths: t.Paths) -> tuple[str, dict]:
    parts: list[tuple[str, str]] = []
    sizes: dict[str, int] = {}

    def add(label: str, content: str) -> None:
        parts.append((label, content))
        sizes[label] = t.est_tokens(content)

    add("MISSION.md", t.read_text(paths.state / "MISSION.md"))
    add("PERSONA.md", t.read_text(paths.state / "PERSONA.md"))

    journal_lines = int(cfg.get("scribe", {}).get("recent_journal_lines", 100))
    add(f"JOURNAL.md (last {journal_lines} lines)",
        t.tail_lines(paths.state / "JOURNAL.md", journal_lines))

    add("notes/INDEX.md", t.read_text(paths.notes / "INDEX.md"))

    for nf in recent_notes(paths, 3):
        add(f"notes/{nf.name}", t.read_text(nf))

    add("existing drafts in outbox/blog/drafts/", t.list_drafts(paths))

    blocks = [f"## {label}\n\n{content.rstrip()}\n" for label, content in parts]
    text = "\n".join(blocks)
    sizes["_total_tokens_est"] = t.est_tokens(text)
    return text, sizes


def approval_thread_id(cfg: dict) -> int | None:
    name = cfg.get("scribe", {}).get("approval_thread", "approvals")
    tg = cfg["telegram"]
    if name == "approvals":
        return tg.get("approvals_thread_id") or tg.get("requests_thread_id") or tg.get("log_thread_id")
    if name == "requests":
        return tg.get("requests_thread_id") or tg.get("log_thread_id")
    return tg.get("log_thread_id")


def write_draft(paths: t.Paths, slug: str, title: str, body_md: str) -> Path:
    drafts = paths.state / "outbox" / "blog" / "drafts"
    drafts.mkdir(parents=True, exist_ok=True)
    today = datetime.now().strftime("%Y-%m-%d")
    fname = f"{today}-{slugify(slug)}.md"
    out = drafts / fname
    # If a draft with this slug exists, suffix with the scribe run number
    if out.exists():
        out = drafts / f"{today}-{slugify(slug)}-{int(time.time())}.md"
    body = (
        f"---\n"
        f"title: \"{title.replace(chr(34), chr(39))}\"\n"
        f"date: {datetime.now(timezone.utc).isoformat(timespec='seconds')}\n"
        f"status: draft\n"
        f"---\n\n"
        f"{body_md.rstrip()}\n"
    )
    out.write_text(body, encoding="utf-8")
    return out


def published_today_count(paths: t.Paths) -> int:
    """Number of posts published in the current UTC day."""
    pub = paths.state / "outbox" / "blog" / "published"
    if not pub.exists():
        return 0
    today_prefix = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    return sum(1 for p in pub.glob(f"{today_prefix}-*.md"))


def autopublish_draft(paths: t.Paths, draft_path: Path, target_dir: str) -> dict:
    """Move a draft to outbox/blog/published/ AND copy to a public web
    directory. Returns a result dict for journaling.

    target_dir defaults to /var/www/html/blog/ (served by nginx). Copies a
    rendered HTML file using a tiny markdown→html shim. The .md goes into
    published/ for record-keeping.
    """
    import shutil
    pub = paths.state / "outbox" / "blog" / "published"
    pub.mkdir(parents=True, exist_ok=True)
    dest_md = pub / draft_path.name
    shutil.move(str(draft_path), str(dest_md))

    # Render to HTML via a minimal shim (no extra deps). Real apps would
    # use a static-site generator, but keep V0 simple — Chris/Mako can
    # upgrade later.
    md = dest_md.read_text(encoding="utf-8")
    html = _md_to_html(md, dest_md.stem)
    web_dir = Path(target_dir)
    try:
        web_dir.mkdir(parents=True, exist_ok=True)
        (web_dir / f"{dest_md.stem}.html").write_text(html, encoding="utf-8")
        web_ok = True
        web_err = ""
    except Exception as e:
        web_ok = False
        web_err = f"{type(e).__name__}: {e}"
    return {"ok": True, "moved_to": str(dest_md.relative_to(paths.root)),
            "web_ok": web_ok, "web_dir": target_dir, "web_err": web_err}


def _md_to_html(md_text: str, slug: str) -> str:
    """Minimal markdown→html. Keeps the YAML front-matter as <details>,
    converts # ## ### headings, paragraphs, and bullet lists. Good enough
    for the V0 blog; swap for a real renderer later."""
    import re as _re
    # Strip front-matter
    title = slug
    body = md_text
    fm = _re.match(r"^---\n(.*?)\n---\n", md_text, _re.DOTALL)
    if fm:
        for line in fm.group(1).splitlines():
            if line.startswith("title:"):
                title = line.split(":", 1)[1].strip().strip('"').strip("'")[:140]
                break
        body = md_text[fm.end():]
    # Very simple: paragraphs are blank-line-separated; ### / ## / # are
    # headings; lines starting with `- ` are bullets.
    out: list[str] = []
    in_list = False
    for raw in body.split("\n"):
        line = raw.rstrip()
        if not line:
            if in_list:
                out.append("</ul>"); in_list = False
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
        elif line.startswith("- "):
            if not in_list:
                out.append("<ul>"); in_list = True
            out.append(f"<li>{_html_escape(line[2:])}</li>")
        else:
            if in_list: out.append("</ul>"); in_list = False
            out.append(f"<p>{_html_escape(line)}</p>")
    if in_list:
        out.append("</ul>")
    return f"""<!doctype html>
<html><head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{_html_escape(title)} — minkforge</title>
<style>
:root {{ color-scheme: dark; }}
body {{ background:#0b0b0b; color:#d6d6d6; font-family: ui-monospace, 'JetBrains Mono', monospace; max-width: 720px; margin: 40px auto; padding: 0 16px; line-height: 1.6; font-size: 14px; }}
h1, h2, h3 {{ color:#fff; line-height: 1.25; }}
h1 {{ font-size: 26px; }}
h2 {{ font-size: 18px; margin-top: 32px; }}
h3 {{ font-size: 15px; margin-top: 24px; }}
p {{ margin: 12px 0; }}
ul {{ padding-left: 22px; }}
li {{ margin: 6px 0; }}
a {{ color:#22d3ee; }}
.meta {{ color:#888; font-size: 11px; text-transform: uppercase; letter-spacing: 0.08em; margin-bottom: 24px; }}
.foot {{ margin-top: 60px; padding-top: 16px; border-top: 1px solid #222; color:#888; font-size: 11px; }}
</style>
</head><body>
<div class="meta">🦫 mako · minkforge.com</div>
{chr(10).join(out)}
<div class="foot">written by an AI agent. <a href="https://dash.minkforge.com/public">live stats</a> · <a href="https://github.com/minkforge/mako-zero">source</a></div>
</body></html>"""


def _html_escape(s: str) -> str:
    return (s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
            .replace('"', "&quot;").replace("'", "&#39;"))


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", required=True)
    args = ap.parse_args()

    cfg = t.load_config(args.config)
    paths = t.Paths(cfg)
    paths.ensure()

    if not cfg.get("scribe", {}).get("enabled", True):
        return 0

    run_n = bump_scribe_counter(paths)
    t_start = time.time()

    full_log: dict[str, Any] = {
        "run_n": run_n,
        "kind": "scribe",
        "started_at": t.now_iso(),
        "system_prompt": None,
        "user_message": None,
        "sizes": None,
        "llm_attempts": [],
        "raw_content": None,
        "parsed_response": None,
        "draft_path": None,
        "telegram_posts": [],
        "errors": [],
    }

    try:
        user_msg, sizes = assemble_scribe_context(cfg, paths)
        full_log["user_message"] = user_msg
        full_log["sizes"] = sizes

        max_in = cfg["context"]["max_input_tokens"]
        if sizes["_total_tokens_est"] > max_in:
            user_msg = t.truncate(user_msg, max_in * 4)
            sizes["_truncated"] = True

        system = t.read_text(paths.prompts / "scribe.md")
        if not system:
            raise RuntimeError("missing prompts/scribe.md")
        full_log["system_prompt"] = system

        content, meta, prov_failures, attempts = t.call_llm_with_fallback(cfg, system, user_msg)
        full_log["llm_attempts"] = attempts
        full_log["raw_content"] = content

        obj = t.extract_json(content)
        if obj is None:
            raise RuntimeError("PARSE_ERROR: no JSON object in scribe response")
        full_log["parsed_response"] = obj

        kind = obj.get("kind")
        wall = round(time.time() - t_start, 2)
        slot = meta.get("slot", "?")
        model = meta.get("model", "?")

        if kind == "draft" and isinstance(obj.get("draft"), dict):
            draft = obj["draft"]
            slug = draft.get("slug") or "untitled"
            title = (draft.get("title") or slug)[:140]
            body_md = draft.get("body_md") or ""
            if not body_md.strip():
                raise RuntimeError("scribe returned draft with empty body_md")
            out_path = write_draft(paths, slug, title, body_md)
            full_log["draft_path"] = str(out_path.relative_to(paths.root))
            summary = (draft.get("summary_for_chris") or title)[:300]

            # Auto-publish — Chris asked for autonomy here. Hard-cap at
            # `scribe.daily_publish_cap` (default 2) per UTC day.
            cap = int(cfg.get("scribe", {}).get("daily_publish_cap", 2))
            already = published_today_count(paths)
            web_dir = cfg.get("scribe", {}).get("publish_dir", "/var/www/html/blog")
            if already >= cap:
                publish_res = {"ok": False, "skipped": True,
                               "reason": f"daily cap reached ({already}/{cap})"}
                full_log["publish"] = publish_res
                text = (f"📝 scribe #{run_n} · {wall}s · {slot}/{model}\n"
                        f"draft kept (cap {already}/{cap} reached today): {out_path.relative_to(paths.root)}\n"
                        f"title: {title}")
            else:
                publish_res = autopublish_draft(paths, out_path, web_dir)
                full_log["publish"] = publish_res
                text = (f"📝 scribe #{run_n} · {wall}s · {slot}/{model}\n"
                        f"published: {publish_res.get('moved_to')}\n"
                        f"web: {web_dir} ({'ok' if publish_res.get('web_ok') else 'FAILED — ' + publish_res.get('web_err','?')})\n"
                        f"title: {title}\n"
                        f"summary: {summary}\n"
                        f"daily count: {already + 1}/{cap}")
                # Audit the autonomous publish.
                try:
                    aud = paths.logs / "audit.jsonl"
                    aud.parent.mkdir(parents=True, exist_ok=True)
                    with aud.open("a", encoding="utf-8") as f:
                        f.write(json.dumps({
                            "ts": t.now_iso(), "kind": "publish", "by": "scribe",
                            "title": title, "slug": slug,
                            "moved_to": publish_res.get("moved_to"),
                            "web_ok": publish_res.get("web_ok"),
                        }, ensure_ascii=False) + "\n")
                except Exception:
                    pass
            res = t.telegram_send(cfg, text, thread_id=approval_thread_id(cfg), label="scribe-publish")
            full_log["telegram_posts"].append({"text": text, "thread_id": approval_thread_id(cfg), "result": res})

        elif kind == "skip":
            reason = (obj.get("skip") or {}).get("reason", "no arc yet") if isinstance(obj.get("skip"), dict) else (obj.get("skip") or "no arc yet")
            text = f"📝 scribe #{run_n} · {wall}s · {slot}/{model} · skipped: {str(reason)[:280]}"
            res = t.telegram_send(cfg, text, thread_id=cfg["telegram"]["log_thread_id"], label="scribe-skip")
            full_log["telegram_posts"].append({"text": text, "thread_id": cfg["telegram"]["log_thread_id"], "result": res})
        else:
            raise RuntimeError(f"scribe: unexpected kind {kind!r}")

    except Exception as e:
        tb = traceback.format_exc()
        t.write_text(paths.logs / f"scribe-error-{run_n}.log",
                     f"{t.now_iso()}\n{tb}\n")
        full_log["errors"].append({"type": type(e).__name__, "message": str(e), "traceback": tb})
        try:
            wall = round(time.time() - t_start, 2)
            text = f"📝 scribe #{run_n} FAIL · {wall}s · {type(e).__name__}: {str(e)[:200]}"
            t.telegram_send(cfg, text, thread_id=cfg["telegram"]["log_thread_id"], label="scribe-fail")
        except Exception:
            pass
        if cfg.get("logging", {}).get("full_payload", True):
            full_log["ended_at"] = t.now_iso()
            full_log["wall_s"] = round(time.time() - t_start, 2)
            full_log["outcome"] = "fail"
            t.write_full_log(paths, "scribe", run_n, full_log)
        return 1

    if cfg.get("logging", {}).get("full_payload", True):
        full_log["ended_at"] = t.now_iso()
        full_log["wall_s"] = round(time.time() - t_start, 2)
        full_log["outcome"] = "ok"
        t.write_full_log(paths, "scribe", run_n, full_log)
    return 0


if __name__ == "__main__":
    sys.exit(main())
