#!/usr/bin/env python3
"""Telegram-driven config commands.

Called from tg_listener when an inbound message starts with `/cfg`,
`/restart`, `/status`, `/inbox`, `/help`. Returns a short string to
post back to the Telegram thread the command came from.

Design notes:
- Surgical line-rewrite of config.yaml — preserves comments and the
  user's hand-edits. Only flat scalar leaves are editable via Telegram
  (lists/dicts edit via SSH).
- Secret keys (api_key, bot_token, token, password) cannot be read or
  set from Telegram. Edit config.yaml directly for those.
- A restart is detached via `start_new_session=True` so this process
  surviving its own restart is fine — the child outlives systemd's
  SIGTERM, sleeps briefly, then runs `systemctl restart mako-zero`.
"""
from __future__ import annotations

import re
import shutil
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import yaml


# Keys that the Telegram surface must never read or write.
SECRET_PATTERNS = ("api_key", "bot_token", "smtp_password", "secret")
# Cloudflare's token/account_id and chat_id sit in their own block —
# we treat anything under `cloudflare.` and `fastmail.` as secret-adjacent.
SECRET_PREFIXES = ("cloudflare.", "fastmail.")
# Keys that require a restart to take effect (loaded once at supervisor
# boot rather than per-tick).
RESTART_PREFIXES = ("paths.", "llm.", "telegram.", "supervisor.",
                    "scribe.enabled", "logging.")
REDACT = "[REDACTED]"


def _is_secret_key(dotted: str) -> bool:
    if dotted.startswith(SECRET_PREFIXES):
        return True
    leaf = dotted.rsplit(".", 1)[-1]
    return any(p in leaf for p in SECRET_PATTERNS)


def _needs_restart(dotted: str) -> bool:
    return dotted.startswith(RESTART_PREFIXES)


# ----------------------- yaml line-edit --------------------------------

_KEY_RE = re.compile(r"^([A-Za-z_][A-Za-z0-9_]*)\s*:\s*(.*)$")


def find_key_line(text: str, dotted: str) -> tuple[int, int, str, str] | None:
    """Locate the line for a dotted YAML key under standard 2-space indent.
    Returns (line_index, line_indent, current_value_str, comment) or None.

    YAML semantics: when a key is duplicated, the LAST occurrence wins.
    We mirror that — scan the whole file and return the last match — so
    edits land on the value that's actually in effect.

    Multi-line values (`|`, `>`, inline lists/dicts) return their literal
    value string; caller decides whether to refuse the edit.
    """
    parts = dotted.split(".")
    lines = text.splitlines()
    cur_path: list[str] = []
    last_hit: tuple[int, int, str, str] | None = None

    for i, line in enumerate(lines):
        stripped = line.lstrip()
        if not stripped or stripped.startswith("#"):
            continue
        indent = len(line) - len(stripped)
        # 2-space indent assumption — drop any path element deeper than this
        while cur_path and len(cur_path) * 2 > indent:
            cur_path.pop()
        m = _KEY_RE.match(stripped)
        if not m:
            continue
        key, rest = m.group(1), m.group(2).rstrip()
        full = cur_path + [key]
        if full == parts:
            cm = re.match(r"^(.*?)(\s*#.*)?$", rest)
            value_str = (cm.group(1) or "").rstrip()
            comment = cm.group(2) or ""
            last_hit = (i, indent, value_str, comment)
            # Don't return — continue in case of YAML duplicate-key
        if rest == "":
            # Section header — descend (replaces same-depth siblings)
            cur_path = full
    return last_hit


def find_duplicate_top_keys(text: str) -> list[str]:
    """Return top-level YAML keys that appear more than once. The user's
    config has been seen with duplicate `supervisor:` blocks; we warn so
    they can clean it up."""
    seen: dict[str, int] = {}
    for line in text.splitlines():
        if not line or line.startswith((" ", "\t", "#")):
            continue
        m = _KEY_RE.match(line)
        if not m:
            continue
        seen[m.group(1)] = seen.get(m.group(1), 0) + 1
    return [k for k, n in seen.items() if n > 1]


def replace_key_line(text: str, dotted: str, new_value: str) -> str:
    """Surgical replace — preserves indentation and inline comment.
    Raises KeyError if the key isn't found, ValueError if the leaf is
    a complex (list/dict/block-scalar) value."""
    found = find_key_line(text, dotted)
    if found is None:
        raise KeyError(dotted)
    i, indent, current, comment = found
    if current.startswith(("[", "{", "|", ">")):
        raise ValueError(f"{dotted}: leaf is a list/dict/block scalar — "
                         "edit via SSH, not Telegram")
    leaf = dotted.rsplit(".", 1)[-1]
    new_line = f"{' ' * indent}{leaf}: {new_value}{comment}"
    lines = text.splitlines(keepends=True)
    # preserve the original line ending
    eol = "\n"
    if lines[i].endswith("\r\n"):
        eol = "\r\n"
    lines[i] = new_line + eol
    return "".join(lines)


# ----------------------- value coercion --------------------------------

def coerce_value(target: Any, raw: str) -> tuple[Any, str]:
    """Coerce a string value to match `target`'s type. Returns
    (parsed_value, yaml_serialized_str)."""
    raw = raw.strip()
    if isinstance(target, bool):
        if raw.lower() in ("true", "yes", "on", "1"):
            return True, "true"
        if raw.lower() in ("false", "no", "off", "0"):
            return False, "false"
        raise ValueError(f"expected bool (true/false), got {raw!r}")
    if isinstance(target, int) and not isinstance(target, bool):
        v = int(raw)
        return v, str(v)
    if isinstance(target, float):
        v = float(raw)
        return v, str(v)
    # str (or unknown) — quote if it looks like it'd be misparsed
    s = raw.strip("'\"")
    if any(c in s for c in ":#") or s.lower() in ("true", "false", "yes", "no", "on", "off", "null"):
        return s, f'"{s}"'
    return s, s


# ----------------------- command handlers ------------------------------

def _walk(obj: dict, dotted: str) -> Any:
    cur: Any = obj
    for p in dotted.split("."):
        if not isinstance(cur, dict) or p not in cur:
            return None
        cur = cur[p]
    return cur


def _flat_keys(d: dict, prefix: str = "") -> list[str]:
    out: list[str] = []
    for k, v in (d or {}).items():
        full = f"{prefix}.{k}" if prefix else k
        if isinstance(v, dict):
            out.extend(_flat_keys(v, full))
        else:
            out.append(full)
    return out


def cmd_get(cfg_path: Path, dotted: str) -> str:
    if _is_secret_key(dotted):
        return f"❌ {dotted}: secret — read config.yaml via SSH"
    cfg = yaml.safe_load(cfg_path.read_text(encoding="utf-8")) or {}
    val = _walk(cfg, dotted)
    if val is None and dotted not in _flat_keys(cfg):
        return f"❌ {dotted}: not found"
    return f"{dotted} = {val!r}"


def cmd_set(cfg_path: Path, dotted: str, value: str) -> str:
    if _is_secret_key(dotted):
        return f"❌ {dotted}: secret — edit config.yaml via SSH, not Telegram"
    text = cfg_path.read_text(encoding="utf-8")
    cfg = yaml.safe_load(text) or {}
    current = _walk(cfg, dotted)
    if current is None:
        # Allow creation only if the key exists in the example schema
        example_path = cfg_path.parent / "config.example.yaml"
        if not example_path.exists():
            return f"❌ {dotted}: not found in config.yaml; can't infer type"
        ex = yaml.safe_load(example_path.read_text(encoding="utf-8")) or {}
        ex_val = _walk(ex, dotted)
        if ex_val is None:
            return (f"❌ {dotted}: unknown key (not in config.yaml or "
                    f"config.example.yaml)")
        return (f"❌ {dotted}: not present in config.yaml — add it via SSH "
                f"first (template in config.example.yaml: {ex_val!r})")
    try:
        new_val, serialized = coerce_value(current, value)
    except (ValueError, TypeError) as e:
        return f"❌ {dotted}: {e}"
    try:
        new_text = replace_key_line(text, dotted, serialized)
    except KeyError:
        return (f"❌ {dotted}: live config has the key but the line-replacer "
                f"couldn't find it (unusual indentation?). Edit via SSH.")
    except ValueError as e:
        return f"❌ {e}"
    # Validate by re-parsing
    try:
        reparsed = yaml.safe_load(new_text)
    except yaml.YAMLError as e:
        return f"❌ {dotted}: rewrite produced invalid YAML — {e}"
    if _walk(reparsed, dotted) != new_val:
        return f"❌ {dotted}: round-trip mismatch — refusing to write"
    # Backup, write, chmod
    bak = cfg_path.with_suffix(cfg_path.suffix + ".bak")
    shutil.copy2(cfg_path, bak)
    cfg_path.write_text(new_text, encoding="utf-8")
    try:
        cfg_path.chmod(0o600)
    except OSError:
        pass
    foot = ("\n→ /restart to apply" if _needs_restart(dotted)
            else "\n→ applied; takes effect on next tick")
    dups = find_duplicate_top_keys(new_text)
    if dups:
        foot += f"\n⚠ duplicate top-level sections: {', '.join(dups)} — last one wins; clean up via SSH"
    return f"✅ {dotted}: {current!r} → {new_val!r}{foot}"


def cmd_show(cfg_path: Path, prefix: str | None = None) -> str:
    cfg = yaml.safe_load(cfg_path.read_text(encoding="utf-8")) or {}
    keys = _flat_keys(cfg)
    if prefix:
        keys = [k for k in keys if k.startswith(prefix)]
    out: list[str] = []
    for k in keys:
        v = _walk(cfg, k)
        if _is_secret_key(k):
            v = REDACT if v else ""
        out.append(f"{k} = {v!r}")
    if not out:
        return "(no keys match)"
    body = "\n".join(out)
    # Trim aggressively — Telegram cap is 4096
    if len(body) > 3500:
        body = body[:3500] + "\n…[truncated, narrow with /cfg show <prefix>]"
    return body


def cmd_keys(cfg_path: Path) -> str:
    cfg = yaml.safe_load(cfg_path.read_text(encoding="utf-8")) or {}
    keys = _flat_keys(cfg)
    editable = [k for k in keys if not _is_secret_key(k)]
    return "Editable keys (use /cfg set <key> <value>):\n" + "\n".join(editable)


def cmd_revert(cfg_path: Path) -> str:
    bak = cfg_path.with_suffix(cfg_path.suffix + ".bak")
    if not bak.exists():
        return "❌ no .bak to revert to"
    # diff summary
    try:
        old = bak.read_text(encoding="utf-8")
        new = cfg_path.read_text(encoding="utf-8")
        n_changed = sum(1 for a, b in zip(old.splitlines(), new.splitlines()) if a != b)
    except OSError:
        n_changed = -1
    shutil.copy2(bak, cfg_path)
    try:
        cfg_path.chmod(0o600)
    except OSError:
        pass
    return (f"✅ reverted config.yaml from .bak ({n_changed} differing lines)\n"
            "→ /restart to apply")


def cmd_restart() -> str:
    """Detach a child that waits 2s then restarts the service. The
    current process gets killed by systemd's `restart`; the child
    survives because it's in a new session."""
    subprocess.Popen(
        ["bash", "-c", "sleep 2 && systemctl restart mako-zero"],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )
    return "🔄 restart scheduled in 2s — see you on the other side"


def cmd_status(cfg_path: Path, paths_root: Path) -> str:
    cfg = yaml.safe_load(cfg_path.read_text(encoding="utf-8")) or {}
    state = paths_root / "state"
    pending = paths_root / "pending"
    try:
        tick_n = (state / "tick_counter.txt").read_text(encoding="utf-8").strip()
    except OSError:
        tick_n = "?"
    journal = state / "JOURNAL.md"
    last_journal = "—"
    if journal.exists():
        lines = journal.read_text(encoding="utf-8").splitlines()
        if lines:
            last_journal = lines[-1][:200]
    pending_n = 0
    pf = pending / "pending_actions.jsonl"
    if pf.exists():
        pending_n = sum(1 for L in pf.read_text(encoding="utf-8").splitlines() if L.strip())
    inbox_path = state / "INBOX.md"
    inbox_chars = 0
    if inbox_path.exists():
        inbox_chars = len(inbox_path.read_text(encoding="utf-8").strip())

    # Availability — import locally to avoid import-cycle at module load
    try:
        import tick as t_mod
        av = t_mod.compute_availability(cfg)
        av_line = f"availability: {av['summary']}"
    except Exception as e:
        av_line = f"availability: (compute error: {e!r})"

    return (f"tick #{tick_n} — last:\n  {last_journal}\n"
            f"pending approvals: {pending_n}\n"
            f"inbox: {inbox_chars} chars\n"
            f"{av_line}")


def cmd_meta(paths_root: Path, message: str) -> str:
    """Append a steering message to state/META_INBOX.md.

    Meta reads this file at the start of each run and archives it after.
    Use this to give the meta loop high-level direction without
    touching the worker INBOX (e.g. "stop tweaking the scribe prompt
    for now", "investigate why ticks are slow", "add a confidence
    nudge to system.md").
    """
    msg = message.strip()
    if not msg:
        return "usage: /meta <message>"
    state_dir = paths_root / "state"
    state_dir.mkdir(parents=True, exist_ok=True)
    p = state_dir / "META_INBOX.md"
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    entry = f"\n[/meta · {ts}]\n{msg}\n"
    with p.open("a", encoding="utf-8") as f:
        f.write(entry)
    return f"📥 META_INBOX += {len(msg)} chars (next meta tick will see it)"


def cmd_inbox(paths_root: Path) -> str:
    p = paths_root / "state" / "INBOX.md"
    if not p.exists():
        return "(inbox empty)"
    body = p.read_text(encoding="utf-8").strip()
    if not body:
        return "(inbox empty)"
    if len(body) > 3500:
        body = body[:3500] + "\n…[truncated]"
    return body


def cmd_help() -> str:
    return (
        "Mako command surface (Telegram):\n"
        "/cfg get <key>          read a config value\n"
        "/cfg set <key> <value>  set a config value (validates + backs up)\n"
        "/cfg show [prefix]      dump all editable keys (secrets redacted)\n"
        "/cfg keys               list keys you can /cfg set\n"
        "/cfg revert             undo last /cfg set\n"
        "/restart                restart the mako-zero service\n"
        "/status                 last tick, queue, availability\n"
        "/inbox                  show current INBOX.md (what Mako sees next tick)\n"
        "/meta <message>         steer the meta loop (appends to META_INBOX.md)\n"
        "/help                   this message\n"
        "\n"
        "Anything else (no leading slash): appended to INBOX.md as steering "
        "for the next tick — or to META_INBOX.md if posted in the #meta thread. "
        "Reply to a NEEDS APPROVAL ping with yes/no/etc. to approve/reject."
    )


# ----------------------- top-level dispatch ----------------------------

def handle_command(text: str, cfg_path: Path, paths_root: Path) -> str | None:
    """Returns a reply string if the message was a recognised command, or
    None if the caller should fall through to normal INBOX-append flow."""
    parts = text.strip().split()
    if not parts:
        return None
    head = parts[0].lower().lstrip("/")
    if not text.startswith("/"):
        return None

    if head in ("help", "h", "?"):
        return cmd_help()
    if head == "status":
        return cmd_status(cfg_path, paths_root)
    if head == "inbox":
        return cmd_inbox(paths_root)
    if head == "meta":
        return cmd_meta(paths_root, " ".join(parts[1:]))
    if head == "restart":
        return cmd_restart()
    if head == "cfg":
        if len(parts) < 2:
            return "usage: /cfg <get|set|show|keys|revert> [...]"
        sub = parts[1].lower()
        rest = parts[2:]
        if sub == "get":
            if not rest:
                return "usage: /cfg get <dotted.key>"
            return cmd_get(cfg_path, rest[0])
        if sub == "set":
            if len(rest) < 2:
                return "usage: /cfg set <dotted.key> <value>"
            return cmd_set(cfg_path, rest[0], " ".join(rest[1:]))
        if sub == "show":
            return cmd_show(cfg_path, rest[0] if rest else None)
        if sub == "keys":
            return cmd_keys(cfg_path)
        if sub == "revert":
            return cmd_revert(cfg_path)
        return f"unknown /cfg subcommand: {sub!r} — try /help"
    return None  # not a command we handle; let the listener fall through
