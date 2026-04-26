# Compaction tick.

Your context is about to overflow, or you flagged a compaction yourself.
This tick is not for new work. It is for trimming and consolidating.

You receive the same hot context as a normal tick, plus the FULL current
JOURNAL.md (not just the last 20 lines).

Your tasks this tick:

1. **Distil JOURNAL.md into 3–7 durable lessons** and append them to
   `notes/learnings.md` (mode: append). Lessons are short, specific,
   and useful for future-you. Not "I should research more" but
   "Cloudflare DNS API rejects A records when content is empty — must
   include `content` field even on PATCH."

2. **Trim JOURNAL.md to its last 10 lines.** Write the trimmed lines to
   `archive/journal-{YYYY-MM-DD-HH}.md` (mode: write).

3. **Refresh notes/INDEX.md** if any notes were added or are missing.
   One line per file: `notes/x.md — short purpose statement.`

4. **Rewrite STATE.md fresh** from current understanding. Drop stale
   detail. Keep it ≤1KB.

5. **Set NEXT.md** to the next concrete forward step (this is what the
   next normal tick will pick up).

6. **Do not emit any actions[].** Compaction is a pure-thinking tick.
   `actions: []`.

7. **`work_done` is still mandatory.** Even on a compaction tick the
   wrapper rejects an empty/missing `work_done`. Summarise what you
   distilled in 1–3 lines (e.g. "compaction: 5 lessons appended,
   journal trimmed to last 10 lines, STATE rewritten").

8. **If an INBOX is present, do NOT silently compact.** Acknowledge
   each item Chris raised in `work_done` first. If addressing the
   INBOX is more urgent than this compaction, defer the compaction
   (set `compact_now: false`, do `actions: [...]` as a normal tick) —
   the wrapper will re-fire compaction next tick if it's still needed.

Output schema is the same as a normal tick. Set `compact_now: false`
in your output (the wrapper handles clearing the flag).
