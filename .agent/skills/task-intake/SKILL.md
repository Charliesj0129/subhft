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

### 3. Route by ROI — direct / delegate / fan-out / stop
Delegation is NOT free: a subagent starts cold (re-derives context the
orchestrator already holds) and its output must still be independently
reviewed, so for small serial tasks delegation costs MORE than doing it
directly (see the net-win column in `.agent/memory/model-routing.md`). Default
is DIRECT; delegation must pay for itself. Pick exactly one route, record WHY:

- **direct** — orchestrator does it. Required for small / single-file /
  low-risk tasks where the orchestrator already has enough context, and ALWAYS
  for Tier-X, review, and git. MUST log a one-line `direct reason`.
- **delegate** — one subagent, one concern. Only if a trigger fires:
  - a large read-only exploration whose full context should stay OUT of the
    orchestrator (saving: context isolation),
  - bulk same-shape mechanical edits large enough to amortize packet+review
    (saving: cheaper model),
  - long-running test-writing / repeated fixes / batch validation
    (saving: long-running labor),
  - work needing independent / adversarial review (saving: review separation).
- **fan-out** — 3+ independent sub-tasks runnable in parallel
  (saving: parallelism).
- **stop** — blocked on a user decision, Tier-X confirmation, or missing input.

If no delegate/fan-out trigger fires, the answer is direct — do not manufacture
a packet for a task cheaper done directly. Consult the class's scoreboard in
`.agent/memory/model-routing.md`; never widen a class's validated scope
silently. Two prior failures of the class/model pair → route per the demotion
rule.

### 4. Evidence before spawn (mandatory for any delegation)
Save to the scratchpad BEFORE spawning: baseline `git status --porcelain`;
pre-edit blob hashes of every allowlisted file; privately-computed expected
results / answer key. Claims without artifacts are graded attested-only.

### 5. Handoff packet
Author per `small-model-handoff` — short packet for Tier-1 / very small Tier-2,
full 12-field packet for high-risk or multi-file (that skill decides the size).
Venue decision, escape-hatch verification wording, explicit ALLOWED FILES.
Executors never run git. One packet = one agent = one concern.

### 6. Spawn mechanics
FIRST verify the session is NOT in plan mode — subagents inherit it as
system-enforced read-only and burn tokens on a plan they cannot execute
(model-routing 2026-07-08 BLOCKED-BY-HARNESS: ~120K tokens wasted). If in plan
mode, exit it before spawning, or tell the user.

Agent tool, `subagent_type: general-purpose` (or `Explore` for pure read-only
fan-out). Assign the CHEAPEST capable model — this is where 降本 actually comes
from, not from spawning itself:
- `model: haiku` — docs, counting, path verification, simple mechanical edits.
- `model: sonnet` — bounded code+test, test writing, read-only investigation,
  medium mechanical edits.
- Opus/orchestrator keeps orchestration, routing, review, git, and final
  acceptance — never delegated.
For a single bounded task use `run_in_background: false` — a backgrounded
executor's plain-text final report is INVISIBLE outside its session. If
backgrounding for parallelism, the packet must require report delivery via
SendMessage, and review never blocks on the self-report.

### 7. Validation plan (written at intake, not after the fact)
State before spawning: which commands prove the change; the break-probe for
any code fix (snapshot fixed file → `git checkout --` to the committed buggy
baseline → run new test expecting the exact failure → restore snapshot →
re-verify blob hash); how pre-existing red gates will be adjudicated
(re-run against the baseline; failures must pre-date the change).

### 8. Checkpoint long tasks (resume without re-derivation)
If the task is expected to outlive one context window (multi-wave plan, >3
verifiable units, or known compaction risk), maintain a resumable block in
`.agent/memory/current_session.md`, updated in place after EACH verifiable
unit: done units (with commit hashes), the exact next step, and verification
state (commands already green vs still owed). A fresh session must be able to
resume from the block alone, without re-deriving decisions from the
transcript. Delete the block when the task completes.

### 9. Review
Per `strict-code-review` Step 0: scope-diff vs the baseline snapshot,
personal re-run of every verification command, break-probe, red-gate
adjudication, ground-truth cross-check. The executor self-report is context,
never evidence.

### 10. Git (only after review passes, only if a commit is in scope)
`branch-safety-check` skill, then
`ALLOWED_PATHS="<task files>" bash scripts/check_git_preconditions.sh
--narrow-commit`; commit only on exit 0. Local commits only — push, merge,
and anything destructive stay human-approved per operation.

### 11. Memory update plan
After EVERY delegation (success or not): ledger entry + scoreboard update in
`.agent/memory/model-routing.md` (schema there), PLUS the archive file the
schema's Archive field points to — packet + executor report + review verdict,
verbatim, at `.agent/memory/delegations/` (see its README). Route other
durable lessons per `memory-update`. Commit the ledger and archive through
the same narrow gate when committing task work.

### 12. Report to user
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
`## Task` (restated, with done condition) ·
`## Classification` (type / tier) ·
`## Routing` — one of {direct, delegate, fan-out, stop} + ROI reason; if
delegate/fan-out, the expected cost-saving type (cheaper model / parallelism /
context isolation / long-running labor); if direct, the `direct reason`
(small / single-file / low-risk / already-have-context) ·
`## Plan` (packet summary if delegating, validation, review, memory) — then
execution, then the §12 report.

## Validation checklist
- [ ] Task restated with an observable done condition
- [ ] Type + tier announced before any work
- [ ] Routing decided (direct / delegate / fan-out / stop) with an ROI reason
- [ ] direct route carries a logged `direct reason`; delegate/fan-out names the
      expected cost-saving type
- [ ] Scoreboard consulted for the class's validated scope
- [ ] Session confirmed NOT in plan mode before any spawn
- [ ] Cheapest capable model assigned to each subagent
- [ ] Evidence artifacts saved before spawn
- [ ] Validation + review plans written at intake
- [ ] Long task (>1 context window expected)? checkpoint block maintained in
      `.agent/memory/current_session.md` per §8
- [ ] Ledger updated (incl. net-win) + delegation archive file written after
      the delegation
- [ ] User report lists checks NOT run

## Example prompt
"Fix this skipped CLI test" → intake announces: code+test, Tier 2, Sonnet,
delegate; packet per `small-model-handoff`; break-probe validation;
`--narrow-commit` gate; `model-routing.md` ledger entry.
