# Capabilities

What you have access to right now. Statuses: ✅ active, ⚠️ partial,
❌ blocked, ◻️ missing.

## Compute & infra
- ✅ [Hetzner / DigitalOcean / wherever] VPS. Root access, full
  shell. The `shell` action runs with full host access — denylist
  blocks the obviously catastrophic.
- ✅ Local filesystem under `/srv/[name]/` for state, notes, drafts,
  workdir.

## LLMs
- ✅ Ollama Cloud — primary. Output token cap [N]K per tick — see
  §Big writes in your system prompt.
- ✅ [Fallback provider, e.g. OpenCode Go]
- ✅ Codex CLI (gpt-5.5) — used by the meta loop on a slow cadence.
  You don't call it directly.

## Comms
- ✅ Telegram bot (log/requests/approvals/digest threads).
- ✅ [User name] — via `ask_chris`. [SLA] response time.

## [Other domain-specific tools you need]
- [Add as needed]

## Accounts (external platforms)
- ◻️ [Account 1] — propose strategy if needed.

## Money
- [Budget ceiling]. Approval threshold: [£X].
- MTD spend tracked in STATE.md.

## What's intentionally not yet here
- [List the things you've deliberately NOT given access to.
  Example: "No public posting. No browser. No outbound email
  campaigns." Be explicit so the agent doesn't propose them.]
- Self-modification soft-guard: don't write to engine files
  (tick.py, supervisor.py, prompts/, config.yaml, *.service).

This file may be edited by [you] as access changes. The agent can
*propose* edits via `ask_chris` but does not write to it directly.
