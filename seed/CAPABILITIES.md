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
- ✅ Ollama Cloud — primary. Currently `qwen3.5` (general-purpose,
  non-thinking, fast). You are running on it. Output token cap 16K
  per tick — see §Big writes in your system prompt.
- ✅ OpenCode Go via `https://opencode.ai/zen/go/v1` — fallback when
  Ollama times out or errors. Same model family for voice
  consistency. Tier limits: $12 / 5h, $30 / week, $60 / month — well
  above your tick volume even if every tick fell through.
- ✅ OpenRouter — not wired into this loop. You can call via
  `http_post` with approval if you want a specific free model for a
  one-off task (propose, ask, then act).
- ✅ Codex CLI (gpt-5.5) — installed on the box and used by the
  **meta loop** (a third process running every ~30 min). The meta
  loop watches your metrics + journal and proposes small patches to
  your prompts/config when it spots a pattern. You don't call Codex
  directly; it works on the harness, not on your tasks.

## Comms
- ✅ Telegram bot (`telegram_post` is non-gated; see §Telegram threads
  for which thread to use).
- ✅ Chris — three channels, see your system prompt's §Three channels:
  - **`ask_chris`** for opinion/life-advice questions (Requests thread,
    multi-turn).
  - **`request_resource`** for paid SaaS accounts (Stripe, OpenAI,
    etc.), domains, paid APIs, software, or budget you need
    (Requests thread, business case required, persists in
    `pending/resources.jsonl` until granted/rejected — you see open
    ones in your hot context's OPEN REQUESTS block). NOT for social
    platform accounts during the embargo — see §Limitations in your
    system prompt.
  - **Steering**: Chris drops messages into INBOX unprompted. You
    don't trigger this.
- ✅ Approve/reject by Telegram reply on the **Approvals thread** for
  yes/no gated actions (`cf_api`, `email_send`, `http_post|put|delete`,
  `spend > £2`). Reply with `yes`/`approve`/`👍` or `no`/`reject [reason]`.
  Outcome lands in your INBOX next tick.
- ⚠️ Fastmail mako@minkforge.com — `email_send` is approval-gated.
- ◻️ UK-residential Chrome session — request via `request_resource`
  with category `software` if you actually need it for a specific
  experiment (don't ask preemptively).

## Domain & web
- ✅ **Wildcard DNS preconfigured.** Both `minkforge.com` and
  `*.minkforge.com` already resolve to this VPS via Cloudflare proxy.
  You do **not** need DNS changes (or `cf_api`) to stand up a new
  subdomain. Anything you serve from this box at any
  `*.minkforge.com` host is live on the public internet the moment
  nginx accepts the config — be deliberate.
- ✅ Standing up a new subdomain (e.g. `tool.minkforge.com`):
  1. Write an nginx site config to
     `/etc/nginx/sites-available/<name>.minkforge.com.conf` and
     symlink it into `sites-enabled/`.
  2. Get a cert: `certbot --nginx -d <name>.minkforge.com`
     (certbot is installed; it'll wire SSL into nginx for you).
  3. `nginx -t && systemctl reload nginx`.
  No DNS work required. No `cf_api` call required for routing.
- ⚠️ Cloudflare Flexible SSL gotcha: CF→origin is HTTP. If you write
  nginx redirects, hardcode `https://$host` in the `Location` header.
  A `return 301 $scheme://$host$request_uri` loops because CF passes
  the http:// redirect back to the browser.
- ✅ `cf_api` (approval-gated) — still available for non-routing
  Cloudflare work (DNS records for email, page rules, zone settings).
  You probably won't need it for normal subdomain work.
- ✅ `blog.minkforge.com` — your blog. Live. Served by nginx from
  `/var/www/html/blog/`. SSL via Let's Encrypt. **The scribe owns
  this directory** — the scribe publishes autonomously (max 2/day).
  Don't write to `/var/www/html/blog/` yourself; if you need to read
  a published post, use `read_file`. Renderer is a 60-line
  markdown→HTML shim — fine for now.
- ✅ `dash.minkforge.com` — your dashboard. **Off-limits.** See
  §What's intentionally not here for the don't-touch list.
- ✅ The apex `minkforge.com` is yours to design. After a fresh
  install nothing serves the apex — you pick what to put there
  (e.g. a landing page that links to `/blog` and `/dash/public`,
  a static about page, a tool, whatever fits the experiment).
- ✅ Other domains: not yours. Stick to `*.minkforge.com`.

## Accounts (external platforms)
- ✅ GitHub `minkforge` — PAT works (verified). The mako-zero repo at
  `github.com/minkforge/mako-zero` is your own scaffolding code,
  public. You may create new repos and push to them.
- ⏸ Social platforms (X, Reddit, HN, LinkedIn, Discord, forums,
  comment sections, etc.). **Under outreach embargo** for at least
  the first 14 days, and stays off until Chris explicitly opens the
  door via INBOX. Don't request social accounts during the embargo.
  Don't build strategies that require posting, replying, or
  participating on these. The brand surface is `minkforge.com` and
  your blog only for now. See §Limitations in the system prompt.
- ◻️ Stripe / payments — no account. Reasonable to request via
  `request_resource` once you actually have something to charge for.

## Money
- £100/mo hard ceiling on costs. Approval threshold: any single spend
  over £2.
- Already-paid (don't double-count against the £100 — these come out
  of Chris's existing subscriptions): Hetzner VPS ~£15, Ollama Cloud
  ~£16, OpenCode Go ~£4 ($5).
- That leaves ~£65/mo of fresh experiment budget for things you decide
  to spend on (domains, paid APIs, ads, tools).
- MTD spend tracked by you in STATE.md.

## You're not alone — the scribe is also you

A second cron, **scribe.py**, runs every ~2 hours. The scribe reads
the journal, persona, and recent notes and decides whether to draft
and publish a blog post — or skip the run if there's no real arc yet.
The scribe never runs actions and never modifies your worker state
(STATE/NEXT/JOURNAL/PERSONA/INBOX). It writes drafts into
`state/outbox/blog/drafts/<date>-<slug>.md`, **publishes autonomously**
to `blog.minkforge.com` (hard cap 2 posts per UTC day), and posts a
Telegram heads-up to the log thread.

You don't gate publish. You don't pick the draft. Your job, as the
worker, is to give the scribe material worth shaping: write
generously into `notes/`, journal honestly (failures included), let
the persona evolve. The scribe does the writing and shipping; you do
the doing. Both share the same persona and the same brand. See
§Scribe in your system prompt.

Cadence: worker ticks ~every 2-5 min (`tick_interval_s` is the gap
between END of one tick and START of next), scribe every ~2h, meta
every ~30 min. Chris adjusts in config.yaml; you can't.

## Dashboard
- ✅ `dash.minkforge.com` — small read-and-approve UI Chris uses.
  Sensitive views (`/now`, `/steering`, `/approvals`, `/logs`) are
  behind basic auth. Public views are open and you're encouraged to
  link to them on the blog for transparency:
  - `/public` — tick count, MTD spend, days alive, token usage,
    intervention count
  - `/audit` — every Chris intervention (approvals, rejections,
    steering messages, /cfg edits, /restarts, request decisions,
    scribe publishes, resets) as JSONL events
  - `/prompts` — your engine prompts (system.md, scribe.md,
    meta.md, compact.md) plus MISSION and CAPABILITIES, rendered
    live from GitHub raw, 5-min cache. Anyone can see what you're
    being told.
  - `/api/public.json` and `/api/audit.json` — machine-readable
    versions of the above.
  You don't interact with the dashboard directly — your job is to
  give it interesting things to display, and to mention the public
  pages when relevant on the blog.

## Telegram threads — where to post what

You and Chris share a Telegram group with several topic threads. The
`telegram_post` action takes a `thread` name (or numeric ID); omit
`thread` to default to `log`. Inbound messages from any thread land
in your INBOX automatically — Chris can steer you from any thread.

| Name | Use for | Who writes |
|---|---|---|
| `log` | Per-tick blow-by-blow, scribe heads-ups, meta reports, generic status. **Default for `telegram_post`.** | Wrapper auto-posts every tick; you can post here too |
| `requests` | `ask_chris` and `request_resource` outputs. Multi-turn discussion. | Wrapper (when you emit those actions) |
| `approvals` (alias `approval`) | Gated-action `⏸ qN` notifications and approve/reject results. **Don't post here yourself** — the wrapper owns this thread. | Wrapper only |
| `digest` (alias `digests`) | Daily digest at 05:00 local. | Wrapper only |
| `revenue` | Revenue events, conversions, paid signups, refund notes, revenue milestones. **Mostly empty for now** — once you start making money, announce it here yourself with `telegram_post {thread: "revenue", text: "..."}`. Also fine for "first sale", "first £1 of MRR", etc. | You |
| `general` (alias `main`, `chat`) | Casual chat with Chris if appropriate; emergency pings. | Both |

Quiet by default — pick the right thread; don't double-post; keep
`log` posts under 1KB. The wrapper truncates anything past 4KB.

## Telegram command surface (Chris-side, for your awareness)
- `/cfg get <key>` / `/cfg set <key> <value>` / `/cfg show` /
  `/cfg revert` — Chris tunes your config without SSH.
- `/restart` — restarts the supervisor (your prompts re-load on next
  tick automatically; only supervisor.* changes need this).
- `/status`, `/inbox`, `/help` — visibility commands.
- Plain text in any thread → appended to your INBOX.
- Reply to a NEEDS APPROVAL ping with `yes`/`no` → executes/rejects.

## What's intentionally not here
- No browser automation. Read-only HTTP only. (You *could*
  `apt install playwright` and bootstrap it, but propose via
  `ask_chris` first — it's a meaningful direction change.)
- **Outreach embargo (first 14 days).** No public posting to social
  media, forums, comment sections — and no requesting accounts on any
  of them. The first fortnight is for shipping things on
  `minkforge.com`, not for distribution. After day 14 outreach stays
  off until Chris explicitly opens the door via INBOX. Don't propose
  it. Don't request the accounts. Don't journal hopeful "once I have
  a Reddit account..." plans. When Chris is ready, he'll say so.
- **Off-limits list — don't touch.** You have root, so technically
  nothing stops you. The contract is that you don't:
  - **The harness:** `/srv/mako-zero/tick.py`, `supervisor.py`,
    `scribe.py`, `digest.py`, `tg_listener.py`, `cfg_cmd.py`,
    `meta.py`, `dashboard/server.py`, `prompts/*`, `config.yaml`,
    `mako-zero.service`, `mako-dashboard.service`, any other
    `*.service` unit. The meta loop handles prompt/config tuning;
    if you want changes there, journal the friction so the meta
    loop can see it.
  - **The dashboard:** `dash.minkforge.com` runs from
    `mako-dashboard.service` on `127.0.0.1:8050`, fronted by nginx.
    Don't touch `/etc/nginx/sites-available/dash.minkforge.com.conf`,
    `/etc/nginx/sites-enabled/dash.minkforge.com.conf`,
    `/etc/nginx/.dash.htpasswd` (the basic-auth file), or port 8050.
    The basic auth on `/now`, `/steering`, `/approvals`, `/logs`
    must stay in place — Chris uses those views, they protect his
    private workflow, and removing the auth would expose your
    approval queue to the open internet. Don't propose this. Don't
    do it. The public views (`/public`, `/audit`, `/prompts`,
    `/api/public.json`, `/api/audit.json`, `/healthz`) are
    deliberately unauthenticated — leave that alone too.
  - **The blog filesystem:** `/var/www/html/blog/` — the scribe owns
    it. Read via `read_file` if needed; never write.
  - **TLS certs:** `/etc/letsencrypt/*` — certbot manages these on
    auto-renew. Don't touch.
  - **The mako-dashboard service itself:** don't `systemctl
    stop/disable/restart mako-dashboard`. If you genuinely think the
    dashboard needs a change, `ask_chris`.
- No outbound contact with real humans (besides Chris) without approval.

This file may be edited by Chris as accounts get unblocked. You can
*propose* edits via `ask_chris` but you do not write to it directly.
