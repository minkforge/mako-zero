# mako-zero

A small autonomous-agent loop. One LLM call per cron tick, structured
JSON response, applied by a thin Python wrapper. Posts a blow-by-blow
update to Telegram after every run. Self-documenting via files Mako
writes itself.

This is V0 — the smallest thing that exercises the full loop end-to-end:
provider call (with fallback), structured response, file writes, real
tool execution for safe tools, queued approval for risky ones, Telegram
update, metrics CSV.

## Layout (on the VPS)

```
# on the VPS (bare deploy):
/srv/mako-zero/
├── mako-tick.sh             # cron entrypoint with flock (every 2 min)
├── mako-digest.sh           # daily digest cron entrypoint (1x/day)
├── tick.py                  # the main logic file
├── digest.py                # daily summary script
├── analyse.py               # post-soak metrics summary
├── supervisor.py            # used by Docker; ignored on bare VPS
├── config.yaml              # secrets + tuning (chmod 600)
├── prompts/
│   ├── system.md            # Mako's system prompt
│   └── compact.md           # used on compaction ticks
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
└── logs/
    ├── metrics.csv          # one row per tick — for the 48h analysis
    └── error-N.log          # tracebacks for failing ticks
```

## Per-tick flow

1. Cron fires `mako-tick.sh` every 2 min. `flock -n` ensures no overlap.
2. If `state/compact_pending.flag` exists → compaction mode, else normal.
3. `tick.py` assembles hot context (~7K tokens) plus up to 3 notes files
   that Mako requested last tick.
4. LLM call: Ollama Cloud (kimi-k2.6) primary, OpenCode fallback.
5. Extract JSON from the response.
6. Apply file writes (sandboxed to writable_paths).
7. Execute non-gated actions; queue gated ones to `pending_actions.jsonl`.
8. Write `LAST_RESULTS.md` for next tick to read.
9. Append to journal. Maybe set compaction flag.
10. Post a tick summary to the Telegram log thread.
11. Append a row to `logs/metrics.csv`.

## Feedback channels

**Mako → you (outbound):**
- Telegram **Log thread** — one post per tick (blow-by-blow).
- Telegram **Approvals thread** (or Requests if approvals isn't set up,
  or Log if neither) — one post each time a gated action is queued.
  Each post includes a `qN` id and the action's key fields.
- Telegram **Requests thread** — when Mako uses `ask_chris`.
- Telegram **Digest thread** (or Log if unset) — one daily summary at
  the time of your `mako-digest.sh` cron entry.
- Failure Telegram post (in Log) when a tick errors out.

**You → Mako (inbound):**
- Write into `state/INBOX.md`. The next tick reads it as the most
  important input, acknowledges it in `work_done`, and adjusts
  `NEXT.md`. The wrapper archives it after that tick. Easiest:
  ```bash
  sudo -u mako-zero tee /srv/mako-zero/state/INBOX.md <<'EOF'
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
- `shell` — sandboxed to `workdir/`, denylist + 30s timeout
- `http_get` — read-only fetch, response truncated to 8KB
- `write_file` — paths under `state/`, `notes/`, `workdir/`, `archive/`,
  `pending/` only
- `read_file` — anywhere under root
- `git` — local repo only, no `push`
- `telegram_post` — uses the bot
- `ask_chris` — posts to Requests thread

**Gated** (queued to `pending/pending_actions.jsonl`, executed by Chris):
- `email_send`
- `cf_api`
- `http_post|put|delete`
- `spend` over £2

Any action with `needs_approval: true` is queued regardless of type.

## Token budgets

- Input cap: 18,000 tokens (4 chars/token heuristic). Over-cap → next
  tick auto-runs in compaction mode.
- Output cap: 12,000 tokens (`num_predict` on Ollama, `max_tokens` on
  OpenCode).
- Total per tick: ~30K tokens.
- Wall clock: 180s primary, 90s fallback. `flock` ensures no overlap if
  a tick runs long.

## Install — clean Ubuntu 24.04 server (recommended)

These steps assume a fresh Hetzner / DigitalOcean / etc. VPS with
Ubuntu 24.04 LTS. Everything runs in Docker; nothing else is installed.

### 1. Initial server hardening (one-off)

As root on the new box:

```bash
# patch and reboot if a kernel update lands
apt-get update && apt-get -y upgrade

# create a deploy user with sudo
adduser --disabled-password --gecos '' deploy
usermod -aG sudo deploy
mkdir -p /home/deploy/.ssh
cp ~/.ssh/authorized_keys /home/deploy/.ssh/authorized_keys
chown -R deploy:deploy /home/deploy/.ssh
chmod 700 /home/deploy/.ssh
chmod 600 /home/deploy/.ssh/authorized_keys

# disable root login over ssh, password auth off
sed -i 's/^#\?PermitRootLogin.*/PermitRootLogin no/' /etc/ssh/sshd_config
sed -i 's/^#\?PasswordAuthentication.*/PasswordAuthentication no/' /etc/ssh/sshd_config
systemctl reload ssh

# basic firewall (Hetzner already restricts inbound, but belt-and-braces)
apt-get -y install ufw
ufw default deny incoming
ufw default allow outgoing
ufw allow OpenSSH
ufw --force enable
```

Log out, back in as `deploy`, and confirm `sudo -v` works before
continuing.

### 2. Install Docker (official repo, not the apt one)

```bash
sudo apt-get -y install ca-certificates curl gnupg

sudo install -m 0755 -d /etc/apt/keyrings
curl -fsSL https://download.docker.com/linux/ubuntu/gpg \
  | sudo gpg --dearmor -o /etc/apt/keyrings/docker.gpg
sudo chmod a+r /etc/apt/keyrings/docker.gpg

echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.gpg] \
  https://download.docker.com/linux/ubuntu $(. /etc/os-release && echo "$VERSION_CODENAME") stable" \
  | sudo tee /etc/apt/sources.list.d/docker.list > /dev/null

sudo apt-get update
sudo apt-get -y install docker-ce docker-ce-cli containerd.io \
  docker-buildx-plugin docker-compose-plugin

# allow the deploy user to run docker without sudo
sudo usermod -aG docker $USER
newgrp docker

# smoke test
docker run --rm hello-world
```

### 3. Get the code onto the server

If pulling from GitHub:

```bash
cd /opt
sudo mkdir -p mako-zero && sudo chown $USER mako-zero
git clone https://github.com/minkforge/mako-zero.git mako-zero
cd mako-zero
```

Or rsync from your laptop:

```bash
# from your laptop
rsync -av --exclude data --exclude .git /path/to/mako-zero/ \
  deploy@<server-ip>:/opt/mako-zero/
```

### 4. Set up the data volume + first boot

```bash
cd /opt/mako-zero
mkdir -p data
sudo chown -R 10001:10001 data       # match the UID baked into the image
docker compose build                  # ~40s
docker compose up -d
docker compose logs -f mako-zero      # watch for "[bootstrap] seeded data/config.yaml"
# ctrl-c when you see the message
```

The supervisor will idle until you populate the config.

### 5. Configure secrets

```bash
sudo $EDITOR /opt/mako-zero/data/config.yaml
sudo chmod 600 /opt/mako-zero/data/config.yaml
sudo chown 10001:10001 /opt/mako-zero/data/config.yaml
```

Required fields before first real tick:

- `llm.primary.api_key` — Ollama Cloud key
- `llm.primary.model` — exact model id (e.g. `kimi-k2.6` or whatever
  Ollama exposes for it)
- `llm.fallback.base_url` + `api_key` + `model` — OpenCode endpoint
- `telegram.bot_token`
- `telegram.chat_id`
- `telegram.{log,requests,approvals,digest}_thread_id`

### 6. Restart and verify

```bash
docker compose restart
docker compose logs -f mako-zero
# expect within ~5s:
#   [supervisor] tick(normal): start ...
#   [supervisor] tick(normal): done rc=0 in N.Ns
```

Then check Telegram — you should see the first tick post in the Log
thread within 30s.

### 7. Useful one-offs

```bash
# fire a digest now (don't wait until 08:00)
docker compose exec mako-zero python3 /app/digest.py --config /data/config.yaml

# soak analysis after 24-48h
docker compose exec mako-zero python3 /app/analyse.py --config /data/config.yaml

# steer Mako mid-run
sudo tee /opt/mako-zero/data/state/INBOX.md <<'EOF'
Pause research, draft the first blog post.
EOF
sudo chown 10001:10001 /opt/mako-zero/data/state/INBOX.md

# watch the live log
docker compose logs -f mako-zero

# upgrade after pulling new code
git pull
docker compose build
docker compose up -d         # state persists in ./data
```

### 8. Optional: set up automatic security updates

```bash
sudo apt-get -y install unattended-upgrades
sudo dpkg-reconfigure -plow unattended-upgrades
```

---

## Install — Docker on any other host

For non-Ubuntu hosts, install Docker per your distro's instructions
then follow steps 3–7 of the Ubuntu section above.

Container internals:
- `supervisor.py` runs as PID 1, handles SIGTERM cleanly.
- Tick subprocess on `tick_interval_s` (default 120s).
- Daily digest at `digest_hour_local` (default 08:00, container TZ defaults to `Europe/London`).
- Healthcheck verifies `tick_counter.txt` was bumped in the last 10 min.
- All stdout streamed to `docker compose logs` with rotation (10MB × 5).
- State, notes, archive, logs, pending, AND config.yaml all live in `./data` on the host. Image is rebuildable without losing state.

## Install — bare VPS without Docker (alternative)

```bash
# from your laptop:
rsync -av --exclude '.git' /tmp/mako-zero/ root@hetzner:/tmp/mako-zero/
ssh root@hetzner

# on the box:
useradd -r -m -d /srv/mako-zero -s /bin/bash mako-zero || true
mkdir -p /srv/mako-zero
chown mako-zero:mako-zero /srv/mako-zero
sudo -u mako-zero bash /tmp/mako-zero/install.sh

# fill in the config
sudo -u mako-zero $EDITOR /srv/mako-zero/config.yaml
sudo -u mako-zero chmod 600 /srv/mako-zero/config.yaml

# manual test (one tick)
sudo -u mako-zero MAKO_ROOT=/srv/mako-zero /srv/mako-zero/mako-tick.sh
tail /srv/mako-zero/logs/metrics.csv
cat /srv/mako-zero/state/LAST_RESULTS.md

# enable cron (every 2 minutes for the loop, daily digest at 08:00)
sudo crontab -u mako-zero -e
# add:
# */2 * * * * /srv/mako-zero/mako-tick.sh
# 0 8 * * *   /srv/mako-zero/mako-digest.sh
```

## 48-hour soak: what to watch

- `logs/metrics.csv` — eyeball for input_tokens_est, output_chars,
  wall_s. Plot the distribution. Check for parse failures.
- `pending/pending_actions.jsonl` — what is Mako trying to do that
  needs your approval? Triage daily.
- Telegram log thread — sanity check the narrative.
- `state/JOURNAL.md` and `notes/` — is the agent learning, or
  rediscovering the same things?

After 48h, total Ollama Cloud usage (check the dashboard) tells you if
2-min cadence is sustainable. If yes, leave it. If no, raise to 5-min
in cron and re-soak.

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

```bash
# wipe state but keep code + prompts + config:
rm -rf /srv/mako-zero/state/*  /srv/mako-zero/notes/*  /srv/mako-zero/workdir/*  /srv/mako-zero/archive/*  /srv/mako-zero/pending/*  /srv/mako-zero/logs/*
sudo -u mako-zero bash /tmp/mako-zero/install.sh   # re-seed
```
