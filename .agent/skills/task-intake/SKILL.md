---
name: task-intake
description: "Orchestrator entry point: convert a natural-language user task ('fix this test', 'check this warning', 'update this doc') into the Agent System v2 workflow — classify task type + risk tier, decide delegate-vs-direct, then produce the handoff packet, validation plan, review plan, and memory-update plan. Run FIRST for every incoming task, before any edit or delegation."
---

# Skill: task-intake

## When to use
FIRST, for every incoming natural-language task, however small. This skill
turns a one-line request into the governed workflow so the user never has to
paste orchestration instructions. Skip only when the user explicitly dictates
the complete workflow themselves, or explicitly says "no intake".

## Required inputs
The user's task statement; current git state; the `AGENTS.md` routing tables
(authority for roles, tiers, and model boundaries — this skill applies them,
it never redefines them).

## Procedure

### 1. Ground the task in repo evidence
Do the CLAUDE.md Retrieval First reads, then the specific surface the task
names. Restate the task as: surface + defect/goal + observable done
condition. Ambiguous after reading source (two defensible scopes, unclear
target)? Ask the user ONE clarifying question — never guess scope in a
money-facing repo.

### 2. Classify (type, tier, model)
Per the two `AGENTS.md` routing tables. A task's tier is the tier of the
RISKIEST file it must touch. Intake hints for common phrasings:

| User says | Usual type | Usual tier / route |
|---|---|---|
| "fix this test / bug" in CLI, reports, ops scripts | code+test | 2 → Sonnet |
| "check / investigate / why does X warn" | bounded read-only investigation | 1 → Sonnet/Explore, zero edits |
| "update this stale doc / path / count" | docs/mechanical | 1 → Haiku (Sonnet if any design choice) |
| "clean up lint/mypy/format debt" | gate cleanup | 2 → Sonnet |
| names hot path, contracts/events, pricing/timebase, broker adapter, risk/order/execution, recorder/WAL, Rust, migrations, alpha governance | — | 3 → STOP, confirm scope with user first |
| live/prod ops, git surgery, pins, frozen profiles, secrets | — | X → never delegated; explicit user confirmation |

Announce the classification to the user BEFORE work: type, tier, executor
model, delegate-vs-direct. This is the user's chance to veto cheaply.

### 3. Decide delegate vs direct
Check the class's scoreboard in `.agent/memory/model-routing.md` first:
- Class validated at this scope → delegate per the tier table.
- Task exceeds the class's validated scope (e.g. >3 files, cross-module)
  → shrink it, run it directly, or flag to the user that this is a widening
  probe. Never widen a class silently.
- Two prior failures of the class/model pair → route per the demotion rule.
- Trivial (<~10 min direct, packet would cost more) → direct is fine; say so.

### 4. Evidence before spawn (mandatory for any delegation)
Save to the scratchpad BEFORE spawning: baseline `git status --porcelain`;
pre-edit blob hashes of every allowlisted file; privately-computed expected
results / answer key. Claims without artifacts are graded attested-only.

### 5. Handoff packet
Author per `small-model-handoff` (canonical template, venue decision,
escape-hatch verification wording, explicit ALLOWED FILES). Executors never
run git. One packet = one agent = one concern.

### 6. Spawn mechanics
Agent tool, `subagent_type: general-purpose`; `model: haiku` (Tier-1
mechanical) or `model: sonnet` (Tier-2). For a single bounded task use
`run_in_background: false` — a backgrounded executor's plain-text final
report is INVISIBLE outside its session. If backgrounding for parallelism,
the packet must require report delivery via SendMessage, and review never
blocks on the self-report.

### 7. Validation plan (written at intake, not after the fact)
State before spawning: which commands prove the change; the break-probe for
any code fix (snapshot fixed file → `git checkout --` to the committed buggy
baseline → run new test expecting the exact failure → restore snapshot →
re-verify blob hash); how pre-existing red gates will be adjudicated
(re-run against the baseline; failures must pre-date the change).

### 8. Review
Per `strict-code-review` Step 0: scope-diff vs the baseline snapshot,
personal re-run of every verification command, break-probe, red-gate
adjudication, ground-truth cross-check. The executor self-report is context,
never evidence.

### 9. Git (only after review passes, only if a commit is in scope)
`branch-safety-check` skill, then
`ALLOWED_PATHS="<task files>" bash scripts/check_git_preconditions.sh
--narrow-commit`; commit only on exit 0. Local commits only — push, merge,
and anything destructive stay human-approved per operation.

### 10. Memory update plan
After EVERY delegation (success or not): ledger entry + scoreboard update in
`.agent/memory/model-routing.md` (schema there); route other durable lessons
per `memory-update`. Commit the ledger through the same narrow gate when
committing task work.

### 11. Report to user
Final message: what changed; commands run with output excerpts; checks NOT
run; delegation outcome (model, interventions count); commit hash or "not
committed". If running as a subagent, deliver via SendMessage.

## Safety rules
Never let a small-looking task skip intake — misclassification is how Tier-3
edits sneak in. Tier-3/X surfaces, Do-NOT-Edit paths, production/live ops,
dependency pins, golden regeneration, secrets → stop and confirm with the
user before proceeding. Preserve every dirty file you did not create.

## Output format
An intake block announced before execution:
`## Task` (restated, with done condition) · `## Classification`
(type / tier / model / delegate?) · `## Plan` (packet summary, validation,
review, memory) — then execution, then the §11 report.

## Validation checklist
- [ ] Task restated with an observable done condition
- [ ] Type + tier + model announced before any work
- [ ] Scoreboard consulted for the class's validated scope
- [ ] Evidence artifacts saved before spawn
- [ ] Validation + review plans written at intake
- [ ] Ledger updated after the delegation
- [ ] User report lists checks NOT run

## Example prompt
"Fix this skipped CLI test" → intake announces: code+test, Tier 2, Sonnet,
delegate; packet per `small-model-handoff`; break-probe validation;
`--narrow-commit` gate; `model-routing.md` ledger entry.
