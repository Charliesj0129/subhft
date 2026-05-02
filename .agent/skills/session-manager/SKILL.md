<!-- REVIEW-2026-04-17: unreferenced by rules/workflows/teams/agents. Confirm or delete. -->
---
name: session-manager
description: Manages conversation context — saves session state, triggers lesson extraction, and restores context on session resume. Use on "Pause", "Wrap up", "Save", or at session boundaries.
---

# Session Manager

## On Session Save (user says "Pause", "Wrap up", "Save")

1. **Write session state** to `.agent/memory/current_session.md`:
   ```markdown
   ## Session: YYYY-MM-DD
   - **Goal**: <what we were working on>
   - **Status**: completed | in-progress | blocked
   - **Key changes**: <files modified, commits made>
   - **Next steps**: <what to do next>
   - **Blockers**: <anything blocking progress>
   ```

2. **Lesson extraction** — ask: "Did we fix a tricky bug or discover a non-obvious pattern?"
   - If YES: append to `.agent/memory/lessons_learned.md`:
     ```markdown
     ## [TYPE] Title (YYYY-MM)
     **Context**: what happened
     **Fix**: what we did
     **Rule**: what to remember
     **Commit**: hash (if applicable)
     ```
     TYPE = `[BUG]`, `[PERF]`, `[ARCH]`, `[GOTCHA]`

3. **Git hygiene check**:
   ```bash
   git stash list                    # Should be <= 3
   git branch | wc -l                # Should be <= 10
   git branch | grep worktree-agent  # Should be 0
   git status --short                # Should be clean or intentional
   ```

4. **Report**: "Session saved. [Lesson logged: <title>]" or "Session saved. No new lessons."

## On Session Restore (session start or "Hello")

1. Read `.agent/memory/current_session.md`
2. Read `.agent/memory/lessons_learned.md` (last 20 lines)
3. Read `.agent/memory/module_gotchas.md` (if exists)
4. Report: "Restored session from [Date]. Last goal was: [Goal]. Status: [Status]."
5. Check if any blockers from last session are still relevant.

## Quick Reference Files

| File | Purpose |
| --- | --- |
| `.agent/memory/current_session.md` | Current session state |
| `.agent/memory/lessons_learned.md` | Accumulated lessons (append-only) |
| `.agent/memory/module_gotchas.md` | Non-obvious module behaviors |
| `.agent/memory/codebase_map.md` | Directory layout reference |
