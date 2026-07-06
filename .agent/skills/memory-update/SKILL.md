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
7. Also update `.agent/memory/current_session.md` per
   `.agent/rules/05-project-structure.md`.

## Safety rules
Memory files are the only files this skill may write. Never store secrets.

## Output format
List of files updated with one-line summary of each change.

## Validation checklist
- [ ] No duplicates created
- [ ] Dates absolute
- [ ] No secrets
- [ ] Wrong/stale entries corrected, not appended-around

## Example prompt
"memory-update: capture today's shioaji 1.5.3 golden-guard findings and the
executor-packet pattern that worked."
