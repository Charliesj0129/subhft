---
name: preflight
description: "Read-only state and risk check before a change: branch, dirty and unpushed work, red CI gates, risk surfaces, reversibility, required gates. Use at session start, before Tier-2/3 or production-adjacent work, or when asked the state of X. Not for making changes."
---

# Preflight

Read-only. Edit nothing, change no git or service state, install nothing.

## 1. State

- `git status --short` and `git log --oneline -10`; classify each dirty file as
  yours, the user's concurrent work, or unknown, and never touch the last two.
- Unpushed or unbacked commits are at risk (`git log --branches --not
  --remotes --oneline`). Local-only history has existed here; treat it as irreplaceable.
- External gate health at session start (say "gates not checked" if `gh` or the
  network is unavailable): `gh run list --limit 15`, and the dormant legs:
  `gh run list --workflow=ci.yml --event=schedule --limit 3`, `--workflow=codeql.yml`,
  `--workflow=deploy.yml`. Report any red or startup_failure run before planning.
  A scheduled gate once sat red for months unnoticed.
- Is a live or sim engine running that the change could affect? Open alerts?
  `.agent/memory/current-risks.md` has the known risks.

## 2. Risk framing (Tier-2/3 or production-adjacent work)

| Question | Look at |
|---|---|
| Does it touch a Do-NOT-Edit path, hot path, contracts, migrations, goldens, pins, or frozen research state? | `AGENTS.md` red lines; each hit raises the tier and needs a stated reason |
| Is dirty user work or an unpushed commit in the blast radius? | step 1 |
| How is it undone? Anything involving production or data needs the user first | rollback written concretely |
| Which gates make it done? | `AGENTS.md` "Done means" |

## 3. Evidence

Read the source for every behavioral claim; open `.agent/memory/module_gotchas.md`
for modules in scope; use guarded ClickHouse queries only (`make ch-query-guard-check`,
`make ch-query-guard-run`). Mark conflicts between docs and source `[DRIFT]`.

## Output

`## State` · `## Findings` (file:line or command evidence) · `## Touched risk
surfaces` · `## Reversibility` · `## Required gates` · `## Blockers needing a user
decision` · `## Recommended next action`.

## Done when

`git status` is unchanged, every claim cites a path or command, uncertainty is
marked rather than smoothed over, and user-decision items are separated out.
