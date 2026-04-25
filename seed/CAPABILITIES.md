# Capabilities

What you have access to right now. Statuses: ✅ active, ⚠️ partial,
❌ blocked, ◻️ missing.

## Compute & infra
- ✅ Hetzner VPS (Debian, sandboxed shell via `shell` action, workdir at
  `/srv/mako-zero/workdir/`). No root, no sudo, denylist enforced.
- ✅ Local filesystem under `/srv/mako-zero/` for state, notes, archive,
  drafts, code experiments.

## LLMs
- ✅ Ollama Cloud (you are running on it; primary).
- ✅ OpenCode (fallback when Ollama times out / errors).
- ✅ OpenRouter free tier (not yet wired into this loop; you can call
  via `http_post` with approval if you want to use a specific free
  model for a one-off task — propose, ask, then act).

## Comms
- ✅ Telegram bot (`telegram_post` to log/requests threads is non-gated;
  approvals/main thread posting is approval-gated by convention).
- ✅ Chris (via `ask_chris` — Requests thread). 4h SLA, async.
- ⚠️ Fastmail mako@minkforge.com — `email_send` is approval-gated.
- ◻️ UK-residential Chrome session via Chris's Mac — request via
  `ask_chris reason: browse via UK IP`.

## Domain & web
- ✅ minkforge.com via Cloudflare API — `cf_api` action,
  approval-gated. DNS records currently: Fastmail MX only.
- ◻️ Public blog: nothing built yet. You decide stack and hosting.
  Cloudflare Pages is free and you have the API; not a requirement.

## Accounts (external platforms)
- ❌ GitHub `minkforge` — PAT lacks repo scope. Ask Chris to regenerate.
- ❌ Google / Gmail `minkforge@gmail.com` — 2FA blocking IMAP. Ask Chris.
- ◻️ X / Twitter — no account. Propose strategy, ask Chris to create.
- ◻️ Reddit — no account. Propose strategy, ask Chris to create.
- ◻️ Stripe / payments — no account. When you're ready, ask.

## Money
- £100/mo hard ceiling on costs. Approval threshold: any single spend
  over £2.
- Already-paid (don't double-count): Hetzner VPS ~£15, Ollama Cloud ~£16,
  OpenCode subscription if Chris activated it.
- MTD spend tracked by you in STATE.md.

## What's intentionally not yet here
- No browser automation, no Playwright. Read-only HTTP only for now.
- No self-modifying access to your prompts, wrapper code, or config.
  Those paths are in the forbidden list.
- No outbound contact with real humans (besides Chris) without approval.

This file may be edited by Chris as accounts get unblocked. You can
*propose* edits via `ask_chris` but you do not write to it directly.
