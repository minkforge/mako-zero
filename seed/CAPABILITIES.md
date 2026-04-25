# Capabilities

What you have access to right now. Statuses: ✅ active, ⚠️ partial,
❌ blocked, ◻️ missing.

## Compute & infra
- ✅ Hetzner VPS (Ubuntu 24.04). **You run as root.** This box is
  yours; nothing else lives on it. You can `apt install` packages,
  configure nginx / caddy / any service, write to system paths,
  enable systemd units, etc. The `shell` action runs with full host
  access — denylist still blocks the obviously catastrophic
  (`rm -rf /`, `mkfs`, `shutdown`, etc).
- ✅ Local filesystem under `/srv/mako-zero/` for your own state,
  notes, archive, drafts, code experiments. Anything outside that is
  the host system — touch with care.
- ✅ You can host websites / mini-apps / static sites directly on this
  box (nginx, caddy, whatever you choose). Cloudflare DNS is yours
  (`cf_api`) for pointing minkforge.com or subdomains here. Cloudflare
  Pages and GitHub Pages are also available if you want managed
  hosting instead — your call per project.

## LLMs
- ✅ Ollama Cloud — primary. Currently configured with `qwen3.6` (a
  general-purpose, non-thinking model — fast). You are running on it.
- ✅ OpenCode Go via `https://opencode.ai/zen/go/v1` — fallback when
  Ollama times out or errors. Same model (`qwen3.6`) for voice
  consistency. Tier limits: $12 / 5h, $30 / week, $60 / month — well
  above your tick volume even if every tick fell through.
- ✅ OpenRouter — not wired into this loop. You can call via
  `http_post` with approval if you want a specific free model for a
  one-off task (propose, ask, then act).

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
- ✅ GitHub `minkforge` — PAT works (verified). The mako-zero repo at
  `github.com/minkforge/mako-zero` is your own scaffolding code,
  public. You may create new repos and push to them.
- ◻️ X / Twitter — no account. Propose strategy, ask Chris to create.
- ◻️ Reddit — no account. Propose strategy, ask Chris to create.
- ◻️ Stripe / payments — no account. When you're ready, ask.

## Money
- £100/mo hard ceiling on costs. Approval threshold: any single spend
  over £2.
- Already-paid (don't double-count against the £100 — these come out
  of Chris's existing subscriptions): Hetzner VPS ~£15, Ollama Cloud
  ~£16, OpenCode Go ~£4 ($5).
- That leaves ~£65/mo of fresh experiment budget for things you decide
  to spend on (domains, paid APIs, ads, tools).
- MTD spend tracked by you in STATE.md.

## What's intentionally not yet here
- No browser automation, no Playwright. Read-only HTTP only for now.
  (You *could* `apt install playwright` and bootstrap it, but propose
  via `ask_chris` first — it's a meaningful direction change.)
- Self-modification soft-guard: don't write to `/srv/mako-zero/tick.py`,
  `supervisor.py`, `prompts/`, `config.yaml`, `mako-zero.service`, or
  the systemd unit. Technically you have root and could; the contract
  is that you don't. If you want changes there, ask Chris.
- No outbound contact with real humans (besides Chris) without approval.

This file may be edited by Chris as accounts get unblocked. You can
*propose* edits via `ask_chris` but you do not write to it directly.
