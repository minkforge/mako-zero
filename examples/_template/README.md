# Example — _template

Blank scaffold for spinning up a new Mako pointed at a different
mission. Copy this directory, fill in the placeholders.

## What to fill in

1. **`MISSION.md`** — the goal, the time horizon, the constraints,
   what success isn't. ≤ 1KB.
2. **`CAPABILITIES.md`** — every tool + status marker. Be explicit
   about limitations.
3. **`STATE.md`** — usually just "tick #0 — orienting". The engine
   rewrites this each tick.
4. **`NEXT.md`** — first instruction. "Read MISSION + CAPABILITIES,
   write notes/orientation.md, plan the first three concrete actions."
5. **`PERSONA.md`** — initial voice. The engine grows this over time;
   you just seed it.
6. **`notes/INDEX.md`** — start with one line; the engine adds rows
   as Mako writes notes.

Then provide a **README.md** alongside (like the income-experiment
one) explaining what config keys, telegram threads, and external
accounts this Mako needs.

## Things to be deliberate about

- **What's the mission's natural cadence?** A research Mako that
  produces one daily summary doesn't need 5-min ticks. Bump
  `tick_interval_s` to 1800 (30 min) and save tokens.
- **Is the scribe useful?** A Mako that doesn't blog can disable the
  scribe entirely (`scribe.enabled: false`).
- **Does this Mako need Cloudflare / Fastmail / Telegram approval?**
  Strip the actions you don't want it emitting in `prompts/system.md`'s
  Tools section so it doesn't propose things that'll always be denied.
- **What's the kill criterion?** What would make you stop this Mako?
  Document it so a future-you can decide cleanly.
