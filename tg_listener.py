#!/usr/bin/env python3
"""Telegram inbound listener — runs as a thread inside supervisor.py.

Long-polls getUpdates against your bot. Inbound messages are routed:

1. Slash-prefixed messages (/cfg, /restart, /status, /inbox, /help)
   → dispatched to cfg_cmd.handle_command. Reply posted back to the
   same thread. Not appended to INBOX.

2. Replies to NEEDS APPROVAL pings → parsed for approve/reject intent
   and the action is executed or rejected via tick.execute_gated_action.

3. Anything else → appended to state/INBOX.md so Mako sees it on his
   next tick. If the Telegram message is a reply, the parent text is
   included as context.

Filters:
- Messages from the bot itself (loopback) are skipped.
- Empty / non-text messages skipped.
- Only the configured chat_id is honoured.

Offset is persisted to state/telegram_offset.txt so we don't replay
old messages across restarts.
"""
from __future__ import annotations

import json
import re
import threading
import time
from datetime import datetime
from pathlib import Path

import requests


def _log(msg: str) -> None:
    print(msg, flush=True)


# Approval-reply detection
QID_RE = re.compile(r'\b(q\d{8,})\b')      # qID format from queue_gated_action
APPROVE_TOKENS = {
    "approve", "approved", "yes", "y", "ok", "okay", "go", "ship",
    "👍", "✓", "✅", "do it", "sure", "send it",
}
REJECT_TOKENS = {
    "reject", "rejected", "no", "n", "nope", "cancel", "drop", "kill",
    "👎", "✗", "❌", "don't", "do not",
}


def _detect_approval_intent(text: str) -> tuple[str, str]:
    """Returns (intent, reason). intent in {'approve','reject','none'}.
    reason is the trailing reason text after the keyword (may be empty)."""
    t = text.strip()
    if not t:
        return ("none", "")
    low = t.lower()
    if low in APPROVE_TOKENS:
        return ("approve", "")
    if low in REJECT_TOKENS:
        return ("reject", "")
    parts = t.split(None, 1)
    first = parts[0].lower()
    rest = parts[1] if len(parts) > 1 else ""
    if first in {"approve", "approved", "ok", "okay", "yes", "ship", "go"}:
        return ("approve", rest)
    if first in {"reject", "rejected", "no", "kill", "cancel", "drop", "nope"}:
        return ("reject", rest)
    return ("none", text)


def _find_pending(paths_root: Path, qid: str) -> tuple[dict | None, list[str]]:
    """Returns (matching_record, lines_to_keep). Caller writes kept lines back."""
    p = paths_root / "pending" / "pending_actions.jsonl"
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


def _write_pending(paths_root: Path, kept: list[str]) -> None:
    p = paths_root / "pending" / "pending_actions.jsonl"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("\n".join(kept) + ("\n" if kept else ""), encoding="utf-8")


def _append_decision(paths_root: Path, decision: dict) -> None:
    p = paths_root / "pending" / "decisions.jsonl"
    p.parent.mkdir(parents=True, exist_ok=True)
    with p.open("a", encoding="utf-8") as f:
        f.write(json.dumps(decision, ensure_ascii=False) + "\n")


def _append_inbox(paths_root: Path, entry: str) -> None:
    p = paths_root / "state" / "INBOX.md"
    p.parent.mkdir(parents=True, exist_ok=True)
    with p.open("a", encoding="utf-8") as f:
        f.write(entry)


def _audit(paths_root: Path, entry: dict) -> None:
    """Append a JSONL audit row. Used for: command invocations, resource
    request updates, approvals, rejections. Never includes secrets — the
    callers are responsible for redaction."""
    import time as _time, json as _json
    entry.setdefault("ts", datetime.now().isoformat(timespec="seconds"))
    p = paths_root / "logs" / "audit.jsonl"
    p.parent.mkdir(parents=True, exist_ok=True)
    with p.open("a", encoding="utf-8") as f:
        f.write(_json.dumps(entry, ensure_ascii=False) + "\n")


def _resource_update(paths_root: Path, rid: str, status: str, reply_text: str) -> None:
    """Update pending/resources.jsonl: rewrite the matching record's status
    and append a discussion turn."""
    p = paths_root / "pending" / "resources.jsonl"
    if not p.exists():
        return
    out_lines: list[str] = []
    for line in p.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            rec = json.loads(line)
        except json.JSONDecodeError:
            out_lines.append(line)
            continue
        if rec.get("id") == rid:
            if status in ("granted", "rejected"):
                rec["status"] = status
            else:
                # Keep status open; record the discussion turn.
                rec.setdefault("discussion", []).append({
                    "ts": datetime.now().isoformat(timespec="seconds"),
                    "by": "chris", "text": reply_text[:1000],
                })
            out_lines.append(json.dumps(rec, ensure_ascii=False))
        else:
            out_lines.append(line)
    p.write_text("\n".join(out_lines) + ("\n" if out_lines else ""), encoding="utf-8")


def _result_summary(action_type: str, result: dict) -> str:
    if not result.get("ok"):
        return f"failed: {result.get('error') or result.get('body','?')[:200]}"
    if action_type == "email_send":
        return f"sent to {result.get('to','?')}"
    if action_type == "cf_api":
        return f"HTTP {result.get('status','?')}"
    if action_type in ("http_post", "http_put", "http_delete"):
        return f"HTTP {result.get('status','?')}"
    if action_type == "spend":
        return f"recorded £{result.get('amount_pence',0)/100:.2f}"
    return "ok"


def _handle_approval(cfg: dict, paths_root: Path, qid: str, intent: str,
                     reason: str, reply_thread: int | None, log) -> None:
    # Lazy-import tick to avoid circular imports at module load
    import tick as t_mod

    pending, kept = _find_pending(paths_root, qid)
    if pending is None:
        msg = f"⚠ {qid} — not found in pending. Already decided, or expired?"
        t_mod.telegram_send(cfg, msg, thread_id=reply_thread, label="approval-miss")
        log(f"[tg-listener] approval-miss: {qid} not in pending")
        return

    action = pending.get("action") or {}
    a_type = action.get("type", "?")

    if intent == "approve":
        log(f"[tg-listener] executing {qid} ({a_type})")
        # We need a Paths-shaped object for tick functions
        paths = t_mod.Paths(cfg)
        result = t_mod.execute_gated_action(cfg, paths, action)
        decision = {
            "id": qid, "decided_at": t_mod.now_iso(),
            "decision": "approve", "by": "telegram_reply",
            "reason": reason or "", "action": action, "result": result,
        }
        _append_decision(paths_root, decision)
        _write_pending(paths_root, kept)
        try:
            _audit(paths_root, {"kind": "approval", "decision": "approve",
                                "by": "telegram", "qid": qid, "type": a_type,
                                "ok": bool(result.get("ok"))})
        except Exception:
            pass

        ok_marker = "✅" if result.get("ok") else "❌"
        tg = (f"{ok_marker} {qid} · {a_type}\n"
              f"{_result_summary(a_type, result)}")
        t_mod.telegram_send(cfg, tg, thread_id=reply_thread, label="approval-result")

        # Tell Mako so he can adapt
        _append_inbox(paths_root, (
            f"\n[approval · {qid} · {a_type}] {ok_marker} executed\n"
            f"action: {json.dumps(action, ensure_ascii=False)[:600]}\n"
            f"result: {json.dumps(result, ensure_ascii=False)[:500]}\n"
        ))

    elif intent == "reject":
        decision = {
            "id": qid, "decided_at": t_mod.now_iso(),
            "decision": "reject", "by": "telegram_reply",
            "reason": reason or "", "action": action,
        }
        _append_decision(paths_root, decision)
        _write_pending(paths_root, kept)
        try:
            _audit(paths_root, {"kind": "approval", "decision": "reject",
                                "by": "telegram", "qid": qid, "type": a_type,
                                "reason": reason[:200]})
        except Exception:
            pass

        msg = f"❎ {qid} · {a_type} · rejected"
        if reason:
            msg += f"\nreason: {reason[:300]}"
        t_mod.telegram_send(cfg, msg, thread_id=reply_thread, label="approval-reject")

        _append_inbox(paths_root, (
            f"\n[approval · {qid} · {a_type}] ❎ rejected by Chris\n"
            f"reason: {reason or '(no reason given)'}\n"
            f"action was: {json.dumps(action, ensure_ascii=False)[:400]}\n"
        ))


def telegram_poller(cfg: dict, paths_root: Path, shutdown: threading.Event,
                    log=_log) -> None:
    bot_token = cfg["telegram"].get("bot_token", "")
    chat_id = str(cfg["telegram"].get("chat_id", ""))
    if not bot_token or not chat_id:
        log("[tg-listener] no bot_token / chat_id configured; listener disabled")
        return

    state_dir = paths_root / "state"
    inbox_path = state_dir / "INBOX.md"
    offset_path = state_dir / "telegram_offset.txt"

    try:
        offset = int((offset_path.read_text(encoding="utf-8") or "0").strip())
    except (FileNotFoundError, ValueError):
        offset = 0

    log(f"[tg-listener] starting (chat_id={chat_id}, offset={offset})")

    while not shutdown.is_set():
        try:
            r = requests.get(
                f"https://api.telegram.org/bot{bot_token}/getUpdates",
                params={
                    "offset": offset,
                    "timeout": 25,
                    "allowed_updates": json.dumps(["message"]),
                },
                timeout=35,
            )
            if r.status_code != 200:
                log(f"[tg-listener] HTTP {r.status_code}: {r.text[:200]}")
                shutdown.wait(timeout=10)
                continue
            data = r.json()
            if not data.get("ok"):
                log(f"[tg-listener] not ok: {str(data)[:200]}")
                shutdown.wait(timeout=10)
                continue

            updates = data.get("result", []) or []
            wrote_any = False
            for update in updates:
                new_offset = update.get("update_id", offset - 1) + 1
                offset = max(offset, new_offset)

                msg = update.get("message")
                if not msg:
                    continue
                if str(msg.get("chat", {}).get("id")) != chat_id:
                    continue
                text = (msg.get("text") or "").strip()
                if not text:
                    continue
                if (msg.get("from") or {}).get("is_bot"):
                    continue

                thread_id_raw = msg.get("message_thread_id", 0)
                thread_id = thread_id_raw or "main"
                ts = datetime.fromtimestamp(msg.get("date", time.time())).strftime("%H:%M")

                # Slash-prefixed → command dispatch
                if text.startswith("/"):
                    try:
                        import cfg_cmd
                        import tick as t_mod
                        cfg_path = paths_root / "config.yaml"
                        reply = cfg_cmd.handle_command(text, cfg_path, paths_root)
                    except Exception as e:
                        reply = f"❌ command error: {type(e).__name__}: {e}"
                    if reply is not None:
                        try:
                            t_mod.telegram_send(cfg, reply,
                                                thread_id=thread_id_raw or None,
                                                label="cmd")
                        except Exception as e:
                            log(f"[tg-listener] reply send failed: {e!r}")
                        log(f"[tg-listener] /cmd: {text[:60]} → {reply[:80]!r}")
                        # AUDIT — every command that produces a reply.
                        try:
                            _audit(paths_root, {
                                "kind": "command", "by": "telegram",
                                "thread": thread_id, "cmd": text[:300],
                                "reply": reply[:400],
                            })
                        except Exception:
                            pass
                        continue
                    # Unrecognised /cmd — fall through and let it land in INBOX

                rt = msg.get("reply_to_message") or {}
                rt_text = (rt.get("text") or "")

                # Resource-request reply detection: if Chris is replying to a
                # `📨 R… · REQUEST · …` message, route it as a resource update
                # — captured to INBOX with the rid context AND audited.
                if rt_text and ("REQUEST" in rt_text and "📨" in rt_text):
                    rid_match = re.search(r"\b(r\d{8,})\b", rt_text)
                    if rid_match:
                        rid = rid_match.group(1)
                        intent_text = text.lower().strip()
                        granted = any(k in intent_text for k in
                                      ("approved", "approve", "granted", "yes",
                                       "do it", "go ahead", "ok", "👍"))
                        rejected = any(k in intent_text for k in
                                       ("rejected", "reject", "denied", "no",
                                        "👎", "don't", "do not"))
                        status = "granted" if granted else ("rejected" if rejected else "discussion")
                        try:
                            _resource_update(paths_root, rid, status, text)
                        except Exception as e:
                            log(f"[tg-listener] resource update failed: {e!r}")
                        marker = ("✅ granted" if status == "granted"
                                  else "❎ rejected" if status == "rejected"
                                  else "💬 discussion")
                        _append_inbox(paths_root, (
                            f"\n[request · {rid}] {marker}\n"
                            f"chris said: {text}\n"
                        ))
                        try:
                            import tick as t_mod
                            t_mod.telegram_send(cfg, f"{marker} · {rid} captured",
                                                thread_id=thread_id_raw or None,
                                                label="request-update")
                        except Exception:
                            pass
                        try:
                            _audit(paths_root, {
                                "kind": "request_update", "by": "telegram",
                                "rid": rid, "status": status,
                                "text": text[:400],
                            })
                        except Exception:
                            pass
                        wrote_any = True
                        continue

                # Approval-flow short-circuit: if this is a reply to a
                # NEEDS APPROVAL ping, parse qID and intent.
                if rt_text and "NEEDS APPROVAL" in rt_text:
                    qid_match = QID_RE.search(rt_text)
                    if qid_match:
                        qid = qid_match.group(1)
                        intent, reason = _detect_approval_intent(text)
                        if intent != "none":
                            try:
                                _handle_approval(cfg, paths_root, qid, intent,
                                                 reason, thread_id_raw or None, log)
                            except Exception as e:
                                log(f"[tg-listener] approval handler error: {e!r}")
                            wrote_any = True
                            continue
                        # else: fall through — treat as a comment, append to INBOX

                reply_ctx = ""
                if rt_text:
                    reply_ctx = f' (reply to: "{rt_text[:200].replace(chr(10), " ")}")'
                entry = f"\n[telegram · thread {thread_id} · {ts}{reply_ctx}]\n{text}\n"

                # Route meta-thread inbound to META_INBOX.md (steering for the
                # meta loop, not the worker). Everything else → INBOX.md.
                meta_thread_id = cfg.get("telegram", {}).get("meta_thread_id") or 0
                if meta_thread_id and thread_id_raw == int(meta_thread_id):
                    target = state_dir / "META_INBOX.md"
                    audit_kind = "meta-steering"
                    label = "meta-inbox"
                else:
                    target = inbox_path
                    audit_kind = "steering"
                    label = "inbox"

                state_dir.mkdir(parents=True, exist_ok=True)
                with target.open("a", encoding="utf-8") as f:
                    f.write(entry)
                wrote_any = True
                log(f"[tg-listener] {label} += {len(text)} chars from thread {thread_id}")
                try:
                    _audit(paths_root, {"kind": audit_kind, "by": "telegram",
                                        "thread": str(thread_id),
                                        "text": text[:500]})
                except Exception:
                    pass

            # Persist the offset even on empty long-poll cycles so we don't
            # re-poll the same window on restart.
            try:
                offset_path.write_text(str(offset), encoding="utf-8")
            except OSError as e:
                log(f"[tg-listener] could not persist offset: {e!r}")

            if not wrote_any:
                # Empty result — Telegram returned because no new messages
                # arrived in the long-poll window. Loop straight back.
                continue

        except (requests.Timeout, requests.ConnectionError) as e:
            log(f"[tg-listener] network: {type(e).__name__}: {str(e)[:200]}")
            shutdown.wait(timeout=10)
        except requests.RequestException as e:
            log(f"[tg-listener] request error: {e!r}")
            shutdown.wait(timeout=10)
        except Exception as e:
            log(f"[tg-listener] unexpected: {e!r}")
            shutdown.wait(timeout=10)

    log("[tg-listener] stopping")
