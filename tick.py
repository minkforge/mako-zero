#!/usr/bin/env python3
"""mako-zero — one cron tick of the autonomous agent loop.

Reads hot context, calls the LLM (with fallback), parses the structured
response, applies file writes, executes non-gated actions, queues gated
ones, writes LAST_RESULTS.md, posts a Telegram update, records metrics.
"""
from __future__ import annotations

import argparse
import csv
import json
import os
import re
import shlex
import subprocess
import sys
import time
import traceback
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import requests
import yaml


# ----------------------------- helpers ---------------------------------

def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def est_tokens(s: str) -> int:
    return max(1, len(s) // 4)


def truncate(s: str, max_bytes: int) -> str:
    b = s.encode("utf-8", "replace")
    if len(b) <= max_bytes:
        return s
    return b[:max_bytes].decode("utf-8", "ignore") + f"\n…[truncated, {len(b)-max_bytes} bytes]"


def read_text(p: Path, default: str = "") -> str:
    try:
        return p.read_text(encoding="utf-8")
    except FileNotFoundError:
        return default


def append_text(p: Path, s: str) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    with p.open("a", encoding="utf-8") as f:
        f.write(s)


def write_text(p: Path, s: str) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(s, encoding="utf-8")


def tail_lines(p: Path, n: int) -> str:
    try:
        lines = p.read_text(encoding="utf-8").splitlines()
    except FileNotFoundError:
        return ""
    return "\n".join(lines[-n:])


# ----------------------------- config ----------------------------------

def load_config(path: str) -> dict:
    with open(path, "r", encoding="utf-8") as f:
        return yaml.safe_load(f)


# ----------------------------- paths -----------------------------------

class Paths:
    def __init__(self, cfg: dict):
        p = cfg["paths"]
        self.root = Path(p["root"])
        self.state = Path(p["state"])
        self.notes = Path(p["notes"])
        self.workdir = Path(p["workdir"])
        self.archive = Path(p["archive"])
        self.logs = Path(p["logs"])
        self.pending = Path(p["pending"])
        self.prompts = Path(p["prompts"])

    def ensure(self) -> None:
        for d in (self.state, self.notes, self.workdir, self.archive, self.logs, self.pending):
            d.mkdir(parents=True, exist_ok=True)


# ------------------------- path safety --------------------------------

def resolve_under(root: Path, rel_or_abs: str) -> Path | None:
    """Resolve a user-supplied path, ensuring it stays under root.
    Returns None if it escapes."""
    p = Path(rel_or_abs)
    if not p.is_absolute():
        p = root / p
    try:
        p_resolved = p.resolve()
        root_resolved = root.resolve()
    except (OSError, RuntimeError):
        return None
    try:
        p_resolved.relative_to(root_resolved)
    except ValueError:
        return None
    return p_resolved


def is_writable(cfg: dict, paths: Paths, target: Path) -> bool:
    """True iff target is under one of the writable_paths and not under any forbidden one."""
    rel: str
    try:
        rel = str(target.relative_to(paths.root))
    except ValueError:
        return False
    rel_top = rel.split(os.sep, 1)[0] if rel else ""
    writable = set(cfg["guardrails"]["writable_paths"])
    if rel_top not in writable:
        return False
    forbidden = cfg["guardrails"]["forbidden_paths"]
    for f in forbidden:
        if rel == f or rel.startswith(f + os.sep) or rel_top == f:
            return False
    return True


# --------------------------- context -----------------------------------

def snapshot_inbox(paths: Paths) -> dict | None:
    p = paths.state / "INBOX.md"
    if not p.exists():
        return None
    content = p.read_text(encoding="utf-8")
    if not content.strip():
        return None
    return {"content": content, "mtime": p.stat().st_mtime}


def archive_inbox(paths: Paths, snap: dict | None) -> None:
    if snap is None:
        return
    ts = datetime.now().strftime("%Y%m%d-%H%M%S")
    write_text(paths.archive / f"inbox-{ts}.md", snap["content"])
    p = paths.state / "INBOX.md"
    if not p.exists():
        return
    try:
        if abs(p.stat().st_mtime - snap["mtime"]) < 0.01:
            write_text(p, "")  # safe to clear, unchanged since snapshot
    except OSError:
        pass


def list_drafts(paths: Paths, limit: int = 12) -> str:
    """Summary of state/outbox/blog/drafts/ for hot context — filename + first heading."""
    drafts_dir = paths.state / "outbox" / "blog" / "drafts"
    if not drafts_dir.exists():
        return "(no drafts yet — scribe hasn't written anything)"
    files = sorted(drafts_dir.glob("*.md"), key=lambda p: p.stat().st_mtime, reverse=True)[:limit]
    if not files:
        return "(no drafts yet — scribe hasn't written anything)"
    lines = []
    for p in files:
        head = ""
        try:
            for line in p.read_text(encoding="utf-8").splitlines():
                s = line.strip()
                if s.startswith("---") or not s:
                    continue
                if s.startswith("title:"):
                    head = s.split(":", 1)[1].strip().strip('"').strip("'")[:80]
                    break
                if s.startswith("#"):
                    head = s.lstrip("#").strip()[:80]
                    break
        except OSError:
            pass
        lines.append(f"- {p.name}{(' — ' + head) if head else ''}")
    return "\n".join(lines)


def assemble_hot_context(cfg: dict, paths: Paths, requested_notes: list[str],
                         inbox_snap: dict | None) -> tuple[str, dict]:
    """Build the user-message hot context block. Returns (text, sizes)."""
    parts: list[tuple[str, str]] = []
    sizes: dict[str, int] = {}

    def add(label: str, content: str) -> None:
        parts.append((label, content))
        sizes[label] = est_tokens(content)

    # TIME — first thing Mako sees so he never miscounts.
    now_utc = datetime.now(timezone.utc)
    days_alive, ticks_alive, first_tick_iso = compute_alive_stats(paths)
    av = compute_availability(cfg)
    try:
        from zoneinfo import ZoneInfo
        local = now_utc.astimezone(ZoneInfo(av.get("tz", "Europe/London")))
        local_str = local.strftime("%Y-%m-%d %H:%M %Z")
    except Exception:
        local_str = now_utc.isoformat(timespec="minutes")
    add("TIME",
        f"now_utc: {now_utc.isoformat(timespec='seconds')}\n"
        f"now_local: {local_str}\n"
        f"days_alive: {days_alive}\n"
        f"ticks_alive: {ticks_alive}\n"
        f"first_tick_at: {first_tick_iso or '(this is your first tick)'}")
    add("AVAILABILITY (Chris's working window)",
        f"in_window: {av['in_window']}\n"
        f"sla_min: {av['sla_min']}\n"
        f"tz: {av['tz']}\n"
        f"summary: {av['summary']}")
    add("MISSION.md", read_text(paths.state / "MISSION.md"))
    if inbox_snap and inbox_snap.get("content", "").strip():
        add("⚡ INBOX FROM CHRIS — read carefully; wrapper will archive after this tick",
            inbox_snap["content"])
    add("CAPABILITIES.md", read_text(paths.state / "CAPABILITIES.md"))
    add("STATE.md", read_text(paths.state / "STATE.md"))
    add("NEXT.md", read_text(paths.state / "NEXT.md"))
    # OPEN REQUESTS — Mako sees what he's already asked Chris for, so he
    # doesn't re-emit duplicates and doesn't keep mentioning blockers.
    add("OPEN REQUESTS (don't re-emit duplicates; park if blocked, work on something else)",
        summarise_open_requests(paths))
    # BLOCKED — count only; details parked in notes/blocked.md (NOT loaded
    # every tick to stop Mako looping on stuck items).
    add("BLOCKED (parked items — don't keep checking; resume when Chris signals an unblock)",
        summarise_blocked(paths))
    # BACKLOG — count + top 3 only. Full list in notes/backlog.md.
    add("BACKLOG (your idea pipeline — see notes/backlog.md for the full list)",
        summarise_backlog(paths))
    add("JOURNAL.md (last %d lines)" % cfg["context"]["recent_journal_lines"],
        tail_lines(paths.state / "JOURNAL.md", cfg["context"]["recent_journal_lines"]))
    add("notes/INDEX.md", read_text(paths.notes / "INDEX.md"))
    add("outbox/blog/drafts/ (scribe's drafts; published autonomously by scribe, max 2/day)",
        list_drafts(paths))
    add("LAST_RESULTS.md", read_text(paths.state / "LAST_RESULTS.md"))
    add("PERSONA.md", read_text(paths.state / "PERSONA.md"))

    # requested notes
    max_notes = cfg["context"]["max_requested_notes"]
    loaded_notes: list[str] = []
    for rn in (requested_notes or [])[:max_notes]:
        target = resolve_under(paths.notes, Path(rn).name if "/" not in rn else rn.replace("notes/", "", 1))
        if target is None or not target.exists():
            continue
        add(f"notes/{target.name}", read_text(target))
        loaded_notes.append(target.name)

    blocks = [f"## {label}\n\n{content.rstrip()}\n" for label, content in parts]
    text = "\n".join(blocks)

    sizes["_total_tokens_est"] = est_tokens(text)
    sizes["_loaded_notes"] = loaded_notes
    return text, sizes


def compute_alive_stats(paths: Paths) -> tuple[int, int, str]:
    """Returns (days_alive, ticks_alive, first_tick_iso). Reads the first
    metrics row to discover when this Mako instance was born; if metrics is
    empty, returns (0, 0, '') — first tick. Reset wipes metrics, so this
    naturally re-zeros on a fresh start."""
    metrics = paths.logs / "metrics.csv"
    if not metrics.exists():
        return 0, 0, ""
    try:
        with metrics.open("r", encoding="utf-8") as f:
            first_line = f.readline()  # header
            second = f.readline()       # first data row
            if not second:
                return 0, 0, ""
            first_ts = second.split(",", 1)[0]
            born = datetime.fromisoformat(first_ts.replace("Z", "+00:00"))
            now = datetime.now(timezone.utc)
            days = max(0, (now - born).days)
            # ticks = lines in file minus header
            f.seek(0)
            n = sum(1 for _ in f) - 1
            return days, max(0, n), first_ts
    except Exception:
        return 0, 0, ""


def summarise_open_requests(paths: Paths) -> str:
    """Summarise pending/resources.jsonl + open ask_chris questions for hot context."""
    p = paths.pending / "resources.jsonl"
    if not p.exists():
        return "(no open resource requests)"
    try:
        rows = [json.loads(L) for L in p.read_text(encoding="utf-8").splitlines() if L.strip()]
    except Exception:
        return "(resources.jsonl unreadable)"
    open_rows = [r for r in rows if r.get("status") in (None, "open", "pending")]
    if not open_rows:
        return "(no open resource requests)"
    lines = [f"{len(open_rows)} open — don't re-emit. Wait for Chris's reply via INBOX:"]
    for r in open_rows[-10:]:
        rid = r.get("id", "?")
        ask = (r.get("action") or {}).get("ask", "?")[:80]
        cat = (r.get("action") or {}).get("category", "?")
        queued = r.get("queued_at", "?")[:19]
        lines.append(f"- {rid} · {cat} · {ask} (queued {queued})")
    return "\n".join(lines)


def summarise_blocked(paths: Paths) -> str:
    """Count-only summary of notes/blocked.md. Full text NOT in hot context."""
    p = paths.notes / "blocked.md"
    if not p.exists():
        return "(none parked)"
    lines = [L for L in p.read_text(encoding="utf-8").splitlines() if L.strip().startswith("- ")]
    if not lines:
        return "(none parked)"
    return (f"{len(lines)} item(s) parked in notes/blocked.md. "
            f"Don't bring them up unless something has changed. "
            f"If you want to unpark something, write_file blocked.md to remove it.")


def summarise_backlog(paths: Paths) -> str:
    """Count + top 3 from notes/backlog.md."""
    p = paths.notes / "backlog.md"
    if not p.exists():
        return "(empty — see backlog mode in your prompt)"
    lines = [L for L in p.read_text(encoding="utf-8").splitlines() if L.strip().startswith("- ")]
    if not lines:
        return "(empty — see backlog mode in your prompt)"
    head = lines[:3]
    return f"{len(lines)} item(s). Top 3:\n" + "\n".join(head)


def read_pending_note_requests(paths: Paths) -> list[str]:
    p = paths.state / "next_notes.json"
    if not p.exists():
        return []
    try:
        return json.loads(p.read_text(encoding="utf-8")) or []
    except Exception:
        return []


def write_pending_note_requests(paths: Paths, names: list[str]) -> None:
    write_text(paths.state / "next_notes.json", json.dumps(names))


# --------------------------- providers ---------------------------------

class ProviderError(RuntimeError):
    pass


def _redact(headers: dict) -> dict:
    """Strip secrets from header dict for logging."""
    out = {}
    for k, v in headers.items():
        if k.lower() in ("authorization", "x-api-key", "api-key"):
            out[k] = "[REDACTED]"
        else:
            out[k] = v
    return out


def call_ollama_chat(spec: dict, messages: list[dict], tools: list[dict] | None = None) -> tuple[dict, dict]:
    """Call Ollama Cloud /api/chat with a full messages list and optional tools.

    Returns (assistant_message, meta). assistant_message is the full message
    dict from the response (role/content/thinking/tool_calls). For
    backward-compat with single-shot callers, callers can pull .content out
    of the returned message; tool callers should look at .tool_calls."""
    base = spec["base_url"].rstrip("/")
    url = f"{base}/api/chat"
    headers = {"Content-Type": "application/json"}
    if spec.get("api_key"):
        headers["Authorization"] = f"Bearer {spec['api_key']}"
    body: dict = {
        "model": spec["model"],
        "messages": messages,
        "stream": False,
        "options": {
            "num_predict": spec.get("num_predict", 8000),
        },
    }
    if tools:
        body["tools"] = tools
    t0 = time.time()
    r = requests.post(url, headers=headers, json=body, timeout=spec["timeout_s"])
    dt = time.time() - t0
    raw_text = r.text
    if r.status_code != 200:
        meta = {"provider": "ollama", "model": spec["model"], "wall_s": round(dt, 2),
                "url": url, "request_headers": _redact(headers), "request_body": body,
                "response_status": r.status_code, "response_body": raw_text[:4000]}
        raise ProviderError(f"ollama HTTP {r.status_code}: {raw_text[:300]}", meta)
    try:
        data = r.json()
    except ValueError:
        raise ProviderError(f"ollama non-JSON response: {raw_text[:300]}")
    msg = data.get("message") or {}
    # Either content or tool_calls must be present.
    has_content = bool((msg.get("content") or "").strip())
    has_tools = bool(msg.get("tool_calls"))
    if not has_content and not has_tools:
        raise ProviderError(f"ollama empty message: {str(data)[:300]}")
    meta = {
        "provider": "ollama",
        "model": spec["model"],
        "wall_s": round(dt, 2),
        "url": url,
        "request_headers": _redact(headers),
        "request_body": body,
        "response_status": r.status_code,
        "response_body": data,
        "prompt_eval_count": data.get("prompt_eval_count"),
        "eval_count": data.get("eval_count"),
    }
    return msg, meta


def call_openai_compat_chat(spec: dict, messages: list[dict], tools: list[dict] | None = None) -> tuple[dict, dict]:
    """OpenAI-compatible chat. Used by scribe / digest fallback paths only —
    the worker tick uses Ollama with native tool calls."""
    base = spec["base_url"].rstrip("/")
    url = f"{base}/chat/completions"
    headers = {"Content-Type": "application/json"}
    if spec.get("api_key"):
        headers["Authorization"] = f"Bearer {spec['api_key']}"
    body: dict = {
        "model": spec["model"],
        "messages": messages,
        "max_tokens": spec.get("num_predict", 6000),
        "stream": False,
    }
    if tools:
        body["tools"] = tools
    t0 = time.time()
    r = requests.post(url, headers=headers, json=body, timeout=spec["timeout_s"])
    dt = time.time() - t0
    raw_text = r.text
    if r.status_code != 200:
        raise ProviderError(f"openai_compat HTTP {r.status_code}: {raw_text[:300]}")
    try:
        data = r.json()
    except ValueError:
        raise ProviderError(f"openai_compat non-JSON response: {raw_text[:300]}")
    try:
        msg = data["choices"][0]["message"]
    except (KeyError, IndexError, TypeError):
        raise ProviderError(f"openai_compat malformed: {str(data)[:300]}")
    has_content = bool((msg.get("content") or "").strip())
    has_tools = bool(msg.get("tool_calls"))
    if not has_content and not has_tools:
        raise ProviderError("openai_compat empty message")
    meta = {
        "provider": "openai_compat",
        "model": spec["model"],
        "wall_s": round(dt, 2),
        "url": url,
        "request_headers": _redact(headers),
        "request_body": body,
        "response_status": r.status_code,
        "response_body": data,
        "usage": data.get("usage"),
    }
    return msg, meta


def call_provider_chat(spec: dict, messages: list[dict], tools: list[dict] | None = None) -> tuple[dict, dict]:
    t = spec["type"]
    if t == "ollama":
        return call_ollama_chat(spec, messages, tools)
    if t == "openai_compat":
        return call_openai_compat_chat(spec, messages, tools)
    raise ProviderError(f"unknown provider type {t}")


# ----------------------- availability ----------------------------------

_DAY_LOOKUP = {"mon": 0, "tue": 1, "wed": 2, "thu": 3, "fri": 4, "sat": 5, "sun": 6}


def _local_now(tz_name: str) -> datetime:
    try:
        from zoneinfo import ZoneInfo
        return datetime.now(ZoneInfo(tz_name))
    except Exception:
        return datetime.now()


def compute_availability(cfg: dict) -> dict:
    """Returns {in_window, sla_min, tz, windows, next_change_iso, summary}.
    If `availability` is missing or empty windows, returns in_window=true and
    sla_min from sla_in_window_min default — i.e. "always available"."""
    av = cfg.get("availability") or {}
    tz_name = av.get("tz", "Europe/London")
    windows_raw = av.get("windows") or []
    in_min = int(av.get("sla_in_window_min", 60))
    out_min = int(av.get("sla_out_window_min", 720))
    if not windows_raw:
        return {"in_window": True, "sla_min": in_min, "tz": tz_name,
                "next_change_iso": None, "summary": "always available (no windows configured)"}
    now = _local_now(tz_name)
    in_window = False
    current_end: datetime | None = None
    for offset in range(0, 8):
        from datetime import timedelta
        day = now + timedelta(days=offset)
        day_idx = day.weekday()
        for w in windows_raw:
            days = [_DAY_LOOKUP.get(str(d).lower()[:3], -1) for d in (w.get("days") or [])]
            if day_idx not in days:
                continue
            try:
                sh, sm = (int(x) for x in str(w["start"]).split(":"))
                eh, em = (int(x) for x in str(w["end"]).split(":"))
            except (ValueError, KeyError):
                continue
            start_t = day.replace(hour=sh, minute=sm, second=0, microsecond=0)
            end_t = day.replace(hour=eh, minute=em, second=0, microsecond=0)
            if offset == 0 and start_t <= now < end_t:
                in_window = True
                current_end = end_t
                break
        if in_window:
            break
    next_change: datetime | None = current_end
    if not in_window:
        # find the next start time across the upcoming week
        from datetime import timedelta
        soonest: datetime | None = None
        for offset in range(0, 8):
            day = now + timedelta(days=offset)
            day_idx = day.weekday()
            for w in windows_raw:
                days = [_DAY_LOOKUP.get(str(d).lower()[:3], -1) for d in (w.get("days") or [])]
                if day_idx not in days:
                    continue
                try:
                    sh, sm = (int(x) for x in str(w["start"]).split(":"))
                except (ValueError, KeyError):
                    continue
                start_t = day.replace(hour=sh, minute=sm, second=0, microsecond=0)
                if start_t > now and (soonest is None or start_t < soonest):
                    soonest = start_t
        next_change = soonest
    summary = (f"in-window (Chris likely responsive · SLA {in_min}m · "
               f"closes {current_end.strftime('%a %H:%M') if current_end else '?'})"
               if in_window
               else f"out-of-window (Chris likely asleep/away · SLA {out_min}m · "
                    f"opens {next_change.strftime('%a %H:%M') if next_change else '?'} · "
                    f"avoid blocking on approvals; do solo work)")
    return {"in_window": in_window, "sla_min": in_min if in_window else out_min,
            "tz": tz_name, "windows": windows_raw,
            "next_change_iso": next_change.isoformat(timespec="minutes") if next_change else None,
            "summary": summary}


def call_chat_with_fallback(cfg: dict, messages: list[dict],
                            tools: list[dict] | None = None) -> tuple[dict, dict, list[str], list[dict]]:
    """Run a single chat round-trip across primary → fallback.

    Returns (assistant_message, meta, failure_strings, attempt_records).
    Used by the worker tick's tool-use loop (one round per loop iteration)
    and indirectly by scribe/digest via the legacy wrapper below."""
    failures: list[str] = []
    attempts: list[dict] = []
    specs = {slot: dict(cfg["llm"][slot]) for slot in ("primary", "fallback")}
    for slot in ("primary", "fallback"):
        spec = specs[slot]
        if not spec.get("base_url") or not spec.get("model"):
            failures.append(f"{slot}: not configured")
            attempts.append({"slot": slot, "ok": False, "error": "not configured"})
            continue
        try:
            msg, meta = call_provider_chat(spec, messages, tools)
            meta["slot"] = slot
            attempts.append({"slot": slot, "ok": True, "meta": meta, "message": msg})
            return msg, meta, failures, attempts
        except (ProviderError, requests.Timeout, requests.ConnectionError) as e:
            failures.append(f"{slot}({spec['type']}): {e}")
            attempts.append({"slot": slot, "ok": False, "type": spec["type"],
                             "model": spec.get("model"), "error": str(e)[:1000]})
            continue
    err = ProviderError("all providers failed: " + "; ".join(failures))
    err.failures = failures
    err.attempts = attempts
    raise err


def call_llm_with_fallback(cfg: dict, system: str, user: str) -> tuple[str, dict, list[str], list[dict]]:
    """Legacy single-shot wrapper for scribe.py / digest.py / one-shot callers.

    Returns (content_str, meta, failure_strings, attempt_records). Builds a
    plain [system, user] message list with no tools."""
    msg, meta, failures, attempts = call_chat_with_fallback(
        cfg,
        [{"role": "system", "content": system}, {"role": "user", "content": user}],
        tools=None,
    )
    content = msg.get("content") or ""
    return content, meta, failures, attempts


# ----------------------- json extraction -------------------------------

def extract_json(text: str) -> dict | None:
    """Find a JSON object: first ```json fence, else first balanced {}."""
    m = re.search(r"```json\s*(\{.*?\})\s*```", text, re.DOTALL)
    if m:
        try:
            return json.loads(m.group(1))
        except json.JSONDecodeError:
            pass
    # fall back: scan for first balanced top-level object
    start = text.find("{")
    while start != -1:
        depth = 0
        in_str = False
        esc = False
        for i in range(start, len(text)):
            c = text[i]
            if in_str:
                if esc:
                    esc = False
                elif c == "\\":
                    esc = True
                elif c == '"':
                    in_str = False
            else:
                if c == '"':
                    in_str = True
                elif c == "{":
                    depth += 1
                elif c == "}":
                    depth -= 1
                    if depth == 0:
                        candidate = text[start:i + 1]
                        try:
                            return json.loads(candidate)
                        except json.JSONDecodeError:
                            break
        start = text.find("{", start + 1)
    return None


# -------------------------- tool execution -----------------------------

def shell_blocked(cmd: str, denylist: list[str]) -> str | None:
    low = cmd.lower()
    for s in denylist:
        if s.lower() in low:
            return s
    return None


def exec_shell(cfg: dict, paths: Paths, action: dict) -> dict:
    cmd = str(action.get("cmd", "")).strip()
    if not cmd:
        return {"ok": False, "error": "empty cmd"}
    blocked = shell_blocked(cmd, cfg["guardrails"]["shell_denylist_substrings"])
    if blocked:
        return {"ok": False, "error": f"denylist matched: {blocked!r}"}
    try:
        proc = subprocess.run(
            cmd,
            shell=True,
            cwd=str(paths.workdir),
            capture_output=True,
            timeout=cfg["tick"]["shell_timeout_s"],
            text=True,
            errors="replace",
        )
    except subprocess.TimeoutExpired:
        return {"ok": False, "error": "timeout"}
    out = (proc.stdout or "")
    err = (proc.stderr or "")
    return {
        "ok": proc.returncode == 0,
        "rc": proc.returncode,
        "stdout": truncate(out, cfg["tick"]["shell_output_max_bytes"]),
        "stderr": truncate(err, cfg["tick"]["shell_output_max_bytes"]),
    }


def exec_http_get(cfg: dict, action: dict) -> dict:
    url = str(action.get("url", "")).strip()
    if not url.lower().startswith(("http://", "https://")):
        return {"ok": False, "error": "url must start with http:// or https://"}
    try:
        r = requests.get(url, timeout=cfg["tick"]["http_timeout_s"], allow_redirects=True,
                         headers={"User-Agent": "mako-zero/0.1 (+https://minkforge.com)"})
    except requests.RequestException as e:
        return {"ok": False, "error": str(e)[:300]}
    body = r.text or ""
    return {
        "ok": r.status_code < 400,
        "status": r.status_code,
        "url_final": r.url,
        "size_bytes": len(r.content),
        "body": truncate(body, cfg["tick"]["http_response_max_bytes"]),
    }


def exec_write_file(cfg: dict, paths: Paths, action: dict) -> dict:
    rel = str(action.get("path", "")).strip()
    content = action.get("content", "")
    mode = str(action.get("mode", "write"))
    target = resolve_under(paths.root, rel)
    if target is None:
        return {"ok": False, "error": "path escapes root"}
    if not is_writable(cfg, paths, target):
        return {"ok": False, "error": f"path not writable: {rel}"}
    if mode == "append":
        append_text(target, content)
    else:
        write_text(target, content)
    return {"ok": True, "path": str(target.relative_to(paths.root)), "bytes": len(content.encode("utf-8")), "mode": mode}


def exec_read_file(cfg: dict, paths: Paths, action: dict) -> dict:
    rel = str(action.get("path", "")).strip()
    target = resolve_under(paths.root, rel)
    if target is None:
        return {"ok": False, "error": "path escapes root"}
    if not target.exists():
        return {"ok": False, "error": "not found"}
    try:
        body = target.read_text(encoding="utf-8")
    except Exception as e:
        return {"ok": False, "error": str(e)[:300]}
    return {"ok": True, "path": str(target.relative_to(paths.root)),
            "body": truncate(body, cfg["tick"]["http_response_max_bytes"])}


def exec_git(cfg: dict, paths: Paths, action: dict) -> dict:
    cmd = str(action.get("cmd", "")).strip()
    if not cmd:
        return {"ok": False, "error": "empty cmd"}
    if "push" in cmd.split():
        return {"ok": False, "error": "git push is gated"}
    full = "git " + cmd
    return exec_shell(cfg, paths, {"cmd": full})


_THREAD_ALIASES = {
    "log": "log_thread_id",
    "requests": "requests_thread_id",
    "request": "requests_thread_id",
    "approvals": "approvals_thread_id",
    "approval": "approvals_thread_id",
    "digest": "digest_thread_id",
    "digests": "digest_thread_id",
    "revenue": "revenue_thread_id",
    "meta": "meta_thread_id",
}
_GENERAL_ALIASES = {"general", "main", "chat"}


def _resolve_thread(cfg: dict, thread) -> int | None:
    """Resolve a thread reference to a numeric Telegram thread ID.

    Accepts:
      - None / ""              → log thread (default)
      - int or numeric str     → returned as int
      - "log" / "requests" /
        "approvals" /
        "digest" / "revenue"
        (and singular/plural
         aliases)              → looked up in cfg["telegram"]
      - "general" / "main" /
        "chat"                 → None (posts to the general group chat)
      - unknown name           → falls back to log thread
    """
    if thread is None or thread == "":
        return cfg["telegram"].get("log_thread_id")
    try:
        return int(thread)
    except (TypeError, ValueError):
        pass
    name = str(thread).strip().lower()
    if name in _GENERAL_ALIASES:
        return None
    key = _THREAD_ALIASES.get(name)
    if key is None:
        return cfg["telegram"].get("log_thread_id")
    return cfg["telegram"].get(key) or cfg["telegram"].get("log_thread_id")


def exec_telegram_post(cfg: dict, action: dict) -> dict:
    thread = _resolve_thread(cfg, action.get("thread"))
    text = str(action.get("text", ""))
    return telegram_send(cfg, text, thread_id=thread, label="action")


def exec_ask_chris(cfg: dict, action: dict) -> dict:
    text = "❓ ask_chris: " + str(action.get("text", "")).strip()
    return telegram_send(cfg, text, thread_id=cfg["telegram"]["requests_thread_id"], label="ask_chris")


def gated_action_thread(cfg: dict) -> int | None:
    tg = cfg["telegram"]
    return (tg.get("approvals_thread_id")
            or tg.get("requests_thread_id")
            or tg.get("log_thread_id"))


def _gated_summary(qid: str, action: dict) -> str:
    t = str(action.get("type", "?"))
    lines = [f"⏸ {qid} · {t} · NEEDS APPROVAL"]
    if t == "email_send":
        lines.append(f"to: {action.get('to','?')}")
        lines.append(f"subject: {str(action.get('subject',''))[:80]}")
        body = str(action.get("body", ""))[:300]
        if body:
            lines.append(f"body: {body}")
    elif t == "cf_api":
        lines.append(f"{action.get('method','?')} {action.get('path','?')}")
        b = action.get("body")
        if b is not None:
            lines.append(f"body: {json.dumps(b)[:200]}")
    elif t in ("http_post", "http_put", "http_delete"):
        lines.append(f"{t.upper()} {action.get('url','?')}")
        b = action.get("body")
        if b is not None:
            body_str = b if isinstance(b, str) else json.dumps(b, ensure_ascii=False)
            lines.append(f"body: {body_str[:300]}")
    elif t == "spend":
        lines.append(f"£{action.get('amount_pence',0)/100:.2f} — {action.get('reason','?')}")
    if action.get("spend"):
        s = action["spend"]
        lines.append(f"spend tag: £{s.get('amount_pence',0)/100:.2f} — {s.get('reason','?')}")
    if action.get("reason") and t not in ("spend",):
        lines.append(f"reason: {str(action['reason'])[:200]}")
    lines.append("approve: edit pending_actions.jsonl or run manually")
    return "\n".join(lines)


def exec_email_send(cfg: dict, action: dict) -> dict:
    """SMTP via Fastmail (or any SMTP_SSL host configured)."""
    import smtplib
    from email.message import EmailMessage
    fm = cfg.get("fastmail", {})
    if not fm.get("smtp_password"):
        return {"ok": False, "error": "fastmail.smtp_password not configured"}
    to = action.get("to")
    if not to:
        return {"ok": False, "error": "missing 'to'"}
    msg = EmailMessage()
    msg["From"] = fm.get("from") or fm.get("user", "")
    msg["To"] = to
    msg["Subject"] = action.get("subject", "(no subject)")
    msg.set_content(action.get("body", ""))
    try:
        with smtplib.SMTP_SSL(fm.get("smtp_host", "smtp.fastmail.com"),
                              fm.get("smtp_port", 465), timeout=30) as s:
            s.login(fm.get("user") or fm.get("from", ""), fm["smtp_password"])
            s.send_message(msg)
    except Exception as e:
        return {"ok": False, "error": f"{type(e).__name__}: {str(e)[:300]}"}
    return {"ok": True, "to": to, "subject": msg["Subject"]}


def _coerce_json_body(body: Any) -> Any:
    """Some models emit the request body as a JSON-encoded *string* instead of
    an object/array. Detect and decode so requests doesn't double-encode."""
    if isinstance(body, str):
        s = body.strip()
        if s.startswith(("{", "[")):
            try:
                return json.loads(s)
            except json.JSONDecodeError:
                return body
    return body


def exec_cf_api(cfg: dict, action: dict) -> dict:
    """Cloudflare API call. action.method, action.path (under /client/v4), action.body."""
    cf = cfg.get("cloudflare", {})
    if not cf.get("token"):
        return {"ok": False, "error": "cloudflare.token not configured"}
    method = str(action.get("method", "GET")).upper()
    path = str(action.get("path", "/"))
    if not path.startswith("/"):
        path = "/" + path
    url = f"https://api.cloudflare.com/client/v4{path}"
    headers = {"Authorization": f"Bearer {cf['token']}", "Content-Type": "application/json"}
    body = _coerce_json_body(action.get("body"))
    try:
        r = requests.request(method, url, headers=headers,
                             json=body if body is not None else None, timeout=30)
    except requests.RequestException as e:
        return {"ok": False, "error": f"{type(e).__name__}: {str(e)[:300]}"}
    return {"ok": r.status_code < 400, "status": r.status_code,
            "body": r.text[:1500]}


def exec_http_mutating(action: dict) -> dict:
    """http_post / http_put / http_delete via requests."""
    t = str(action.get("type", "http_post"))
    method = t.replace("http_", "").upper()
    url = action.get("url")
    if not url:
        return {"ok": False, "error": "missing 'url'"}
    body = _coerce_json_body(action.get("body"))
    headers = action.get("headers") or {}
    kwargs: dict = {"headers": headers, "timeout": 30}
    if isinstance(body, (dict, list)):
        kwargs["json"] = body
    elif isinstance(body, str):
        kwargs["data"] = body
    try:
        r = requests.request(method, url, **kwargs)
    except requests.RequestException as e:
        return {"ok": False, "error": f"{type(e).__name__}: {str(e)[:300]}"}
    return {"ok": r.status_code < 400, "status": r.status_code,
            "body": r.text[:1500]}


def exec_spend(paths: Paths, action: dict) -> dict:
    """Append to state/spend.jsonl — simple ledger; STATE.md still tracks MTD."""
    rec = {
        "ts": now_iso(),
        "amount_pence": int(action.get("amount_pence", 0) or 0),
        "reason": str(action.get("reason", ""))[:300],
    }
    p = paths.state / "spend.jsonl"
    p.parent.mkdir(parents=True, exist_ok=True)
    with p.open("a", encoding="utf-8") as f:
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    return {"ok": True, "amount_pence": rec["amount_pence"], "reason": rec["reason"]}


def execute_gated_action(cfg: dict, paths: Paths, action: dict) -> dict:
    """Run a gated action (called by the listener after approval).
    Single dispatch for all gated types."""
    t = str(action.get("type", ""))
    if t == "email_send":
        return exec_email_send(cfg, action)
    if t == "cf_api":
        return exec_cf_api(cfg, action)
    if t in ("http_post", "http_put", "http_delete"):
        return exec_http_mutating(action)
    if t == "spend":
        return exec_spend(paths, action)
    return {"ok": False, "error": f"no gated executor for {t!r}"}


def queue_gated_action(cfg: dict, paths: Paths, action: dict) -> dict:
    qid = f"q{int(time.time()*1000)}"
    rec = {"id": qid, "queued_at": now_iso(), "action": action}
    append_text(paths.pending / "pending_actions.jsonl", json.dumps(rec) + "\n")
    av = compute_availability(cfg)
    suppress = bool((cfg.get("availability") or {}).get(
        "suppress_approval_pings_out_of_window", True))
    silent = (not av["in_window"]) and suppress
    summary = _gated_summary(qid, action)
    if silent:
        summary = "🌙 (out-of-hours · silent ping)\n" + summary
    try:
        telegram_send(cfg, summary, thread_id=gated_action_thread(cfg),
                      label="gated", silent=silent)
    except Exception:
        pass  # never let notification failure break queueing
    return {"ok": True, "queued": True, "id": qid, "type": action.get("type"),
            "silent_ping": silent}


def queue_resource_request(cfg: dict, paths: Paths, action: dict) -> dict:
    """request_resource is a multi-turn conversation with Chris on the
    Requests thread. Persists to pending/resources.jsonl so Mako sees it
    as an open request in hot context next tick."""
    rid = f"r{int(time.time()*1000)}"
    rec = {"id": rid, "queued_at": now_iso(), "status": "open", "action": action}
    append_text(paths.pending / "resources.jsonl", json.dumps(rec) + "\n")
    av = compute_availability(cfg)
    suppress = bool((cfg.get("availability") or {}).get(
        "suppress_approval_pings_out_of_window", True))
    silent = (not av["in_window"]) and suppress
    cat = action.get("category", "other")
    ask = str(action.get("ask", "(no ask)"))[:120]
    rationale = str(action.get("rationale", ""))[:300]
    business_case = str(action.get("business_case", ""))[:500]
    alts = str(action.get("alternatives_tried", ""))[:200]
    summary = (
        f"📨 {rid} · REQUEST · {cat}\n"
        f"ask: {ask}\n"
        f"rationale: {rationale}\n"
        + (f"business case: {business_case}\n" if business_case else "")
        + (f"alternatives tried: {alts}\n" if alts else "")
        + f"reply on this thread to discuss / approve / reject"
    )
    if silent:
        summary = "🌙 (out-of-hours · silent ping)\n" + summary
    try:
        # Resource requests go to the Requests thread (not Approvals — they
        # need discussion, not a yes/no).
        thread = (cfg["telegram"].get("requests_thread_id")
                  or cfg["telegram"].get("approvals_thread_id")
                  or cfg["telegram"].get("log_thread_id"))
        telegram_send(cfg, summary, thread_id=thread, label="request", silent=silent)
    except Exception:
        pass
    return {"ok": True, "queued": True, "id": rid, "type": "request_resource",
            "category": cat, "silent_ping": silent}


# ----------------- non-gated dispatcher --------------------------------

NON_GATED = {"shell", "http_get", "write_file", "read_file", "git",
             "telegram_post", "ask_chris", "cf_api"}
GATED = {"email_send", "http_post", "http_put", "http_delete", "spend"}


def dispatch_action(cfg: dict, paths: Paths, action: dict) -> dict:
    t = str(action.get("type", "")).strip()
    needs_approval = bool(action.get("needs_approval", False))
    # Resource requests are their own queue + Telegram thread.
    if t == "request_resource":
        return queue_resource_request(cfg, paths, action)
    if needs_approval or t in GATED:
        return queue_gated_action(cfg, paths, action)
    if t == "shell":
        return exec_shell(cfg, paths, action)
    if t == "http_get":
        return exec_http_get(cfg, action)
    if t == "write_file":
        return exec_write_file(cfg, paths, action)
    if t == "read_file":
        return exec_read_file(cfg, paths, action)
    if t == "git":
        return exec_git(cfg, paths, action)
    if t == "cf_api":
        return exec_cf_api(cfg, action)
    if t == "telegram_post":
        return exec_telegram_post(cfg, action)
    if t == "ask_chris":
        return exec_ask_chris(cfg, action)
    return {"ok": False, "error": f"unknown action type: {t!r}"}


# ------------------------- tool-use loop -------------------------------
#
# Inside a worker tick the model runs an iterative tool-use loop: it can call
# any of the tools below, see the result, and decide the next step. This
# replaces the old single-shot JSON-blob protocol where every action had to be
# planned in advance. The loop terminates when the model calls the special
# `finish` tool (which carries the final structured payload) or when the
# iteration cap / soft deadline forces a wrap-up.

def tool_definitions() -> list[dict]:
    """Static tool schemas exposed to the model. Add a tool here AND wire it
    in `execute_tool_call` below — the two must stay in sync."""
    return [
        {"type": "function", "function": {
            "name": "shell",
            "description": "Run a shell command in workdir/. 30s timeout. Output truncated to 4KB. Denylist blocks the obviously catastrophic.",
            "parameters": {"type": "object", "properties": {
                "cmd": {"type": "string", "description": "Shell command to run"}
            }, "required": ["cmd"]}
        }},
        {"type": "function", "function": {
            "name": "read_file",
            "description": "Read a file under /srv/mako-zero/. Body truncated to 8KB.",
            "parameters": {"type": "object", "properties": {
                "path": {"type": "string", "description": "Path under /srv/mako-zero/"}
            }, "required": ["path"]}
        }},
        {"type": "function", "function": {
            "name": "write_file",
            "description": "Write or append to a file under state/, notes/, workdir/, archive/, or pending/. Other paths rejected.",
            "parameters": {"type": "object", "properties": {
                "path": {"type": "string"},
                "content": {"type": "string"},
                "mode": {"type": "string", "enum": ["write", "append"], "description": "default: write"}
            }, "required": ["path", "content"]}
        }},
        {"type": "function", "function": {
            "name": "http_get",
            "description": "Read-only HTTP GET. Bare HTTP — no JS rendering. 30s timeout, response truncated to 8KB.",
            "parameters": {"type": "object", "properties": {
                "url": {"type": "string"}
            }, "required": ["url"]}
        }},
        {"type": "function", "function": {
            "name": "git",
            "description": "Local git command (no push). Runs in workdir/.",
            "parameters": {"type": "object", "properties": {
                "cmd": {"type": "string", "description": "git args without the leading 'git'"}
            }, "required": ["cmd"]}
        }},
        {"type": "function", "function": {
            "name": "cf_api",
            "description": "Cloudflare API call for minkforge.com. Free + non-gated. method/path/body.",
            "parameters": {"type": "object", "properties": {
                "method": {"type": "string", "enum": ["GET", "POST", "PUT", "PATCH", "DELETE"]},
                "path": {"type": "string", "description": "Path under /client/v4, e.g. /zones/.../dns_records"},
                "body": {"description": "Request body (object or null)"}
            }, "required": ["method", "path"]}
        }},
        {"type": "function", "function": {
            "name": "telegram_post",
            "description": "Post a message to one of your Telegram threads. Use sparingly — the wrapper auto-posts a per-tick summary already.",
            "parameters": {"type": "object", "properties": {
                "thread": {"type": "string", "description": "Thread name: log/requests/approvals/digest/revenue/meta or numeric id"},
                "text": {"type": "string"}
            }, "required": ["text"]}
        }},
        {"type": "function", "function": {
            "name": "ask_chris",
            "description": "Open question to Chris (Requests thread, multi-turn, his reply lands in INBOX next tick). Use sparingly.",
            "parameters": {"type": "object", "properties": {
                "text": {"type": "string"}
            }, "required": ["text"]}
        }},
        {"type": "function", "function": {
            "name": "request_resource",
            "description": "Structured business case for a tool/account/budget you need from Chris.",
            "parameters": {"type": "object", "properties": {
                "category": {"type": "string", "enum": ["domain", "software", "budget", "api_key", "paid_service", "other"]},
                "ask": {"type": "string"},
                "rationale": {"type": "string"},
                "business_case": {"type": "string"},
                "alternatives_tried": {"type": "string"}
            }, "required": ["category", "ask", "rationale"]}
        }},
        {"type": "function", "function": {
            "name": "finish",
            "description": "Terminate the tick with the final structured summary. Call exactly once at the end. work_done is mandatory and must reflect what you actually did this tick (past tense, specific).",
            "parameters": {"type": "object", "properties": {
                "work_done": {"type": "string", "description": "1-3 line journal entry, past tense, specific, includes failures honestly"},
                "tick_mode": {"type": "string", "enum": ["operative", "generative"]},
                "state_md": {"type": "string", "description": "Full rewritten STATE.md (≤1KB), includes MTD spend"},
                "next_md": {"type": "string", "description": "Full rewritten NEXT.md (≤500B), specifies first action of next tick"},
                "persona_update": {"type": "object", "properties": {
                    "mode": {"type": "string", "enum": ["append", "write", "skip"]},
                    "content": {"type": "string"}
                }},
                "request_notes": {"type": "array", "items": {"type": "string"}, "description": "Up to 5 notes/ files to load into hot context next tick"},
                "telegram": {"type": "string", "description": "≤1000 char log thread post for this tick (200-500 ideal)"},
                "compact_now": {"type": "boolean"},
                "drift_flag": {"type": "string", "description": "Short note if you've drifted from MISSION.md, else omit"},
                "gated_actions": {"type": "array", "description": "Approval-gated actions to queue (email_send, http_post|put|delete, spend>£2). Each item has type and the type's args plus needs_approval:true.",
                                  "items": {"type": "object"}}
            }, "required": ["work_done"]}
        }},
    ]


def execute_tool_call(cfg: dict, paths: Paths, name: str, args: dict) -> dict:
    """Dispatch a single tool call. The `finish` tool is handled in
    run_tool_loop, not here."""
    if name == "shell":
        return exec_shell(cfg, paths, {"cmd": args.get("cmd", "")})
    if name == "read_file":
        return exec_read_file(cfg, paths, {"path": args.get("path", "")})
    if name == "write_file":
        return exec_write_file(cfg, paths, {
            "path": args.get("path", ""),
            "content": args.get("content", ""),
            "mode": args.get("mode", "write"),
        })
    if name == "http_get":
        return exec_http_get(cfg, {"url": args.get("url", "")})
    if name == "git":
        return exec_git(cfg, paths, {"cmd": args.get("cmd", "")})
    if name == "cf_api":
        return exec_cf_api(cfg, {
            "method": args.get("method", "GET"),
            "path": args.get("path", "/"),
            "body": args.get("body"),
        })
    if name == "telegram_post":
        return exec_telegram_post(cfg, {
            "thread": args.get("thread"),
            "text": args.get("text", ""),
        })
    if name == "ask_chris":
        return exec_ask_chris(cfg, {"text": args.get("text", "")})
    if name == "request_resource":
        return queue_resource_request(cfg, paths, {
            "type": "request_resource",
            "category": args.get("category", "other"),
            "ask": args.get("ask", ""),
            "rationale": args.get("rationale", ""),
            "business_case": args.get("business_case", ""),
            "alternatives_tried": args.get("alternatives_tried", ""),
        })
    return {"ok": False, "error": f"unknown tool: {name!r}"}


def _truncate_tool_result(result: dict, max_bytes: int = 8000) -> str:
    """Serialise a tool result for the model, capped to keep context bounded."""
    s = json.dumps(result, ensure_ascii=False)
    if len(s.encode("utf-8")) <= max_bytes:
        return s
    return s[:max_bytes] + f" …[truncated, full result was {len(s)} chars]"


def _coerce_args(raw: Any) -> dict:
    """Tool-call arguments arrive as a dict (Ollama native) or a JSON-encoded
    string (some openai-compat backends). Normalise to dict."""
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, str):
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            return {}
    return {}


def run_tool_loop(cfg: dict, paths: Paths, system: str, user_msg: str,
                  full_log: dict, tick_started_at: float) -> tuple[dict, dict, list[dict]]:
    """Drive the in-tick tool-use loop. Returns (final_payload, last_meta, attempts).

    `final_payload` is the args dict from the model's `finish` tool call. If
    the model never calls finish (loop cap / deadline / explicit content-only
    response), we synthesise a minimal payload from the last assistant content
    so the wrapper can still journal something."""
    tl = cfg.get("tool_loop") or {}
    max_iter = int(tl.get("max_iterations", 8))
    soft_deadline_s = float(tl.get("soft_deadline_s", 240))

    tools = tool_definitions()
    messages: list[dict] = [
        {"role": "system", "content": system},
        {"role": "user", "content": user_msg},
    ]
    full_log["llm_attempts"] = []
    full_log["tool_calls"] = []
    last_meta: dict = {}
    last_msg: dict = {}

    for step in range(max_iter):
        # Soft deadline: if we're past the budget, nudge the model to wrap up
        # (the model still has one more LLM round-trip to call finish).
        elapsed = time.time() - tick_started_at
        if elapsed > soft_deadline_s:
            messages.append({
                "role": "user",
                "content": (f"⚠️ Soft deadline hit ({int(elapsed)}s elapsed). "
                            f"Call the `finish` tool now with whatever progress you have. "
                            f"Don't start any more shell/http work this tick.")
            })

        msg, meta, _failures, attempts_round = call_chat_with_fallback(cfg, messages, tools)
        full_log["llm_attempts"].extend(attempts_round)
        last_meta = meta
        last_msg = msg

        # Persist the assistant turn into the conversation. Strip any thinking
        # field — Ollama's tool calls already encode the decision.
        assistant_turn: dict = {"role": "assistant"}
        if msg.get("content"):
            assistant_turn["content"] = msg["content"]
        if msg.get("tool_calls"):
            assistant_turn["tool_calls"] = msg["tool_calls"]
        messages.append(assistant_turn)

        tool_calls = msg.get("tool_calls") or []
        if not tool_calls:
            # Model returned content with no tool calls. Treat as implicit finish:
            # try to extract JSON from content; if absent, synthesise.
            content = (msg.get("content") or "").strip()
            obj = extract_json(content) if content else None
            if obj and obj.get("work_done"):
                full_log["tool_calls"].append({"name": "<implicit_finish>", "args": obj, "result": {"ok": True}})
                return obj, last_meta, full_log["llm_attempts"]
            # Force one more round explicitly demanding finish().
            messages.append({
                "role": "user",
                "content": ("You returned content without calling any tool. "
                            "Call the `finish` tool now with your work_done + state_md + next_md + telegram fields. "
                            "Nothing else.")
            })
            continue

        # Process each tool call in order. If one of them is `finish`, that
        # short-circuits the loop.
        finish_payload: dict | None = None
        for tc in tool_calls:
            fn = (tc.get("function") or {})
            name = fn.get("name", "")
            args = _coerce_args(fn.get("arguments"))
            tc_id = tc.get("id") or f"call_{len(full_log['tool_calls'])}"

            if name == "finish":
                finish_payload = args
                full_log["tool_calls"].append({"id": tc_id, "name": name, "args": args, "result": {"ok": True, "terminator": True}})
                # Tool response message — Ollama expects it even for finish.
                messages.append({"role": "tool", "tool_call_id": tc_id, "name": name,
                                 "content": json.dumps({"ok": True})})
                continue

            result = execute_tool_call(cfg, paths, name, args)
            full_log["tool_calls"].append({"id": tc_id, "name": name, "args": args, "result": result})
            messages.append({"role": "tool", "tool_call_id": tc_id, "name": name,
                             "content": _truncate_tool_result(result)})

        if finish_payload is not None:
            return finish_payload, last_meta, full_log["llm_attempts"]

    # Loop cap exhausted without a finish. Synthesise a minimal payload from
    # whatever the last assistant turn said so we still journal + telegram.
    last_text = (last_msg.get("content") or "").strip()
    obj = extract_json(last_text) if last_text else None
    if obj and obj.get("work_done"):
        return obj, last_meta, full_log["llm_attempts"]
    synthesised = {
        "work_done": (f"tool-loop hit max_iterations={max_iter} without calling finish; "
                      f"last assistant content: {last_text[:300] if last_text else '<empty>'}"),
        "tick_mode": "operative",
        "state_md": read_text(paths.state / "STATE.md"),
        "next_md": read_text(paths.state / "NEXT.md"),
        "telegram": "tool-loop exhausted without finish() — see tick payload",
        "_synthesised": True,
    }
    return synthesised, last_meta, full_log["llm_attempts"]


# ----------------------- loop detection --------------------------------
#
# Replaces the broken model-reported `progress_confidence` (was clamped to 7
# across 1000+ ticks) with a deterministic check on the journal: if the new
# work_done text is too similar to recent entries, flag and force generative.

def detect_loop(paths: Paths, current_work_done: str, cfg: dict) -> dict:
    """Compare current work_done against the last N journal entries.
    Returns a dict with loop_detected/similar_count/sample for hot context."""
    from difflib import SequenceMatcher
    tl = cfg.get("tool_loop") or {}
    recent_n = int(tl.get("loop_detect_recent", 5))
    ratio_thr = float(tl.get("loop_detect_ratio", 0.6))
    count_thr = int(tl.get("loop_detect_count", 3))

    journal = paths.state / "JOURNAL.md"
    if not journal.exists() or not current_work_done.strip():
        return {"loop_detected": False, "similar_count": 0, "recent_n": recent_n,
                "ratio_threshold": ratio_thr, "count_threshold": count_thr}
    lines = [L for L in journal.read_text(encoding="utf-8").splitlines()
             if L.strip().startswith("#")][-recent_n:]
    cur = current_work_done.lower().strip()
    similar = 0
    sample: list[str] = []
    for line in lines:
        if " — " not in line:
            continue
        prev = line.split(" — ", 1)[1].lower().strip()
        ratio = SequenceMatcher(None, cur, prev).ratio()
        if ratio >= ratio_thr:
            similar += 1
            sample.append(f"{ratio:.2f}: {line[:100]}")
    return {
        "loop_detected": similar >= count_thr,
        "similar_count": similar,
        "recent_n": recent_n,
        "ratio_threshold": ratio_thr,
        "count_threshold": count_thr,
        "sample": sample,
    }


# ------------------------- file writes (model) -------------------------

def apply_model_files(cfg: dict, paths: Paths, files: list[dict]) -> list[dict]:
    results = []
    for i, entry in enumerate(files or []):
        if not isinstance(entry, dict):
            results.append({
                "ok": False,
                "error": f"files[{i}] must be an object",
                "entry_type": type(entry).__name__,
            })
            continue
        results.append(exec_write_file(cfg, paths, entry))
    return results


def apply_state_md(paths: Paths, key: str, content: str) -> None:
    if content is None:
        return
    target = paths.state / key
    write_text(target, content if content.endswith("\n") else content + "\n")


def apply_persona_update(paths: Paths, upd: dict | None) -> None:
    if not upd or upd.get("mode") == "skip":
        return
    content = upd.get("content") or ""
    if not content.strip():
        return
    p = paths.state / "PERSONA.md"
    if upd.get("mode") == "append":
        append_text(p, ("\n" if read_text(p) else "") + content.rstrip() + "\n")
    else:
        write_text(p, content if content.endswith("\n") else content + "\n")


# --------------------------- journal -----------------------------------

def append_journal(paths: Paths, tick_n: int, work_done: str) -> None:
    line = f"#{tick_n} {now_iso()} — {work_done.strip().replace(chr(10), ' / ')}\n"
    append_text(paths.state / "JOURNAL.md", line)


# --------------------------- telegram ----------------------------------

TELEGRAM_HARD_LIMIT = 4096   # Telegram sendMessage cap (chars, UTF-16 code units in practice)
TELEGRAM_SAFE_LIMIT = 4000   # leave headroom for emoji / multi-byte


def telegram_send(cfg: dict, text: str, thread_id: int | None = None, label: str = "",
                  silent: bool = False) -> dict:
    tok = cfg["telegram"]["bot_token"]
    chat = cfg["telegram"]["chat_id"]
    if not tok:
        return {"ok": False, "error": "no telegram bot token configured"}
    if thread_id is None:
        thread_id = cfg["telegram"]["log_thread_id"]
    url = f"https://api.telegram.org/bot{tok}/sendMessage"

    truncated = False
    original_len = len(text)
    if original_len > TELEGRAM_SAFE_LIMIT:
        marker = f"\n\n…[truncated · {original_len} chars total · see logs/ticks/<n>.json]"
        text = text[:TELEGRAM_SAFE_LIMIT - len(marker)] + marker
        truncated = True

    payload: dict[str, Any] = {"chat_id": chat, "text": text}
    if thread_id:
        payload["message_thread_id"] = int(thread_id)
    if silent:
        payload["disable_notification"] = True
    try:
        r = requests.post(url, json=payload, timeout=10)
    except requests.RequestException as e:
        return {"ok": False, "error": str(e)[:200], "label": label, "truncated": truncated}
    if r.status_code != 200:
        return {"ok": False, "status": r.status_code, "body": r.text[:300],
                "label": label, "truncated": truncated, "original_len": original_len}
    return {"ok": True, "label": label, "truncated": truncated, "sent_len": len(text),
            "original_len": original_len}


# --------------------------- metrics -----------------------------------

METRIC_FIELDS = [
    "ts", "tick_n", "mode", "wall_s", "provider_used", "model_used",
    "input_tokens_est", "input_tokens", "output_tokens", "output_chars",
    "progress_confidence",     # legacy column — kept empty so old analyse/dashboards still parse
    "loop_score",              # NEW: similar_count from detect_loop (0..recent_n)
    "tool_steps",              # NEW: number of non-finish tool calls executed in the tick
    "actions_count", "actions_executed", "actions_queued",
    "parse_ok", "drift_flag", "compact_now", "failures",
]


def write_full_log(paths: Paths, kind: str, n: int, payload: dict) -> Path | None:
    """Write the full request/response payload for one run to logs/<kind>/<n>.json.
    Returns the path or None if writing fails. kind is 'ticks' or 'scribe'."""
    out = paths.logs / kind / f"{n:08d}.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    try:
        out.write_text(json.dumps(payload, ensure_ascii=False, indent=2, default=str),
                       encoding="utf-8")
        return out
    except Exception as e:
        # Logging failure must never break the tick.
        sys.stderr.write(f"write_full_log failed: {e!r}\n")
        return None


def record_metric(paths: Paths, row: dict) -> None:
    p = paths.logs / "metrics.csv"
    new_file = not p.exists()
    p.parent.mkdir(parents=True, exist_ok=True)
    with p.open("a", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=METRIC_FIELDS)
        if new_file:
            w.writeheader()
        w.writerow({k: row.get(k, "") for k in METRIC_FIELDS})


# --------------------------- tick counter ------------------------------

def bump_tick(paths: Paths) -> int:
    p = paths.state / "tick_counter.txt"
    n = int(read_text(p, "0") or "0") + 1
    write_text(p, str(n))
    return n


# --------------------------- compaction --------------------------------

def maybe_set_compaction(paths: Paths, tick_n: int, every_n: int, agent_flag: bool) -> None:
    flag = paths.state / "compact_pending.flag"
    if agent_flag or (every_n > 0 and tick_n > 0 and tick_n % every_n == 0):
        write_text(flag, now_iso())


def clear_compaction(paths: Paths) -> None:
    f = paths.state / "compact_pending.flag"
    if f.exists():
        f.unlink()


# --------------------------- main tick ---------------------------------

def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", required=True)
    ap.add_argument("--mode", choices=["normal", "compact"], default="normal")
    args = ap.parse_args()

    cfg = load_config(args.config)
    paths = Paths(cfg)
    paths.ensure()

    tick_n = bump_tick(paths)
    t_start = time.time()

    failures: list[str] = []
    parse_ok = False
    actions_executed = 0
    actions_queued = 0
    drift_flag: str | None = None
    compact_now_flag = False
    output_chars = 0
    meta: dict = {}

    inbox_snap = snapshot_inbox(paths)

    # Full-payload log (written at the very end, success or failure).
    full_log: dict[str, Any] = {
        "tick_n": tick_n,
        "mode": args.mode,
        "started_at": now_iso(),
        "system_prompt": None,
        "user_message": None,
        "sizes": None,
        "inbox_present": bool(inbox_snap),
        "llm_attempts": [],
        "raw_content": None,
        "parsed_response": None,
        "tool_calls": [],
        "loop_detection": None,
        "files_written": [],
        "actions": [],
        "telegram_posts": [],
        "errors": [],
    }
    obj: dict = {}

    try:
        # --- assemble context ---
        requested_notes = read_pending_note_requests(paths)
        user_msg, sizes = assemble_hot_context(cfg, paths, requested_notes, inbox_snap)

        # If last tick fired a loop warning, prepend it so it's the first
        # thing Mako sees this tick. The file is one-shot — we clear it after
        # reading so it doesn't keep nagging him.
        loop_warn_path = paths.state / "loop_warning.md"
        if loop_warn_path.exists():
            warn_text = read_text(loop_warn_path).strip()
            if warn_text:
                user_msg = (f"## ⚠️ LOOP WARNING (from last tick — read this first)\n\n"
                            f"{warn_text}\n\n---\n\n") + user_msg
            try:
                loop_warn_path.unlink()
            except OSError:
                pass

        full_log["user_message"] = user_msg
        full_log["sizes"] = sizes

        max_in = cfg["context"]["max_input_tokens"]
        if sizes["_total_tokens_est"] > max_in:
            # force compaction next tick; truncate aggressively this tick
            write_text(paths.state / "compact_pending.flag", now_iso())
            user_msg = truncate(user_msg, max_in * 4)
            sizes["_truncated"] = True

        # --- system prompt ---
        prompt_file = "compact.md" if args.mode == "compact" else "system.md"
        system = read_text(paths.prompts / prompt_file)
        if not system:
            raise RuntimeError(f"missing system prompt: {prompt_file}")
        full_log["system_prompt"] = system

        # --- LLM run ---
        if args.mode == "compact":
            # Compaction is pure summarisation: keep the legacy single-shot
            # JSON-blob path. No tools needed.
            content, meta, prov_failures, attempts = call_llm_with_fallback(cfg, system, user_msg)
            failures.extend(prov_failures)
            output_chars = len(content)
            full_log["llm_attempts"] = attempts
            full_log["raw_content"] = content
            obj = extract_json(content) or {}
        else:
            # Normal tick: drive the in-tick tool-use loop. The model calls
            # tools (shell, read_file, write_file, http_get, git, cf_api,
            # telegram_post, ask_chris, request_resource) and sees their
            # results before deciding the next step. Loop ends when the model
            # calls the `finish` tool with the final structured payload.
            obj, meta, attempts = run_tool_loop(cfg, paths, system, user_msg, full_log, t_start)
            output_chars = len(json.dumps(obj, ensure_ascii=False))
            # tally tool-call results into action counts so Telegram + metrics
            # still reflect "what happened this tick".
            for tc in full_log.get("tool_calls", []):
                if tc.get("name") in (None, "finish", "<implicit_finish>"):
                    continue
                actions_executed += 1

        # --- validate finish payload ---
        wd_check = obj.get("work_done")
        if not (isinstance(wd_check, str) and wd_check.strip()):
            raise RuntimeError("PARSE_ERROR: work_done missing or empty — "
                               "rejecting tick to preserve INBOX + compaction flag")
        parse_ok = True
        full_log["parsed_response"] = obj

        # --- apply state writes ---
        if isinstance(obj.get("state_md"), str):
            apply_state_md(paths, "STATE.md", obj["state_md"])
        if isinstance(obj.get("next_md"), str):
            apply_state_md(paths, "NEXT.md", obj["next_md"])
        apply_persona_update(paths, obj.get("persona_update"))

        # --- legacy: model-emitted file writes (compaction path only;
        #     tool-use path uses write_file as a tool, not a struct field) ---
        file_results = apply_model_files(cfg, paths, obj.get("files") or [])
        full_log["files_written"] = file_results

        # --- gated actions queue ---
        # Tool-use path: gated actions arrive as obj["gated_actions"].
        # Compaction path: legacy obj["actions"] array (rarely used now).
        action_results: list[dict] = []
        gated_in = obj.get("gated_actions") or obj.get("actions") or []
        for a in gated_in:
            if not isinstance(a, dict):
                continue
            res = dispatch_action(cfg, paths, a)
            action_results.append({"action": a, "result": res})
            if res.get("queued"):
                actions_queued += 1
            else:
                actions_executed += 1
        full_log["actions"] = action_results

        # --- LAST_RESULTS.md for next tick ---
        last_results = render_last_results(tick_n, args.mode, file_results, action_results, meta,
                                           full_log.get("tool_calls"))
        write_text(paths.state / "LAST_RESULTS.md", last_results)

        # --- journal ---
        wd = obj.get("work_done") or "(no work_done)"
        append_journal(paths, tick_n, wd)

        # --- loop detection (after journal append, against now-recent history) ---
        loop_info = detect_loop(paths, wd, cfg)
        full_log["loop_detection"] = loop_info
        if loop_info["loop_detected"]:
            warn = (f"Your last {loop_info['recent_n']} journal entries are too similar to each "
                    f"other ({loop_info['similar_count']} matches at ratio ≥ {loop_info['ratio_threshold']:.2f}, "
                    f"threshold {loop_info['count_threshold']}). You're stuck. This tick MUST be "
                    f"`tick_mode: generative` — brainstorm 3+ NEW backlog ideas, do NOT continue "
                    f"the same task. Sample of similar entries:\n"
                    + "\n".join(f"  - {s}" for s in loop_info.get("sample", [])))
            write_text(paths.state / "loop_warning.md", warn)
            try:
                telegram_send(cfg,
                              f"⚠️ #{tick_n} loop detected · {loop_info['similar_count']}/{loop_info['recent_n']} similar work_done entries · forcing generative next tick",
                              thread_id=cfg["telegram"]["log_thread_id"], label="loop_alert")
            except Exception:
                pass

        # --- requested notes for next tick ---
        write_pending_note_requests(paths, obj.get("request_notes") or [])

        # --- compaction flag ---
        compact_now_flag = bool(obj.get("compact_now"))
        if args.mode == "compact":
            clear_compaction(paths)
        else:
            maybe_set_compaction(paths, tick_n, cfg["tick"]["compact_every_n_ticks"],
                                 compact_now_flag)

        # --- inbox archival (only on parse_ok) ---
        archive_inbox(paths, inbox_snap)

        # --- drift ---
        drift_flag = obj.get("drift_flag")

        # --- telegram ---
        tg_text = (obj.get("telegram") or "").strip() or wd
        tg_cap = int(cfg.get("tick", {}).get("telegram_summary_max_chars", 1000))
        if len(tg_text) > tg_cap:
            tg_text = tg_text[:tg_cap - 30].rstrip() + f" …[truncated · {len(tg_text)} chars]"
        wall = round(time.time() - t_start, 2)
        n_tools = sum(1 for tc in full_log.get("tool_calls", [])
                      if tc.get("name") not in (None, "finish", "<implicit_finish>"))
        header = (f"🦫 #{tick_n} · {datetime.now().strftime('%H:%M')} · "
                  f"{wall}s · {meta.get('slot','?')}/{meta.get('model','?')} · "
                  f"{n_tools}🔧\n")
        body = tg_text
        extra = []
        if actions_queued:
            extra.append(f"queued: {actions_queued}⏸")
        if drift_flag:
            extra.append(f"drift: {drift_flag[:60]}")
        if loop_info.get("loop_detected"):
            extra.append("⚠ loop")
        if extra:
            body = body + "\n" + " · ".join(extra)
        telegram_send(cfg, header + body, thread_id=cfg["telegram"]["log_thread_id"], label="tick")

    except Exception as e:
        if getattr(e, "failures", None):
            failures.extend(str(f) for f in e.failures if str(f) not in failures)
        if getattr(e, "attempts", None):
            full_log["llm_attempts"] = e.attempts
        tb = traceback.format_exc()
        write_text(paths.logs / f"error-{tick_n}.log",
                   f"{now_iso()}\n{tb}\n\nfailures: {failures}\n")
        full_log["errors"].append({"type": type(e).__name__, "message": str(e), "traceback": tb})
        msg = f"🦫 #{tick_n} FAIL · {type(e).__name__}: {str(e)[:200]}"
        try:
            res = telegram_send(cfg, msg, thread_id=cfg["telegram"]["log_thread_id"], label="fail")
            full_log["telegram_posts"].append({"text": msg, "thread_id": cfg["telegram"]["log_thread_id"], "result": res})
        except Exception:
            pass
        record_metric(paths, {
            "ts": now_iso(), "tick_n": tick_n, "mode": args.mode,
            "wall_s": round(time.time() - t_start, 2),
            "provider_used": meta.get("slot", ""), "model_used": meta.get("model", ""),
            "input_tokens_est": "", "output_chars": output_chars,
            "actions_count": "", "actions_executed": actions_executed,
            "actions_queued": actions_queued, "parse_ok": parse_ok,
            "drift_flag": drift_flag or "", "compact_now": compact_now_flag,
            "failures": " | ".join(failures + [f"{type(e).__name__}: {e}"])[:500],
        })
        if cfg.get("logging", {}).get("full_payload", True):
            full_log["ended_at"] = now_iso()
            full_log["wall_s"] = round(time.time() - t_start, 2)
            full_log["outcome"] = "fail"
            write_full_log(paths, "ticks", tick_n, full_log)
        return 1

    # Real token counts from the provider response, if available.
    in_tok = meta.get("prompt_eval_count") or (meta.get("usage") or {}).get("prompt_tokens") or ""
    out_tok = meta.get("eval_count") or (meta.get("usage") or {}).get("completion_tokens") or ""
    tool_steps_n = sum(1 for tc in full_log.get("tool_calls", [])
                       if tc.get("name") not in (None, "finish", "<implicit_finish>"))
    loop_score = (full_log.get("loop_detection") or {}).get("similar_count", "")

    record_metric(paths, {
        "ts": now_iso(), "tick_n": tick_n, "mode": args.mode,
        "wall_s": round(time.time() - t_start, 2),
        "provider_used": meta.get("slot", ""), "model_used": meta.get("model", ""),
        "input_tokens_est": sizes.get("_total_tokens_est", ""),
        "input_tokens": in_tok, "output_tokens": out_tok,
        "output_chars": output_chars,
        "progress_confidence": "",   # deprecated: model no longer reports
        "loop_score": loop_score,
        "tool_steps": tool_steps_n,
        "actions_count": len((obj.get("gated_actions") or obj.get("actions") or [])),
        "actions_executed": actions_executed,
        "actions_queued": actions_queued,
        "parse_ok": parse_ok,
        "drift_flag": drift_flag or "",
        "compact_now": compact_now_flag,
        "failures": " | ".join(failures)[:500],
    })
    if cfg.get("logging", {}).get("full_payload", True):
        full_log["ended_at"] = now_iso()
        full_log["wall_s"] = round(time.time() - t_start, 2)
        full_log["outcome"] = "ok"
        write_full_log(paths, "ticks", tick_n, full_log)
    return 0


# --------------------------- last results ------------------------------

def render_last_results(tick_n: int, mode: str, file_results: list[dict],
                        action_results: list[dict], meta: dict,
                        tool_calls: list[dict] | None = None) -> str:
    lines = [f"# LAST_RESULTS — tick #{tick_n} ({mode}) — {now_iso()}", ""]
    lines.append(f"_provider: {meta.get('slot','?')} / {meta.get('model','?')} · wall {meta.get('wall_s','?')}s_")
    lines.append("")

    # Tool-use loop trace — what the model actually did inside this tick.
    real_tools = [tc for tc in (tool_calls or [])
                  if tc.get("name") not in (None, "finish", "<implicit_finish>")]
    if real_tools:
        lines.append("## tool-loop trace")
        for i, tc in enumerate(real_tools, 1):
            name = tc.get("name", "?")
            args = tc.get("args") or {}
            r = tc.get("result") or {}
            ok = "✅" if r.get("ok") else "❌"
            head = f"### {i}. {name} · {ok}"
            lines.append(head)
            if name == "shell":
                lines.append(f"cmd: `{str(args.get('cmd',''))[:200]}`  rc={r.get('rc','?')}")
                if r.get("stdout"):
                    lines.append("stdout:\n```\n" + r["stdout"] + "\n```")
                if r.get("stderr"):
                    lines.append("stderr:\n```\n" + r["stderr"] + "\n```")
                if not r.get("ok") and r.get("error"):
                    lines.append(f"error: {r['error']}")
            elif name == "http_get":
                lines.append(f"url: {str(args.get('url',''))[:200]}  status={r.get('status','?')}  size={r.get('size_bytes','?')}B")
                if r.get("body"):
                    lines.append("body:\n```\n" + r["body"] + "\n```")
                if not r.get("ok") and r.get("error"):
                    lines.append(f"error: {r['error']}")
            elif name == "read_file":
                if r.get("ok"):
                    lines.append(f"path: {r.get('path','?')}")
                    if r.get("body") is not None:
                        lines.append("body:\n```\n" + r["body"] + "\n```")
                else:
                    lines.append(f"error: {r.get('error','?')}")
            elif name == "write_file":
                if r.get("ok"):
                    lines.append(f"wrote: {r.get('path','?')} ({r.get('bytes','?')}B, {r.get('mode','write')})")
                else:
                    lines.append(f"error: {r.get('error','?')}")
            elif name == "git":
                lines.append(f"cmd: git {str(args.get('cmd',''))[:200]}  rc={r.get('rc','?')}")
                if r.get("stdout"):
                    lines.append("stdout:\n```\n" + r["stdout"] + "\n```")
                if r.get("stderr"):
                    lines.append("stderr:\n```\n" + r["stderr"] + "\n```")
            elif name == "cf_api":
                lines.append(f"{args.get('method','?')} {args.get('path','?')}  status={r.get('status','?')}")
                if r.get("body"):
                    lines.append("body:\n```\n" + str(r["body"])[:600] + "\n```")
            elif name in ("telegram_post", "ask_chris"):
                lines.append("delivered" if r.get("ok") else f"failed: {r.get('error','?')}")
            elif name == "request_resource":
                lines.append(f"queued resource request id={r.get('id','?')}")
            else:
                lines.append("```json\n" + json.dumps(r, ensure_ascii=False)[:400] + "\n```")
            lines.append("")

    if file_results:
        lines.append("## file writes (legacy)")
        for r in file_results:
            if r.get("ok"):
                lines.append(f"- ✅ {r['path']} ({r['bytes']}B, {r['mode']})")
            else:
                lines.append(f"- ❌ {r.get('error','?')}")
        lines.append("")

    if action_results:
        lines.append("## gated/queued actions")
        for i, item in enumerate(action_results, 1):
            a, r = item["action"], item["result"]
            t = a.get("type", "?")
            head = f"### {i}. {t}"
            if r.get("queued"):
                lines.append(head + f" · ⏸ queued for approval (id: {r.get('id','?')})")
                lines.append(f"```json\n{json.dumps(a, ensure_ascii=False)[:600]}\n```")
                continue
            ok = r.get("ok")
            lines.append(head + (" · ✅" if ok else " · ❌"))
            # compact one-screen result
            if t == "shell":
                lines.append(f"cmd: `{a.get('cmd','')[:200]}`  rc={r.get('rc','?')}")
                if r.get("stdout"):
                    lines.append("stdout:\n```\n" + r["stdout"] + "\n```")
                if r.get("stderr"):
                    lines.append("stderr:\n```\n" + r["stderr"] + "\n```")
                if not ok and r.get("error"):
                    lines.append(f"error: {r['error']}")
            elif t == "http_get":
                lines.append(f"url: {a.get('url','')[:200]}  status={r.get('status','?')}  size={r.get('size_bytes','?')}B")
                if r.get("body"):
                    lines.append("body:\n```\n" + r["body"] + "\n```")
                if not ok and r.get("error"):
                    lines.append(f"error: {r['error']}")
            elif t in ("write_file", "read_file"):
                if ok:
                    lines.append(f"path: {r.get('path','?')}")
                    if r.get("body") is not None:
                        lines.append("body:\n```\n" + r["body"] + "\n```")
                else:
                    lines.append(f"error: {r.get('error','?')}")
            elif t == "git":
                lines.append(f"cmd: git {a.get('cmd','')[:200]}  rc={r.get('rc','?')}")
                if r.get("stdout"):
                    lines.append("stdout:\n```\n" + r["stdout"] + "\n```")
                if r.get("stderr"):
                    lines.append("stderr:\n```\n" + r["stderr"] + "\n```")
            elif t in ("telegram_post", "ask_chris"):
                lines.append(("delivered" if ok else f"failed: {r.get('error','?')}"))
            else:
                lines.append("```json\n" + json.dumps(r, ensure_ascii=False)[:400] + "\n```")
            lines.append("")
    elif not real_tools and not file_results:
        lines.append("_no actions emitted, no tools used_")
    return "\n".join(lines) + "\n"


if __name__ == "__main__":
    sys.exit(main())
