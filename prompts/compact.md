# Compaction tick.

Your context is about to overflow, or you flagged a compaction yourself.
This tick is not for new work. It is for trimming and consolidating.

You receive the same hot context as a normal tick, plus the FULL current
JOURNAL.md (not just the last 20 lines).

Your tasks this tick:

1. **Distil JOURNAL.md into 3–7 durable lessons** and append them to
   `notes/learnings.md` (mode: append). Lessons are short, specific,
   and useful for future-you. Not "I should research more" but "Reddit
   r/SideProject moderation is strict — read sidebar before posting."

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

Output schema is the same as a normal tick. Set `compact_now: false`
in your output (the wrapper handles clearing the flag).
