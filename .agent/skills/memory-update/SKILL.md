---
name: memory-update
description: "Route durable session facts into the correct .agent/memory/ file without duplication or secrets. Use at session end / 'save' / 'wrap up', after KILL verdicts, incidents, durable decisions, user corrections, or notable delegation outcomes."
---

# Skill: memory-update

## When to use
Session end / "save" / "wrap up"; after any KILL verdict, incident, durable
decision, or user correction; after a delegation pattern works or fails.

## Required inputs
The session's durable facts (not chat noise).

## Procedure
1. Classify each fact → target file per `.agent/memory/README.md` routing
   table (gotcha / testing lesson / decision / risk / routing / open question /
   failed attempt / successful pattern / overview).
2. Search the target file for an existing entry; UPDATE rather than duplicate;
   delete entries proven wrong.
3. Convert relative dates to absolute (YYYY-MM-DD). Include the why, not just
   the what. Cite commits/paths.
4. Do not record: anything derivable from code/git/CLAUDE.md; secrets,
   credentials, account IDs; one-off conversational context.
5. Keep entries <=10 lines; move long narratives to a dated topic file and
   link it.
6. Promotion/retirement pass: any lesson now appearing twice in
   `model-routing.md` → promote into the relevant SKILL.md and replace the
   memory prose with a one-line pointer; delete lessons superseded by skill
   text; collapse outcome entries >1 quarter old with no unique lesson into
   the scoreboard counts.
7. Session-ending invocation ("save" / "wrap up" / session end): run the
   full §Session wrap-up checklist below, in order.

## Session wrap-up (ordered checklist — run top to bottom)

Replaces the prose convention "update current_session.md at session end";
a wrap-up that skips a step states which and why.

1. [ ] `current_session.md`: refresh Last Updated / Status / Blockers /
       Context; update the branch registry (`.agent/rules/30-git.md`
       §Branch discipline); refresh the resumable block of any task that
       outlives this session (task-intake §8); delete blocks of completed
       tasks.
2. [ ] Lessons routing: run Procedure steps 1–6 on every durable fact from
       the session.
3. [ ] `current-risks.md`: add new active risks (owner + expiry condition);
       delete resolved ones, noting resolution in the relevant lessons file.
4. [ ] `open-questions.md`: add newly blocked decisions; move resolved ones
       out, citing where the answer landed.
5. [ ] Uncommitted agent-system sweep (same-session commit rule):
       `git status --short -- CLAUDE.md AGENTS.md .agent/` — any modified
       governance file needs its `docs(agents):` narrow-gate commit +
       `.agent/CHANGELOG.md` line before the session ends (hand the commit
       to the commit-work skill; this skill writes only memory files).
       Agent-system debt does not cross sessions.
6. [ ] Dual-memory cross-check: apply `.agent/memory/README.md` §Division
       of labor — shareable lessons found only in private memory move to
       repo memory; user preferences found in repo memory move to private.

## Safety rules
Memory files are the only files this skill may write. Never store secrets.
(Wrap-up step 5 detects governance debt but delegates the commit to the
commit-work skill.)

## Output format
List of files updated with one-line summary of each change. For a wrap-up:
the checklist with each step ticked or explicitly skipped-with-reason.

## Validation checklist
- [ ] No duplicates created
- [ ] Dates absolute
- [ ] No secrets
- [ ] Wrong/stale entries corrected, not appended-around
- [ ] Session-ending runs: all 6 wrap-up steps ticked or skip-justified

## Example prompt
"memory-update: capture today's shioaji 1.5.3 golden-guard findings and the
executor-packet pattern that worked."
