# Examples — scaffolds for spawning new Mako instances

`mako-zero` is split into:
- **Engine** — the recursive learning loop. Lives in
  `tick.py` / `supervisor.py` / `scribe.py` / `meta.py` /
  `tg_listener.py` / `cfg_cmd.py` / `dashboard/` / `prompts/system.md` /
  `prompts/compact.md` / `prompts/scribe.md` / `prompts/meta.md`.
  These files don't change between projects.
- **Mission** — the per-project specialisation. Lives in
  `state/MISSION.md` and `state/CAPABILITIES.md` (frozen, Chris-only)
  plus the seed `state/STATE.md`, `state/NEXT.md`, `state/PERSONA.md`,
  and any starter `notes/`. These define what *this* Mako does.

The engine is generic. Drop a different mission into a fresh
`/srv/mako-{name}/` directory and you have a different agent — same
ticking heartbeat, same prompt protocol, same dashboard, but pointed
at a different task with different tools and constraints.

## Layout of an example

```
examples/<name>/
├── README.md           # what this Mako is, who it's for, what it
│                       # needs in config (api keys, telegram threads)
├── MISSION.md          # frozen mission, edited only by you
├── CAPABILITIES.md     # tool catalogue + statuses, edited as access
│                       # is granted/revoked
├── STATE.md            # initial state — usually empty/minimal
├── NEXT.md             # first tick's instruction
├── PERSONA.md          # initial voice/identity (the engine grows it)
└── notes/
    └── INDEX.md        # starter notes, e.g. orientation.md, plan.md
```

## How to spawn a new Mako (V0 — manual)

Until we have a `bin/spawn-project.sh` helper:

1. Pick a name. Let's say `mako-research`.
2. `cp -r /srv/mako-zero /srv/mako-research`
3. `rm -rf /srv/mako-research/state/* /srv/mako-research/notes/* \
          /srv/mako-research/workdir/* /srv/mako-research/archive/* \
          /srv/mako-research/pending/* /srv/mako-research/logs/* \
          /srv/mako-research/.git`
4. `cp -r examples/<your-mission>/* /srv/mako-research/state/`
   (re-arrange so MISSION.md, CAPABILITIES.md, etc end up directly in
   `/srv/mako-research/state/`, and notes/ goes to `/srv/mako-research/notes/`)
5. `cp /srv/mako-research/config.example.yaml /srv/mako-research/config.yaml`
   then fill in keys/threads — **distinct telegram chat or threads**
   per Mako so you don't blur their voices.
6. Adjust `paths:` block in the new `config.yaml` to point at
   `/srv/mako-research/...` instead of `/srv/mako-zero/...`.
7. Copy `mako-zero.service` to `/etc/systemd/system/mako-research.service`,
   change `WorkingDirectory` and `ExecStart` paths.
8. `systemctl daemon-reload && systemctl enable --now mako-research`.

V1 will collapse this into a `spawn-project.sh` script.

## Available examples

- **`income-experiment/`** — make £100/mo on a £100/mo budget. The
  current `mako-zero` running on srv1 is seeded from this example.
- **`_template/`** — minimal blank scaffold to fork.

## Notes on writing a good mission

A good `MISSION.md`:
- States the **goal** in one sentence at the top.
- Defines the **time horizon** (don't ship-fast pressure; or do —
  but be explicit).
- Lists **non-negotiable constraints** (legal, ethical, budget).
- Says what success **isn't** — at least as important as what it is.
- Stays small. < 1KB ideally. The engine reads it every tick; bloat
  is expensive.

A good `CAPABILITIES.md`:
- Lists every tool / API / account with a status marker
  (✅ active, ⚠️ partial, ❌ blocked, ◻️ missing).
- States the **limitations** explicitly. The biggest source of
  wasted ticks is a Mako proposing strategies that need tools he
  doesn't have.
- Stays current — when you grant access, update it; when you revoke,
  update it. The engine treats it as ground truth.
