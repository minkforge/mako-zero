# Example — Income Experiment

The original Mako. Goal: make £100/month online, on a £100/month
budget, documenting the process publicly under the `minkforge.com`
brand.

This is the seed for the `mako-zero` instance running on srv1.

## What this Mako does

- Researches monetisable opportunities (tools, content, services,
  small SaaS) within reach of a single AI agent on a £100/mo budget.
- Builds and ships small experiments end-to-end: code → deploy →
  measure → write up.
- Maintains a public blog at `minkforge.com` documenting failures
  and learnings.
- Reports honestly when an idea doesn't work and pivots without
  sentimentality.

## What this Mako does NOT do

- No public posting on social media or forums (Chris does any
  external sharing manually).
- No outbound cold email at scale (one-off sharp emails OK with
  approval, no campaigns).
- No services that need a UK residential IP, browser session, or
  account on a platform Mako doesn't have.

## What you need to provide in config.yaml

- Ollama Cloud key (`llm.primary.api_key`)
- OpenCode Go key (`llm.fallback.api_key`)
- Telegram bot token + chat_id + threads (log/requests/approvals/digest)
- Cloudflare token (`cloudflare.token`) — for managing minkforge.com DNS
- Fastmail SMTP credentials (optional, for `email_send` action)

## What you need to provide in CAPABILITIES.md

Update the per-platform statuses as you grant Mako access:
- ✅ Active (he can use it now)
- ⚠️ Partial (works but with caveats — note them)
- ❌ Blocked (he knows about it, can't use it yet)
- ◻️ Missing (not yet created)

## Cadence (defaults — tune per project)

- Worker tick: every 5 min (`supervisor.tick_interval_s: 300`)
- Scribe: every 2h (`supervisor.scribe_interval_s: 7200`)
- Meta: every 30 min (`supervisor.meta_interval_s: 1800`),
  `meta.enabled: false` until you've smoke-tested Codex
- Digest: 08:00 local (`supervisor.digest_hour_local: 8`)

## Telegram threads needed (in one supergroup)

- **Log** — per-tick blow-by-blow (high volume)
- **Requests** — `ask_chris` pings (medium volume)
- **Approvals** — `NEEDS APPROVAL` pings (low volume, important)
- **Digest** — daily 08:00 summary (low volume)

Fewer threads also work — they fall back to Log if not configured.
