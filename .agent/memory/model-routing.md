# Model Routing (tiers + observed delegation outcomes)

Record here: the operative tier table (mirrors AGENTS.md) and OBSERVED
outcomes — which delegations succeeded/failed by surface and why; packet
lessons. Do NOT record: generic model claims; single anecdotes (wait for a
2nd occurrence before writing a pattern).

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

### 2026-07-06 · Tier 1 · docs (MODULES_REFERENCE.md count re-verification) · Haiku 4.5 · SUCCESS
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
