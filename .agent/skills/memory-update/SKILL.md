---
name: memory-update
description: "Route durable session facts into the right .agent/memory file without duplicates or secrets, and wrap up a session. Use at save or wrap-up, and after an incident, KILL verdict, durable decision, or user correction. Not for chat noise or facts derivable from code or git."
---

# Memory update

Writes only memory files, never secrets.

## Route each fact

One fact lives in exactly one file; the routing table is `.agent/memory/README.md`
(gotcha, testing lesson, decision, risk, routing outcome, open question, failed
attempt, successful pattern). Then:

1. Search the target for an existing entry and update it; delete entries proven wrong.
2. Use absolute dates (YYYY-MM-DD), the why as well as the what, and cite commits or paths.
3. Skip anything derivable from code, git, or `AGENTS.md`; skip one-off chat context.
4. Keep entries under ~10 lines; push long narratives to a dated topic file and link it.
5. A lesson that appears twice in `model-routing.md` moves into the relevant
   SKILL.md, leaving a one-line pointer. Drop lessons the skill text now covers.

## Session wrap-up (only when ending or asked to save)

- `current_session.md`: status, blockers, next step; update the branch registry
  (`.agent/rules/30-git.md`); keep a resumable block for work that outlives the
  session and delete blocks for finished work.
- `current-risks.md`: add new active risks (owner + expiry condition); remove resolved ones.
- `open-questions.md`: add newly blocked decisions; move answered ones out, citing where.
- Check `git status --short -- AGENTS.md CLAUDE.md .agent/`: governing-doc edits
  left uncommitted go to `git-safety`, plus an `.agent/CHANGELOG.md` line.
- Hygiene glance: `git stash list`, stray `worktree-agent-*` branches, leftover worktrees.
- Repo memory holds what another agent working here would need; per-user
  preferences stay in private memory. Move strays across.

## Done when

No duplicates, dates absolute, no secrets, stale entries corrected rather than
appended around. Report the files updated with one line each.
