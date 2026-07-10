# .agent/memory — Routing Table

One fact goes in exactly one file. Update in place; never append a duplicate.
Absolute dates (YYYY-MM-DD) only. NEVER store secrets, credentials, or account
IDs. Entries <=10 lines; long narratives go to a dated topic file, linked.

| File | Purpose | Update when |
|---|---|---|
| `project-overview.md` | Stable orientation for cold-start agents | Architecture-level change only (rare) |
| `architecture-decisions.md` | Why-records for decisions invisible in code | Decision made or reversed |
| `module_gotchas.md` | Non-obvious per-module behavior (existing format) | Gotcha bites or is discovered; delete when fixed |
| `lessons_learned.md` | Legacy mixed lessons (existing; prefer the specific files below for new entries) | — |
| `testing-lessons.md` | Test-INFRA traps and fixture patterns | Test infrastructure (not product) was the problem |
| `current-risks.md` | Live, expiring risk register with owner + expiry condition | Every session that touches/observes a risk; prune aggressively |
| `model-routing.md` | Model-tier table + observed delegation outcomes | After each delegation with a notable outcome |
| `open-questions.md` | Unresolved decisions: what blocks them, who decides | Add when blocked; move out when resolved |
| `failed-attempts.md` | Refuted approaches + research KILL index — do not re-walk | Immediately after any refuted approach |
| `successful-patterns.md` | Recipes confirmed >=2 times | On second confirmation; promote mature ones into a rule/skill |
| `current_session.md` | Session state handoff (existing convention) | Session end / "save" / "wrap up" |
| `delegations/` (dir) | Verbatim packet + executor report + review verdict per delegation, one file `YYYY-MM-DD-<slug>.md` (see its README) | With every new `model-routing.md` ledger entry, which links it |

What does NOT belong here: anything derivable from code, git history, or
CLAUDE.md; one-off conversational context; secrets of any kind.

## Division of labor: repo memory vs orchestrator private memory

Two memory systems coexist and have already diverged once (ROI records in
repo, session state in private). Each fact lives in exactly one:

- **Repo `.agent/memory/` (committed, model-agnostic)**: anything a DIFFERENT
  agent or model working this repo would need — delegation outcomes and
  routing evidence, research verdicts and refuted approaches, gotchas, risks,
  open questions, session handoff state. Committed skill/rule EXAMPLE text
  stays public-literature-only; real findings belong in these memory files.
- **Orchestrator private memory (per-user, outside the repo)**: who the user
  is, preferences and feedback on how to work, cross-session conversational
  context, pointers to private artifacts. Project facts another agent would
  need do NOT stay there — they move here.
- **Wrap-up cross-check**: once at session end (memory-update skill
  §Session wrap-up), scan both for strays — shareable lessons found only in
  private memory move here; user preferences found here move to private.

Maintained via the `memory-update` skill (`.agent/skills/memory-update/SKILL.md`).
