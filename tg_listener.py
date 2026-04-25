#!/usr/bin/env python3
"""Telegram inbound listener — runs as a thread inside supervisor.py.

Long-polls getUpdates against your bot. Any non-command text message
in the configured chat_id gets appended to state/INBOX.md, so Mako
sees it on his next tick. If the Telegram message is a reply, the
original text is included as context.

Filters:
- Messages from the bot itself (loopback) are skipped.
- Messages starting with "/" are treated as bot commands and skipped.
- Empty / non-text messages skipped.
- Only the configured chat_id is honoured.

Offset is persisted to state/telegram_offset.txt so we don't replay
old messages across restarts.
"""
from __future__ import annotations

import json
import threading
import time
from datetime import datetime
from pathlib import Path

import requests


def _log(msg: str) -> None:
    print(msg, flush=True)


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
                if not text or text.startswith("/"):
                    continue
                if (msg.get("from") or {}).get("is_bot"):
                    continue

                thread_id = msg.get("message_thread_id", 0) or "main"
                ts = datetime.fromtimestamp(msg.get("date", time.time())).strftime("%H:%M")

                reply_ctx = ""
                rt = msg.get("reply_to_message")
                if rt and rt.get("text"):
                    rt_text = rt["text"][:200].replace("\n", " ")
                    reply_ctx = f' (reply to: "{rt_text}")'

                entry = f"\n[telegram · thread {thread_id} · {ts}{reply_ctx}]\n{text}\n"

                state_dir.mkdir(parents=True, exist_ok=True)
                with inbox_path.open("a", encoding="utf-8") as f:
                    f.write(entry)
                wrote_any = True
                log(f"[tg-listener] inbox += {len(text)} chars from thread {thread_id}")

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
