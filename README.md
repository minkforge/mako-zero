# mako-zero

A small autonomous-agent loop. One LLM call per tick, structured
JSON response, applied by a thin Python wrapper. Posts a blow-by-blow
update to Telegram after every run. Self-documenting via files Mako
writes itself.

This is V0 — the smallest thing that exercises the full loop end-to-end:
provider call (with fallback), structured response, file writes, real
tool execution, queued approval for the risky stuff, Telegram update,
metrics CSV. Runs on a single dedicated Ubuntu box under systemd. Mako
runs as root by design — the box is his.

## Layout (on the host)

```
/srv/mako-zero/
├── supervisor.py            # systemd entrypoint: scheduler + bootstrap
├── tick.py                  # worker — does the work each tick (~60s)
├── scribe.py                # writer — drafts blog posts (~30 min)
├── digest.py                # daily summary
├── analyse.py               # post-soak metrics summary
├── install.sh               # idempotent first-run setup
├── mako-zero.service        # systemd unit (installed to /etc/systemd/system/)
├── config.yaml              # secrets + tuning (chmod 600, gitignored)
├── prompts/
│   ├── system.md            # worker prompt — does the work
│   ├── compact.md           # used on compaction ticks
│   └── scribe.md            # writer prompt — drafts blog posts
├── state/                   # files Mako reads + rewrites every tick
│   ├── MISSION.md           # frozen, edited only by Chris
│   ├── CAPABILITIES.md      # current access list, edited by Chris
│   ├── STATE.md             # short snapshot, Mako rewrites each tick
│   ├── NEXT.md              # what next tick should pick up
│   ├── PERSONA.md           # grows over time, Mako appends
│   ├── JOURNAL.md           # append-only, last 20 lines in hot context
│   ├── LAST_RESULTS.md      # results of last tick's actions
│   ├── INBOX.md             # YOU write here to steer Mako; cleared after each tick
│   ├── tick_counter.txt
│   ├── compact_pending.flag # presence triggers compaction tick
│   └── next_notes.json      # which notes/ files Mako wants loaded next tick
├── notes/                   # Mako's long-form research / plans
│   └── INDEX.md
├── workdir/                 # sandboxed shell + drafts
├── archive/                 # rolled-out journals after compaction
├── pending/
│   └── pending_actions.jsonl   # gated actions waiting on Chris
├── state/outbox/blog/drafts/   # scribe's blog drafts awaiting your approval
└── logs/
    ├── metrics.csv             # one row per tick — for the 48h analysis
    ├── ticks/<NNNNNNNN>.json   # FULL request/response payload per worker tick
    ├── scribe/<NNNNNNNN>.json  # FULL request/response payload per scribe run
    └── error-N.log             # tracebacks for failing ticks
```

## Two cooperating loops

`supervisor.py` (systemd `Type=simple`, restart on failure) runs both
the worker and the scribe sequentially in one process. No overlap by
construction — only one subprocess fires at a time.

### Worker tick (`tick.py`, ~60s cadence)

1. If `state/compact_pending.flag` exists → compaction mode, else normal.
2. Assemble hot context (~7K tokens) plus up to 3 notes files Mako
   requested last tick + INBOX.md if Chris dropped one.
3. LLM call: Ollama Cloud (`qwen3.5`) primary, OpenCode Go (`qwen3.5`)
   fallback.
4. Extract JSON from the response.
5. Apply file writes (writable_paths only — code/prompts/config blocked).
6. Execute non-gated actions; queue gated ones to `pending_actions.jsonl`.
7. Write `LAST_RESULTS.md` for next tick to read.
8. Append to journal. Maybe set compaction flag.
9. Post a tick summary to the Telegram log thread.
10. Append a row to `logs/metrics.csv` AND a full payload to
    `logs/ticks/<NNNNNNNN>.json`.

### Scribe run (`scribe.py`, ~30 min cadence)

1. Read MISSION, PERSONA, last 100 journal lines, notes index, recent
   notes, and the existing blog drafts in the outbox.
2. LLM call (same provider chain).
3. Either drafts a blog post into `state/outbox/blog/drafts/` and pings
   the approvals thread, or explicitly skips with a reason.
4. Never runs actions, never modifies worker state.
5. Full payload to `logs/scribe/<NNNNNNNN>.json`.

The scribe is the writer; the worker is the doer. They share the same
persona and the same brand.

## Feedback channels

**Mako → you (outbound):**
- Telegram **Log thread** — one post per tick (blow-by-blow).
- Telegram **Approvals thread** (or Requests if approvals isn't set up,
  or Log if neither) — one post each time a gated action is queued.
  Each post includes a `qN` id and the action's key fields.
- Telegram **Requests thread** — when Mako uses `ask_chris`.
- Telegram **Digest thread** (or Log if unset) — one daily summary at
  `supervisor.digest_hour_local` (08:00 by default).
- Failure Telegram post (in Log) when a tick errors out.

**You → Mako (inbound):**
- Write into `state/INBOX.md`. The next tick reads it as the most
  important input, acknowledges it in `work_done`, and adjusts
  `NEXT.md`. The wrapper archives it after that tick. Easiest:
  ```bash
  cat > /srv/mako-zero/state/INBOX.md <<'EOF'
  Stop researching for a bit, draft the first blog post.
  EOF
  ```
- Edit `state/CAPABILITIES.md` to mark blocked things active.
- Edit `state/MISSION.md` to retune the goal (frozen by convention,
  but you're admin).
- Approve a queued gated action: read `pending/pending_actions.jsonl`,
  do the action manually (V0 has no auto-executor — V1 will).

## Action types

**Non-gated** (executed in-tick):
- `shell` — runs as root with cwd = `workdir/`, 30s timeout, denylist
  blocks the obviously catastrophic (`rm -rf /`, `mkfs`, `shutdown`)
- `http_get` — read-only fetch, response truncated to 8KB
- `write_file` — paths under `state/`, `notes/`, `workdir/`, `archive/`,
  `pending/`; `tick.py`/`prompts/`/`config.yaml`/etc. forbidden
- `read_file` — anywhere under `/srv/mako-zero/`
- `git` — local repo only, no `push`
- `telegram_post` — uses the bot
- `ask_chris` — posts to Requests thread

**Gated** (queued to `pending/pending_actions.jsonl`, executed by Chris):
- `email_send`
- `cf_api`
- `http_post|put|delete`
- `spend` over £2

Any action with `needs_approval: true` is queued regardless of type.

## Full-payload logging

Every worker tick and every scribe run writes a complete
request/response payload to disk:

- `logs/ticks/<NNNNNNNN>.json` — system prompt, user message, all
  attempted LLM provider calls (request body + response body, with
  `Authorization` headers redacted), parsed JSON response, files
  written, action results, telegram posts, errors.
- `logs/scribe/<NNNNNNNN>.json` — same shape for scribe runs.

This is the forensic log for offline analysis (post-mortems, prompt
tuning, model evaluation). The summary stats remain in
`logs/metrics.csv` — fast to read at scale, but not enough detail when
something weird happens.

Storage is ~50–100KB per tick + similar per scribe run. Roughly
40MB/day at default cadence. Disable via `logging.full_payload: false`
in config, or rotate manually:

```bash
find /srv/mako-zero/logs/ticks  -mtime +14 -delete
find /srv/mako-zero/logs/scribe -mtime +14 -delete
```

## Token budgets

- Input cap: 18,000 tokens (4 chars/token heuristic). Over-cap → next
  tick auto-runs in compaction mode.
- Output cap: 12,000 tokens (`num_predict` on Ollama, `max_tokens` on
  OpenCode).
- Total per tick: ~30K tokens.
- Wall clock: 180s primary, 120s fallback. The supervisor runs ticks
  sequentially, so no overlap is possible — a long tick just delays
  the next one.

## Install — clean Ubuntu 24.04 server (with systemd)

These steps assume a fresh Hetzner / DigitalOcean / etc. VPS with
Ubuntu 24.04 LTS, dedicated to Mako (single-purpose box). Everything
runs directly on the host; the supervisor manages the loop under
systemd. Mako runs as root by design — the box is his.

### 1. Server prep (as root)

```bash
# patch
apt-get update && apt-get -y upgrade

# basic dependencies
apt-get -y install python3 python3-pip git ca-certificates curl

# basic firewall
apt-get -y install ufw
ufw default deny incoming
ufw default allow outgoing
ufw allow OpenSSH
ufw --force enable
```

If you want to add ports later for Mako-hosted sites (80/443 for
nginx/caddy), Mako can do that himself with `ufw allow http https`
once he's running.

### 2. Get the code

```bash
cd /srv
git clone https://github.com/minkforge/mako-zero.git
cd mako-zero
```

(Or rsync from your laptop into `/srv/mako-zero/` if you're iterating
locally and don't want to push through GitHub between every change.)

### 3. Run install.sh

```bash
bash /srv/mako-zero/install.sh
```

This:
- creates the directory tree under `/srv/mako-zero/`
- copies code into place
- seeds `state/MISSION.md`, `CAPABILITIES.md`, etc. (idempotent — won't
  clobber edits)
- installs Python deps (`requests`, `PyYAML`)
- seeds `/srv/mako-zero/config.yaml` from `config.example.yaml`
- installs the systemd unit at `/etc/systemd/system/mako-zero.service`
- runs `systemctl daemon-reload`

### 4. Configure secrets

```bash
$EDITOR /srv/mako-zero/config.yaml
chmod 600 /srv/mako-zero/config.yaml
```

Required fields before first real tick:

- `llm.primary.api_key` — Ollama Cloud key
- `llm.fallback.api_key` — OpenCode Go key
- `telegram.bot_token`
- `telegram.chat_id`
- `telegram.{log,requests,approvals,digest}_thread_id`

The provider URLs and model ids ship pre-filled (`qwen3.6` on both
sides) so you only fill in the keys.

### 5. Start the service

```bash
systemctl enable --now mako-zero
journalctl -u mako-zero -f
```

Within ~5s you should see:

```
[supervisor] starting (tick every 60s, digest at 08:00 local)
[supervisor] tick(normal): start ...
[supervisor] tick(normal): done rc=0 in N.Ns
```

And the first tick should post to your Telegram log thread.

### 6. Useful commands

```bash
# tail logs
journalctl -u mako-zero -f
journalctl -u mako-zero --since "1 hour ago"

# control
systemctl status mako-zero
systemctl restart mako-zero
systemctl stop mako-zero

# fire a digest now (don't wait until 08:00)
python3 /srv/mako-zero/digest.py --config /srv/mako-zero/config.yaml

# post-soak metrics analysis after 24-48h
python3 /srv/mako-zero/analyse.py --config /srv/mako-zero/config.yaml

# steer Mako mid-run
cat > /srv/mako-zero/state/INBOX.md <<'EOF'
Pause research for now, draft the first blog post.
EOF
# next tick (within ~60s) consumes it and archives to archive/inbox-*.md

# upgrade after pulling new code
cd /srv/mako-zero && git pull
bash /srv/mako-zero/install.sh    # idempotent
systemctl restart mako-zero
```

### 7. Optional: automatic security updates

```bash
apt-get -y install unattended-upgrades
dpkg-reconfigure -plow unattended-upgrades
```

## Install on other Linux distros

The install script is portable to any systemd-based Linux. You'll need
Python 3.10+, `pip`, and `git`. Adjust package install commands per
distro, then run `install.sh` as root from the cloned repo.

## 48-hour soak: what to watch

- `logs/metrics.csv` — eyeball for input_tokens_est, output_chars,
  wall_s. Plot the distribution. Check for parse failures.
- `pending/pending_actions.jsonl` — what is Mako trying to do that
  needs your approval? Triage daily.
- Telegram log thread — sanity check the narrative.
- `state/JOURNAL.md` and `notes/` — is the agent learning, or
  rediscovering the same things?

After 48h, total Ollama Cloud usage (check their dashboard) tells you
if the cadence is sustainable. If you're well under quota, drop
`tick_interval_s` further; if you're brushing it, raise to 120s or
300s and re-soak.

## Things deliberately not in V0

- No browser automation (no Playwright). Read-only HTTP only.
- No vector store, no RAG. Notes are markdown, agent steers context
  via `request_notes`.
- No live conversational Telegram bot. Just outbound updates from
  ticks; you reply asynchronously and Chris's replies will land in
  CAPABILITIES/MISSION/Telegram check-ins, not in real-time chat.
  (V1 adds this.)
- No multi-project scheduler. One agent, one focus at a time. (V1 adds
  the project abstraction.)
- No encrypted secrets. Config is a flat file; chmod 600 it. Move to
  age/sops in V1.

## Resetting

Wipe runtime state but keep code + prompts + config:

```bash
systemctl stop mako-zero
rm -rf /srv/mako-zero/state/* \
       /srv/mako-zero/notes/* \
       /srv/mako-zero/workdir/* \
       /srv/mako-zero/archive/* \
       /srv/mako-zero/pending/* \
       /srv/mako-zero/logs/*
bash /srv/mako-zero/install.sh   # re-seeds state from seed/, won't touch config
systemctl start mako-zero
journalctl -u mako-zero -f
```

To go further and reset the config too, also delete `config.yaml` —
`install.sh` will reseed it from `config.example.yaml` and you'll need
to re-paste your secrets.
