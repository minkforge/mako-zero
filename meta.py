#!/usr/bin/env python3
"""Mako Meta — self-improvement loop.

Runs on a slow cadence (default 30 min) under the supervisor. Builds
a context summary at state/META_INPUT.md, then invokes the local
`codex` CLI with prompts/meta.md as the system prompt and the context
file as the task. Codex has full filesystem access and can edit
prompts/config; it commits its changes to git locally (no push).

Outputs:
- state/META_INPUT.md            input handed to Codex (gitignored)
- state/META_REPORTS.md          rolling log of what each meta tick did
- logs/meta/<NNNNNNNN>.json      full payload per run
- Telegram log thread post

The meta loop never executes Mako actions, never modifies state/*.md
or notes/* — that's Mako's working memory.
"""
from __future__ import annotations

import argparse
import csv
import json
import os
import re
import shutil
import subprocess
import sys
import time
import traceback
from datetime import datetime, timezone
from pathlib import Path

import yaml


APP_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(APP_DIR))
import tick as t_mod  # noqa: E402


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def load_cfg(p: str) -> dict:
    with open(p, "r", encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def bump_meta_counter(state: Path) -> int:
    p = state / "meta_counter.txt"
    n = 0
    if p.exists():
        try:
            n = int(p.read_text(encoding="utf-8").strip() or "0")
        except ValueError:
            n = 0
    n += 1
    p.write_text(str(n), encoding="utf-8")
    return n


def tail_lines(p: Path, n: int) -> str:
    if not p.exists():
        return ""
    return "\n".join(p.read_text(encoding="utf-8").splitlines()[-n:])


def read_redacted_config(p: Path) -> str:
    text = p.read_text(encoding="utf-8")
    out_lines = []
    for line in text.splitlines():
        low = line.lower()
        if any(s in low for s in ("api_key:", "bot_token:", "smtp_password:", "token:")):
            # crude redaction — keep `key: ` part, drop value
            head = line.split(":", 1)[0]
            out_lines.append(f"{head}: [REDACTED]")
        else:
            out_lines.append(line)
    return "\n".join(out_lines)


def metrics_summary(logs: Path, n: int = 50) -> str:
    p = logs / "metrics.csv"
    if not p.exists():
        return "(no metrics.csv yet)"
    with p.open("r", encoding="utf-8") as f:
        rows = list(csv.DictReader(f))[-n:]
    if not rows:
        return "(metrics.csv empty)"
    parse_ok = sum(1 for r in rows if r.get("parse_ok") == "True")
    no_actions = sum(1 for r in rows if (r.get("actions_count") or "0") in ("0", ""))
    walls = [float(r["wall_s"]) for r in rows if r.get("wall_s")]
    drifts = [r.get("drift_flag", "") for r in rows if r.get("drift_flag")]
    summary = [
        f"# metrics summary (last {len(rows)} ticks)",
        f"parse_ok rate: {parse_ok}/{len(rows)} ({100*parse_ok/len(rows):.1f}%)",
        f"no-actions tick rate: {no_actions}/{len(rows)} ({100*no_actions/len(rows):.1f}%)",
        f"wall_s avg: {sum(walls)/len(walls):.1f}, max: {max(walls):.1f}, min: {min(walls):.1f}" if walls else "wall_s: n/a",
        f"drift flags raised: {len([d for d in drifts if d])}",
        "",
        "## raw rows (newest first)",
        "ts,tick,mode,wall,provider,parse_ok,actions,queued,drift",
    ]
    for r in reversed(rows):
        summary.append(",".join([
            r.get("ts", ""), r.get("tick_n", ""), r.get("mode", ""),
            r.get("wall_s", ""), r.get("provider_used", ""),
            r.get("parse_ok", ""), r.get("actions_count", ""),
            r.get("actions_queued", ""), (r.get("drift_flag") or "")[:30],
        ]))
    return "\n".join(summary)


def recent_errors(logs: Path, n: int = 5) -> str:
    files = sorted(logs.glob("error-*.log"), key=lambda p: p.stat().st_mtime, reverse=True)[:n]
    if not files:
        return "(no recent error logs)"
    out = []
    for f in files:
        out.append(f"## {f.name}")
        out.append(f.read_text(encoding="utf-8")[:2500])
        out.append("")
    return "\n".join(out)


def consume_meta_inbox(state: Path, archive: Path) -> str:
    """Read state/META_INBOX.md if present; archive + clear it.

    Returns the inbox text (or empty string). Archived to
    archive/meta-inbox-<UTC-timestamp>.md so Chris can review later.
    """
    p = state / "META_INBOX.md"
    if not p.exists():
        return ""
    try:
        text = p.read_text(encoding="utf-8").strip()
    except Exception:
        return ""
    if not text:
        return ""
    # Archive then clear
    try:
        archive.mkdir(parents=True, exist_ok=True)
        ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H-%M-%SZ")
        (archive / f"meta-inbox-{ts}.md").write_text(text, encoding="utf-8")
    except Exception:
        pass
    try:
        p.write_text("", encoding="utf-8")
    except Exception:
        pass
    return text


def build_context(cfg: dict, paths: t_mod.Paths, meta_n: int) -> str:
    state = paths.state
    logs = paths.logs

    parts: list[tuple[str, str]] = []

    parts.append(("intro", f"Meta tick #{meta_n} at {now_iso()}.\n"
                            f"Mako worker tick counter: {tail_lines(state/'tick_counter.txt', 1)}"))

    # Steering from Chris — highest priority, consume + archive
    inbox_text = consume_meta_inbox(state, paths.archive)
    if inbox_text:
        parts.append(("⚡ META INBOX FROM CHRIS — address this before doing anything else",
                      inbox_text))

    parts.append(("metrics", metrics_summary(logs, n=50)))
    parts.append(("recent journal (last 30)", tail_lines(state / "JOURNAL.md", 30)))
    parts.append(("recent errors", recent_errors(logs, n=5)))
    parts.append(("prior meta reports (last)", tail_lines(state / "META_REPORTS.md", 80)))

    # current prompts and config
    for name in ("prompts/system.md", "prompts/compact.md", "prompts/scribe.md"):
        p = paths.root / name
        if p.exists():
            parts.append((name, p.read_text(encoding="utf-8")))

    cfg_p = paths.root / "config.yaml"
    if cfg_p.exists():
        parts.append(("config.yaml (secrets redacted)", read_redacted_config(cfg_p)))

    # last few inbox archives so meta sees Chris's directives
    archive = paths.archive
    if archive.exists():
        recents = sorted(archive.glob("inbox-*.md"), key=lambda p: p.stat().st_mtime, reverse=True)[:3]
        if recents:
            inbox_lines = []
            for r in recents:
                inbox_lines.append(f"## {r.name}")
                inbox_lines.append(r.read_text(encoding="utf-8")[:800])
                inbox_lines.append("")
            parts.append(("recent INBOX archives", "\n".join(inbox_lines)))

    blocks = [f"# {label}\n\n{content.rstrip()}\n" for label, content in parts]
    return "\n---\n".join(blocks)


def call_codex(meta_prompt: str, context: str, cfg: dict, timeout_s: int = 600) -> dict:
    """Invoke `codex` CLI. We use the `exec` subcommand which runs
    non-interactively and applies edits. Falls back to surfacing what
    codex did regardless of exit status."""
    meta_cfg = cfg.get("meta", {}) or {}
    model = meta_cfg.get("codex_model", "gpt-5.5")
    cwd = "/srv/mako-zero"

    # Codex reads a prompt from stdin or args. We'll pass the combined
    # prompt as an arg; codex CLI supports `codex exec "<task>"`.
    # The system instruction is prepended into the task itself since
    # codex doesn't have a `--system` flag (varies by version).
    full_input = (
        "# Meta system instructions\n\n"
        + meta_prompt
        + "\n\n---\n\n# Current meta-tick input\n\n"
        + context
        + "\n\n---\n\nMake at most one change. Then commit with a `meta:` "
        + "prefixed git message (no push). Append your report to "
        + "state/META_REPORTS.md before exiting."
    )

    args = ["codex", "exec", "--model", model, "--skip-git-repo-check",
            "--cd", cwd, full_input]

    t0 = time.time()
    try:
        proc = subprocess.run(args, capture_output=True, timeout=timeout_s,
                              text=True, errors="replace")
        rc = proc.returncode
        stdout = proc.stdout
        stderr = proc.stderr
    except subprocess.TimeoutExpired as e:
        rc = -1
        stdout = e.stdout or ""
        stderr = (e.stderr or "") + "\n[TIMEOUT]"
    except FileNotFoundError:
        return {"ok": False, "error": "codex CLI not found in PATH",
                "wall_s": 0.0}
    except Exception as e:
        return {"ok": False, "error": f"{type(e).__name__}: {e}",
                "wall_s": round(time.time() - t0, 2)}
    return {
        "ok": rc == 0,
        "rc": rc,
        "wall_s": round(time.time() - t0, 2),
        "stdout": stdout[-8000:],
        "stderr": stderr[-4000:],
        "args_summary": f"codex exec --model {model} (cwd={cwd}, input={len(full_input)} chars)",
    }


def git_status_and_log(repo: Path) -> dict:
    """Capture what Codex actually changed. Returns recent diff stat + commit log."""
    out: dict = {}
    try:
        r = subprocess.run(["git", "-C", str(repo), "log", "-3", "--oneline"],
                           capture_output=True, text=True, timeout=10)
        out["recent_log"] = r.stdout.strip()
    except Exception as e:
        out["recent_log"] = f"err: {e}"
    try:
        r = subprocess.run(["git", "-C", str(repo), "status", "--porcelain"],
                           capture_output=True, text=True, timeout=10)
        out["status_porcelain"] = r.stdout.strip()
    except Exception as e:
        out["status_porcelain"] = f"err: {e}"
    try:
        r = subprocess.run(["git", "-C", str(repo), "diff", "HEAD~1", "--stat"],
                           capture_output=True, text=True, timeout=10)
        out["last_commit_stat"] = r.stdout.strip()[:1500]
    except Exception as e:
        out["last_commit_stat"] = f"err: {e}"
    return out


# Files / dirs meta must NEVER auto-commit, even if Codex stages them.
# .gitignore already excludes most of these — this is belt-and-braces.
_DENY_PATHS = (
    "config.yaml",            # local secrets — gitignored, don't override
    ".env",                    # gitignored
    ".dash.htpasswd",          # dashboard auth
    "state/", "notes/",        # Mako's working memory (gitignored)
    "workdir/", "archive/",
    "pending/", "logs/",
    "__pycache__/",
    "OVERNIGHT-",              # local handover notes (gitignored anyway)
)
_DENY_SUFFIXES = (".pem", ".crt", ".key", ".htpasswd")

# Secret-shaped patterns that should never appear in a diff being pushed.
# Conservative: catches "<keyword>: <non-empty>" and AWS/GCP key prefixes.
_SECRET_LINE_PATTERNS = (
    re.compile(r"^\+.*\b(api_key|bot_token|smtp_password|password|secret|access_token)\b\s*:\s*[\"']?[^\"'\s][^\"']*", re.IGNORECASE),
    re.compile(r"^\+.*\bAKIA[0-9A-Z]{16}\b"),                   # AWS access key
    re.compile(r"^\+.*\bAIza[0-9A-Za-z_\-]{30,}\b"),            # GCP API key
    re.compile(r"^\+.*\b(sk|rk|pk)_(test|live)_[0-9a-zA-Z]{16,}\b"),  # Stripe
    re.compile(r"^\+.*\bxox[abposr]-[0-9A-Za-z\-]{20,}\b"),     # Slack
    re.compile(r"^\+.*\bgithub_pat_[0-9A-Za-z_]{20,}\b"),       # GitHub PAT
    re.compile(r"^\+.*\bghp_[0-9A-Za-z]{30,}\b"),               # GitHub classic
)


def _is_denied(path: str) -> bool:
    if any(path.startswith(p) for p in _DENY_PATHS):
        return True
    if any(path.endswith(s) for s in _DENY_SUFFIXES):
        return True
    return False


def _scan_diff_for_secrets(repo: Path) -> list[str]:
    """Scan the most recent commit's diff for secret-shaped lines.

    Runs against HEAD vs HEAD~1 (i.e. what we're about to push).
    Returns a list of offending line snippets; empty = clean.
    """
    try:
        r = subprocess.run(
            ["git", "-C", str(repo), "diff", "HEAD~1", "HEAD", "--unified=0"],
            capture_output=True, text=True, timeout=15)
    except Exception as e:
        # If we can't scan, abort the push (fail closed)
        return [f"could not run git diff: {e}"]
    if r.returncode != 0:
        return [f"git diff rc={r.returncode}: {r.stderr[:200]}"]
    hits: list[str] = []
    for line in r.stdout.splitlines():
        for pat in _SECRET_LINE_PATTERNS:
            if pat.search(line):
                hits.append(line[:200])
                break
    return hits


def _git_push(repo: Path) -> dict:
    """Push origin/main. Returns {ok, rc, stdout, stderr}."""
    try:
        r = subprocess.run(["git", "-C", str(repo), "push", "origin", "main"],
                           capture_output=True, text=True, timeout=60)
        return {"ok": r.returncode == 0, "rc": r.returncode,
                "stdout": r.stdout[-500:], "stderr": r.stderr[-500:]}
    except Exception as e:
        return {"ok": False, "rc": -1, "error": f"{type(e).__name__}: {e}"}


def git_commit_meta_changes(repo: Path, meta_n: int, codex_summary: str = "") -> dict:
    """Codex's sandbox mounts .git read-only, so it commits via us.

    Whitelist is now permissive: any tracked-or-untracked file dirty after
    a meta run gets committed, EXCEPT files matching _DENY_PATHS/_DENY_SUFFIXES.
    `.gitignore` already keeps secrets and runtime state out, but we also
    pre-screen paths and post-screen the diff for secret-shaped values.

    After commit, we git push origin main — but only if the diff scan
    is clean. A failed scan means the commit stays local; Chris is
    alerted via the meta status post.
    """
    out: dict = {"committed": False, "pushed": False, "skipped": [], "added": []}
    try:
        r = subprocess.run(["git", "-C", str(repo), "status", "--porcelain"],
                           capture_output=True, text=True, timeout=10)
    except Exception as e:
        out["error"] = f"git status: {e}"
        return out
    if r.returncode != 0:
        out["error"] = f"git status rc={r.returncode}: {r.stderr[:300]}"
        return out
    if not r.stdout.strip():
        out["note"] = "no changes to commit"
        return out

    to_add: list[str] = []
    for line in r.stdout.splitlines():
        # porcelain format: 'XY <path>' (X=staged, Y=unstaged); untracked is '?? <path>'
        path = line[3:].strip()
        if _is_denied(path):
            out["skipped"].append(path)
            continue
        to_add.append(path)
    if not to_add:
        out["note"] = "dirty files all on deny list; refusing to commit"
        return out

    try:
        ar = subprocess.run(["git", "-C", str(repo), "add"] + to_add,
                            capture_output=True, text=True, timeout=10)
        if ar.returncode != 0:
            out["error"] = f"git add rc={ar.returncode}: {ar.stderr[:300]}"
            return out
        out["added"] = to_add
        msg_first = (codex_summary.splitlines() or [""])[0][:80] if codex_summary else "auto-patch"
        msg = f"meta #{meta_n}: {msg_first}\n\nAuto-committed by meta loop.\nFiles: {', '.join(to_add)}"
        cr = subprocess.run(["git", "-C", str(repo), "commit", "-m", msg],
                            capture_output=True, text=True, timeout=15)
        if cr.returncode != 0:
            out["error"] = f"git commit rc={cr.returncode}: {cr.stderr[:300]}"
            return out
        out["committed"] = True
        out["commit_msg"] = msg
    except Exception as e:
        out["error"] = f"{type(e).__name__}: {e}"
        return out

    # Pre-push secret scan — fail closed
    secret_hits = _scan_diff_for_secrets(repo)
    if secret_hits:
        out["push_aborted"] = "secret-shaped content in diff"
        out["secret_hits"] = secret_hits[:5]
        return out

    push_res = _git_push(repo)
    out["push"] = push_res
    out["pushed"] = bool(push_res.get("ok"))
    return out


def write_full_log(logs: Path, n: int, payload: dict) -> Path | None:
    out = logs / "meta" / f"{n:08d}.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    try:
        out.write_text(json.dumps(payload, ensure_ascii=False, indent=2, default=str),
                       encoding="utf-8")
        return out
    except Exception as e:
        sys.stderr.write(f"meta full-log write failed: {e!r}\n")
        return None


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", required=True)
    ap.add_argument("--dry-run", action="store_true",
                    help="build context but don't call codex")
    args = ap.parse_args()

    cfg = load_cfg(args.config)
    paths = t_mod.Paths(cfg)
    paths.ensure()

    meta_n = bump_meta_counter(paths.state)
    started = now_iso()
    t0 = time.time()

    full = {"meta_n": meta_n, "started_at": started, "config_path": args.config}

    try:
        context = build_context(cfg, paths, meta_n)
        # persist for visibility
        meta_input = paths.state / "META_INPUT.md"
        meta_input.write_text(context, encoding="utf-8")
        full["context_chars"] = len(context)

        meta_prompt = (paths.root / "prompts" / "meta.md").read_text(encoding="utf-8")
        full["meta_prompt_chars"] = len(meta_prompt)

        if args.dry_run:
            full["dry_run"] = True
            full["wall_s"] = round(time.time() - t0, 2)
            full["ended_at"] = now_iso()
            write_full_log(paths.logs, meta_n, full)
            print(f"[meta #{meta_n}] dry-run: context {len(context)} chars; META_INPUT.md written")
            return 0

        codex_meta = (cfg.get("meta", {}) or {})
        timeout_s = int(codex_meta.get("timeout_s", 600))
        codex_res = call_codex(meta_prompt, context, cfg, timeout_s=timeout_s)
        full["codex"] = codex_res

        # Codex's sandbox mounts .git read-only, so it can't commit. We
        # commit on its behalf, only for files inside the meta whitelist.
        commit_res = git_commit_meta_changes(paths.root, meta_n,
                                             codex_summary=codex_res.get("stdout", "")[-2000:])
        full["commit"] = commit_res

        # Capture what was committed
        full["git"] = git_status_and_log(paths.root)

        wall = round(time.time() - t0, 2)
        full["wall_s"] = wall
        full["ended_at"] = now_iso()
        write_full_log(paths.logs, meta_n, full)

        # Telegram blow-by-blow → meta thread (fallback log)
        meta_thread = (cfg["telegram"].get("meta_thread_id")
                       or cfg["telegram"].get("log_thread_id"))
        if codex_res.get("ok"):
            committed = commit_res.get("committed")
            pushed = commit_res.get("pushed")
            added = commit_res.get("added") or []
            skipped = commit_res.get("skipped") or []
            note = commit_res.get("note") or ""
            if committed and pushed:
                msg = (f"🧠 meta #{meta_n} · {wall}s · committed + pushed\n"
                       f"files: {', '.join(added)[:200]}")
            elif committed and commit_res.get("push_aborted"):
                hits = commit_res.get("secret_hits") or []
                msg = (f"🚨 meta #{meta_n} · {wall}s · committed but PUSH ABORTED\n"
                       f"reason: {commit_res['push_aborted']}\n"
                       f"hits: {' / '.join(h[:80] for h in hits[:3])}\n"
                       f"local commit kept; SSH in and review")
            elif committed:
                push_err = (commit_res.get("push") or {}).get("stderr", "") or "no push attempted"
                msg = (f"⚠️ meta #{meta_n} · {wall}s · committed but push failed\n"
                       f"files: {', '.join(added)[:200]}\n"
                       f"err: {push_err[:200]}")
            elif note == "no changes to commit":
                msg = f"🧠 meta #{meta_n} · {wall}s · no changes (within tolerance)"
            else:
                detail = note or commit_res.get("error") or ""
                msg = (f"🧠 meta #{meta_n} · {wall}s · skipped\n"
                       f"{detail[:200]}\n"
                       + (f"skipped: {', '.join(skipped)[:120]}" if skipped else ""))
        else:
            err = codex_res.get("error") or codex_res.get("stderr", "")[:300]
            msg = f"🧠 meta #{meta_n} · {wall}s · FAIL\n{err[:400]}"
        try:
            t_mod.telegram_send(cfg, msg, thread_id=meta_thread, label="meta")
        except Exception:
            pass

        return 0 if codex_res.get("ok") else 1

    except Exception as e:
        tb = traceback.format_exc()
        full["error"] = {"type": type(e).__name__, "message": str(e), "traceback": tb}
        full["wall_s"] = round(time.time() - t0, 2)
        full["ended_at"] = now_iso()
        write_full_log(paths.logs, meta_n, full)
        try:
            meta_thread = (cfg["telegram"].get("meta_thread_id")
                           or cfg["telegram"].get("log_thread_id"))
            t_mod.telegram_send(cfg,
                                f"🧠 meta #{meta_n} · CRASH · {type(e).__name__}: {str(e)[:300]}",
                                thread_id=meta_thread, label="meta-crash")
        except Exception:
            pass
        return 2


if __name__ == "__main__":
    sys.exit(main())
