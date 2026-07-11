---
name: read-only-audit
description: "Read-only project/state audit before planning any change. Use at session start, on unfamiliar ground, before Tier-3 work, or when asked 'what is the state of X'. Produces evidence-cited findings without modifying anything."
---

# Skill: read-only-audit

## When to use
Start of any session or task on unfamiliar ground; before planning any change;
when asked "what's the state of X". Always before Tier-3 work (see `AGENTS.md`).

## Required inputs
Task statement; current branch name.

## Procedure
1. `git status --short` + `git log --oneline -10` — record branch, dirty files,
   unpushed commits. Do NOT touch dirty files later.
2. Read `docs/MODULES_REFERENCE.md`, `.agent/rules/00-index.md`,
   `.agent/skills/00-index.md`; open only task-relevant rules/skills.
3. Read `.agent/memory/module_gotchas.md` entries for modules in scope.
4. `rg`/Read the actual source for every behavioral claim you will make.
5. Note contradictions between docs and source explicitly as [DRIFT].
6. Write findings; make NO edits, run NO state-changing commands.

## Safety rules
Read-only means: no Edit/Write outside scratchpad, no git state changes, no
docker/service commands, no installs. Guarded queries only for ClickHouse
(`make ch-query-guard-check` / `ch-query-guard-run`).

## Output format
`## State` (branch/dirty/unpushed) · `## Findings` (each with file:line
evidence) · `## Drift/uncertainty` · `## Recommended next action`.

## Validation checklist
- [ ] Zero files modified (`git status` unchanged)
- [ ] Every claim has a path or command citation
- [ ] Uncertainty marked, not smoothed over

## Example prompt
"Run read-only-audit on the recorder WAL replay path before we plan the
dedup change; I need current behavior, gotchas, and test coverage."
