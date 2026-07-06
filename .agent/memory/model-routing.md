# Model Routing (tiers + observed delegation outcomes)

Record here: the operative tier table (mirrors AGENTS.md) and OBSERVED
outcomes — which delegations succeeded/failed by surface and why; packet
lessons. Do NOT record: generic model claims; single anecdotes (wait for a
2nd occurrence before writing a pattern).

## Scoreboard (input to AGENTS.md demotion/promotion rules)

| Model | Task type | Attempts | Successes | Interventions |
|---|---|---|---|---|
| Haiku 4.5 | docs/mechanical | 1 | 1 | 0 |
| Sonnet | Tier-2 code+test | 1 | 1 | 1 (report nudge) |

## Record schema (write after EVERY delegation, success or not)

```
### <date> · Tier <n> · <task type> · <surface> · <model> · <SUCCESS|PARTIAL|FAIL|ESCALATED>
Interventions: <n> · Cost: <tokens/time if known> · Net win vs doing directly: <y/n/unclear>
Lessons: <=3 bullets, only if new.
```

Update the scoreboard in the same edit. Bad-packet failures: fix the packet,
don't demote the model. Lessons appearing twice → promote into the relevant
SKILL.md and replace with a pointer (see `memory-update` skill).

## Tier table (authoritative copy in AGENTS.md)
- Tier 1 (docs/comments/test-only/scratch): Haiku/Sonnet executor, Sonnet review.
- Tier 2 (non-hot-path src, CLI, reports, ops scripts): Sonnet executor,
  Sonnet review + Fable spot-check.
- Tier 3 (hot path, contracts/events, pricing/timebase, broker adapters,
  risk/order/execution/gateway, recorder/WAL, Rust, migrations, alpha
  governance, Do-NOT-Edit list): tight packet or Fable directly;
  Fable/Opus review MANDATORY.
- Tier X (live/prod ops, git surgery, secrets, dependency pins, frozen
  registry/profiles): never delegated; Fable + explicit user confirmation.

## Observed outcomes
(Each entry: date, tier, surface, executor model, outcome, packet lesson.)

### 2026-07-06 · Tier 1 · docs/mechanical · MODULES_REFERENCE.md count re-verification · Haiku 4.5 · SUCCESS
Interventions: 0 · Cost: ~69K tokens / 3 min · Net win vs doing directly: no (capability probe)
Pilot delegation via small-model-handoff → worktree-isolated executor →
strict-code-review. Executor corrected 17 numeric claims; every number matched
the orchestrator's independently pre-computed ground truth; scope held (1 file,
prose untouched, no git commands, zero escalations); ~69K tokens / 81 tool
uses / ~3 min. Review verdict APPROVE with no diff findings.
Packet lessons:
- The packet's hand-typed "rows to check" enumeration omitted one row (`core`);
  the executor correctly let the general rule ("every row with a bold count")
  win and disclosed the extra edit. → Generate enumerations from commands, and
  state precedence explicitly: general rule beats enumerated list.
- Giving exact count COMMANDS (not answers) worked: deterministic for the
  executor, still independently checkable by the reviewer. Reuse this shape for
  any mechanical-verification task.
- One data point only — do not generalize to Tier-2 code tasks yet; next pilot
  should be a Tier-2 non-hot-path code+test change (Sonnet executor).

### 2026-07-06 · Tier 2 · code+test · CLI un-skip live→sim downgrade regression test · Sonnet · SUCCESS
Interventions: 1 (report-delivery nudge) · Cost: not measured · Net win vs doing directly: unclear (capability probe)
Real recorded debt: `test_cmd_run_downgrades_live` skipped since the
prometheus-mock era, leaving the fail-safe live→sim credential downgrade with
zero regression coverage. Packet prescribed an extraction-only refactor in
`cli/_run.py` + helper-level tests. Executor delivered exactly that: 2 files,
byte-identical warning string, 3 behavior-named tests, single-seam
monkeypatch, no sys.modules stubbing. Verification: 22 passed/0 skipped on
the file; full `make test` 14013 passed, coverage 87.77%; ruff/format/
hygiene/discipline/boundary all green; orchestrator break-probe confirmed the
new tests fail when the downgrade condition is broken. Executor correctly
ESCALATED (not fixed) 2 pre-existing mypy `unused-ignore` errors in off-limits
shioaji files — exactly the stop-condition behavior the packet asked for.
Packet lessons:
- Worktree isolation conflicts with tasks that must RUN the test suite: a
  fresh worktree lacks the built venv/Rust artifacts. Pattern that worked:
  main-tree execution + 2-file allowlist + orchestrator before/after
  `git status --porcelain` snapshot + hash-verified rollback plan. (Skill
  updated with this caveat.)
- Background-spawned executors can go idle without delivering their report;
  nudge via SendMessage, but never block review on the self-report — the
  orchestrator's independent diff/validation found the same facts first.
- "Expect clean" verification commands need a pre-existing-red escape hatch:
  the packet's "make typecheck (expect clean)" was unsatisfiable on a branch
  with unrelated debt; the stop-condition ("errors in files you did not
  change") saved it. Word future packets that way from the start.
