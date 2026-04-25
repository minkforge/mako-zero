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

    add("MISSION.md", read_text(paths.state / "MISSION.md"))
    if inbox_snap and inbox_snap.get("content", "").strip():
        add("⚡ INBOX FROM CHRIS — read carefully; wrapper will archive after this tick",
            inbox_snap["content"])
    add("CAPABILITIES.md", read_text(paths.state / "CAPABILITIES.md"))
    add("STATE.md", read_text(paths.state / "STATE.md"))
    add("NEXT.md", read_text(paths.state / "NEXT.md"))
    add("JOURNAL.md (last %d lines)" % cfg["context"]["recent_journal_lines"],
        tail_lines(paths.state / "JOURNAL.md", cfg["context"]["recent_journal_lines"]))
    add("notes/INDEX.md", read_text(paths.notes / "INDEX.md"))
    add("outbox/blog/drafts/ (scribe's drafts — read full text via read_file before publishing)",
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


def call_ollama(spec: dict, system: str, user: str) -> tuple[str, dict]:
    base = spec["base_url"].rstrip("/")
    url = f"{base}/api/chat"
    headers = {"Content-Type": "application/json"}
    if spec.get("api_key"):
        headers["Authorization"] = f"Bearer {spec['api_key']}"
    body = {
        "model": spec["model"],
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        "stream": False,
        "options": {
            "num_predict": spec.get("num_predict", 8000),
        },
    }
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
    content = (data.get("message") or {}).get("content")
    if not content:
        raise ProviderError(f"ollama empty content: {str(data)[:300]}")
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
    return content, meta


def call_openai_compat(spec: dict, system: str, user: str) -> tuple[str, dict]:
    base = spec["base_url"].rstrip("/")
    url = f"{base}/chat/completions"
    headers = {"Content-Type": "application/json"}
    if spec.get("api_key"):
        headers["Authorization"] = f"Bearer {spec['api_key']}"
    body = {
        "model": spec["model"],
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        "max_tokens": spec.get("num_predict", 6000),
        "stream": False,
    }
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
        content = data["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError):
        raise ProviderError(f"openai_compat malformed: {str(data)[:300]}")
    if not content:
        raise ProviderError("openai_compat empty content")
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
    return content, meta


def call_provider(spec: dict, system: str, user: str) -> tuple[str, dict]:
    t = spec["type"]
    if t == "ollama":
        return call_ollama(spec, system, user)
    if t == "openai_compat":
        return call_openai_compat(spec, system, user)
    raise ProviderError(f"unknown provider type {t}")


def call_llm_with_fallback(cfg: dict, system: str, user: str) -> tuple[str, dict, list[str], list[dict]]:
    """Returns (content, meta, failure_strings, attempt_records).
    attempt_records contains the request/response for every attempt
    (success and failure) so the per-tick log captures the full picture."""
    failures: list[str] = []
    attempts: list[dict] = []
    for slot in ("primary", "fallback"):
        spec = cfg["llm"][slot]
        if not spec.get("base_url") or not spec.get("model"):
            failures.append(f"{slot}: not configured")
            attempts.append({"slot": slot, "ok": False, "error": "not configured"})
            continue
        try:
            content, meta = call_provider(spec, system, user)
            meta["slot"] = slot
            attempts.append({"slot": slot, "ok": True, "meta": meta, "content": content})
            return content, meta, failures, attempts
        except (ProviderError, requests.Timeout, requests.ConnectionError) as e:
            failures.append(f"{slot}({spec['type']}): {e}")
            attempts.append({"slot": slot, "ok": False, "type": spec["type"],
                             "model": spec.get("model"), "error": str(e)[:1000]})
            continue
    raise ProviderError("all providers failed: " + "; ".join(failures))


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


def exec_telegram_post(cfg: dict, action: dict) -> dict:
    thread = action.get("thread", cfg["telegram"]["log_thread_id"])
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


def queue_gated_action(cfg: dict, paths: Paths, action: dict) -> dict:
    qid = f"q{int(time.time()*1000)}"
    rec = {"id": qid, "queued_at": now_iso(), "action": action}
    append_text(paths.pending / "pending_actions.jsonl", json.dumps(rec) + "\n")
    try:
        telegram_send(cfg, _gated_summary(qid, action),
                      thread_id=gated_action_thread(cfg), label="gated")
    except Exception:
        pass  # never let notification failure break queueing
    return {"ok": True, "queued": True, "id": qid, "type": action.get("type")}


# ----------------- non-gated dispatcher --------------------------------

NON_GATED = {"shell", "http_get", "write_file", "read_file", "git",
             "telegram_post", "ask_chris"}
GATED = {"email_send", "cf_api", "http_post", "http_put", "http_delete", "spend"}


def dispatch_action(cfg: dict, paths: Paths, action: dict) -> dict:
    t = str(action.get("type", "")).strip()
    needs_approval = bool(action.get("needs_approval", False))
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
    if t == "telegram_post":
        return exec_telegram_post(cfg, action)
    if t == "ask_chris":
        return exec_ask_chris(cfg, action)
    return {"ok": False, "error": f"unknown action type: {t!r}"}


# ------------------------- file writes (model) -------------------------

def apply_model_files(cfg: dict, paths: Paths, files: list[dict]) -> list[dict]:
    results = []
    for entry in files or []:
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


def telegram_send(cfg: dict, text: str, thread_id: int | None = None, label: str = "") -> dict:
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
    "input_tokens_est", "output_chars", "actions_count",
    "actions_executed", "actions_queued",
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
        "files_written": [],
        "actions": [],
        "telegram_posts": [],
        "errors": [],
    }

    try:
        # --- assemble context ---
        requested_notes = read_pending_note_requests(paths)
        user_msg, sizes = assemble_hot_context(cfg, paths, requested_notes, inbox_snap)
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

        # --- LLM call ---
        content, meta, prov_failures, attempts = call_llm_with_fallback(cfg, system, user_msg)
        failures.extend(prov_failures)
        output_chars = len(content)
        full_log["llm_attempts"] = attempts
        full_log["raw_content"] = content

        # --- parse ---
        obj = extract_json(content)
        if obj is None:
            raise RuntimeError("PARSE_ERROR: no JSON object in response")
        parse_ok = True
        full_log["parsed_response"] = obj

        # --- apply state writes ---
        if isinstance(obj.get("state_md"), str):
            apply_state_md(paths, "STATE.md", obj["state_md"])
        if isinstance(obj.get("next_md"), str):
            apply_state_md(paths, "NEXT.md", obj["next_md"])
        apply_persona_update(paths, obj.get("persona_update"))

        # --- model-emitted file writes ---
        file_results = apply_model_files(cfg, paths, obj.get("files") or [])
        full_log["files_written"] = file_results

        # --- actions ---
        action_results: list[dict] = []
        for a in obj.get("actions") or []:
            res = dispatch_action(cfg, paths, a)
            action_results.append({"action": a, "result": res})
            if res.get("queued"):
                actions_queued += 1
            else:
                actions_executed += 1
        full_log["actions"] = action_results

        # --- LAST_RESULTS.md for next tick ---
        last_results = render_last_results(tick_n, args.mode, file_results, action_results, meta)
        write_text(paths.state / "LAST_RESULTS.md", last_results)

        # --- journal ---
        wd = obj.get("work_done") or "(no work_done)"
        append_journal(paths, tick_n, wd)

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
        tg_text = tg_text[:300]
        wall = round(time.time() - t_start, 2)
        header = (f"🦫 #{tick_n} · {datetime.now().strftime('%H:%M')} · "
                  f"{wall}s · {meta.get('slot','?')}/{meta.get('model','?')}\n")
        body = tg_text
        extra = []
        if actions_executed or actions_queued:
            extra.append(f"acts: {actions_executed}✓ {actions_queued}⏸")
        if drift_flag:
            extra.append(f"drift: {drift_flag[:60]}")
        if extra:
            body = body + "\n" + " · ".join(extra)
        telegram_send(cfg, header + body, thread_id=cfg["telegram"]["log_thread_id"], label="tick")

    except Exception as e:
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

    record_metric(paths, {
        "ts": now_iso(), "tick_n": tick_n, "mode": args.mode,
        "wall_s": round(time.time() - t_start, 2),
        "provider_used": meta.get("slot", ""), "model_used": meta.get("model", ""),
        "input_tokens_est": sizes.get("_total_tokens_est", ""),
        "output_chars": output_chars,
        "actions_count": len((obj.get("actions") or [])),
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
                        action_results: list[dict], meta: dict) -> str:
    lines = [f"# LAST_RESULTS — tick #{tick_n} ({mode}) — {now_iso()}", ""]
    lines.append(f"_provider: {meta.get('slot','?')} / {meta.get('model','?')} · wall {meta.get('wall_s','?')}s_")
    lines.append("")

    if file_results:
        lines.append("## file writes")
        for r in file_results:
            if r.get("ok"):
                lines.append(f"- ✅ {r['path']} ({r['bytes']}B, {r['mode']})")
            else:
                lines.append(f"- ❌ {r.get('error','?')}")
        lines.append("")

    if action_results:
        lines.append("## actions")
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
    else:
        lines.append("_no actions emitted_")
    return "\n".join(lines) + "\n"


if __name__ == "__main__":
    sys.exit(main())
