# Alpha Candidate Loop v1 — Implementation Spec

Status: SPEC (approved 2026-06-11, not yet implemented)
Owner: research
Scope: batch alpha-FACTOR candidate generation/validation/evaluation loop. Not a strategy system.

Pinned decisions:

- Data scope: full ~Feb–Jun 2026 TXF L2 span, including pre-2026-03-31 days where trade
  direction is unrecorded; `trade_imbalance` candidates evaluate only on direction-clean days
  (per-day `dir_coverage >= 0.95`, true from ~2026-03-31 / E6-era onward).
- v1.0 generation: JSONL drop only (`--from-jsonl`); no LLM API call inside the loop.
- New code lives in one package: `research/candidate_loop/`. Nothing in `src/` imports it.

## 1. Executive Summary

Alpha Candidate Loop v1 is a batch pipeline that turns cheap-model-generated JSON alpha-factor
candidates into recorded, versioned, auditable verdicts. Cheap models generate strict-JSON
candidates per family → a deterministic validator/compiler accepts or kills them → a
deterministic evaluator scores survivors against cached per-day L2 primitive panels → results
go to ClickHouse (registry + summary metrics) and Parquet (detailed diagnostics) → a
deterministic failure summarizer produces `failure_summary.json` → (v1.1) Fable 5 reads the
summary and rewrites next-generation prompts, with all system-change proposals quarantined in
`pending_governor_actions`.

It reuses existing repo machinery: hftbt.npz L2 data and its replay logic
(`research/tools/regime_lab/snapshot_builder.py`), IC/metric functions
(`research/tools/batch_alpha_eval.py`, `research/backtest/metrics.py`), cost/latency configs
(`config/research/cost_profiles.yaml`, `config/research/latency_profiles.yaml`), the ClickHouse
client factory (`src/hft_platform/infra/ch_client.py`), migration conventions
(`src/hft_platform/migrations/clickhouse/`), and the kill-ledger dual-sink writer pattern
(`src/hft_platform/alpha/kill_ledger.py`).

Success = the loop runs 120 candidates end-to-end with every candidate reaching a final status,
every rejection carrying a death_reason, all results queryable and versioned, and zero promoted
candidates being a valid outcome.

## 2. System Boundary

In scope: factor candidates (formulas over 8 whitelisted L2 primitives), schema validation,
formula compilation, point-in-time predictive evaluation, crude cost proxy, label-shift latency
stress, append-only recording, failure summarization, prompt-rewrite governance with human
approval.

Out of scope (hard): entry/exit rules, position sizing, risk rules, execution, fill models,
queue models, paper/live trading, portfolio construction, dashboards. PROMOTED ≠ tradable; it
means "passed v1 hard gates, top 1% by score, enters stricter retest queue."

Governance boundaries:

1. Generators produce factor candidates only.
2. Fable 5 cannot change the judge — schema/evaluator/scoring/primitive/promotion changes go to
   `pending_governor_actions` and require human approval before activation.
3. Test-split results are recorded but mechanically excluded from everything the generator and
   governor see.

Relation to existing alpha governance: this loop sits BEFORE the Gate A–F pipeline. A PROMOTED
factor is a candidate input to the existing governed alpha workflow
(`docs/runbooks/alpha-development-workflow.md`), never a bypass of it. The live registry stays
frozen per loop_v1 L11.

## 3. Milestones

### v1.0 — Smoke Loop

6 families × 20 candidates = 120, ingested from pre-generated JSONL (`--from-jsonl`).

Complete when: all 120 reach final statuses; `research.alpha_candidates` and
`research.experiment_results` written to ClickHouse; Parquet diagnostics written; every
REJECTED/INVALID has a death_reason; `failure_summary.json` produced. Zero promotions is a
valid outcome.

### v1.1 — Governor Loop

Fable 5 reads failure_summary + scoreboard (train+validation only) and produces family-specific
second-round prompts (versioned, `prompts/v2/`), `research_notes.md`, and
`pending_governor_actions.jsonl`.

Complete when: round-2 prompts explicitly cite round-1 failure patterns; prompt versions are
recorded in candidate provenance; the second smoke batch runs the full loop; no
evaluator/schema/scoring change is activated without human approval.

### v1.2 — Expansion Batch

6 families × 100 = 600 candidates through the same loop.

Complete when: family survival rates and death-reason distributions are queryable;
WATCHLIST/PROMOTED queues are produced; the top-1% promotion rule runs
(`ceil(survivors × 1%)`); the failure summary feeds generation round 3.

## 4. Component Architecture

New package `research/candidate_loop/`:

```
research/candidate_loop/
├── __main__.py          # CLI: generate / run / summarize / promote
├── schema.py            # msgspec.Struct candidate types, Status/DeathReason enums, alpha_id hashing
├── grammar.py           # tokenizer + recursive-descent parser (style of src/hft_platform/alpha/dsl/parser.py,
│                        #   extended with Call/kwarg/string/division/Compare nodes; no eval)
├── validator.py         # staged validation → INVALID|COMPILED, death_reason mapping, complexity, dedupe
├── panels.py            # per-(data_version, day) primitive panel build + npz cache + dir_coverage flag
├── primitives.py        # the 8 primitives + 4 transforms over a Panel
├── compiler.py          # tree-walk AST evaluator → np.ndarray (style of src/hft_platform/alpha/dsl/compiler.py)
├── evaluator.py         # per-candidate per-split metrics; latency shift; cost proxy
├── scoring.py           # hard gates + versioned final_score + top-1% promotion
├── splits.py            # split_definition yaml loader/validator (explicit day lists)
├── ch_writer.py         # append-only CH inserts + jsonl fallback (kill-ledger pattern)
├── artifacts.py         # Parquet tree + diagnostics.json
├── failure_summary.py   # train+validation-only aggregation → failure_summary.{json,md}
├── generate.py          # v1.0: --from-jsonl ingest with provenance header (LLM wiring deferred to v1.1)
├── prompts/v1/          # one .md per family, frontmatter prompt_id/version (repo-versioned)
├── candidates/          # <generation_run_id>/family=<f>.jsonl (gitignored)
├── runs/                # <run_id>/ state, fallback jsonl, failure_summary (gitignored)
├── governor/            # v1.1: pending_governor_actions.jsonl, next_generation_prompts/, research_notes.md
└── GOVERNOR.md          # v1.1 documented procedure
```

Config: `config/research/candidate_loop/{split_definition_v1.yaml, evaluator_v1.yaml, scoring_v1.yaml}`.

Migrations: `src/hft_platform/migrations/clickhouse/20260612_001_create_research_alpha_candidates.sql`
and `..._002_create_research_experiment_results.sql` — new `research` database (`audit` is for
governance/compliance records; experiment telemetry does not belong there).

Tests: `tests/unit/research/candidate_loop/` (behavior-named per `.agent/rules/50-testing.md`).

Dependency change: add `pyarrow` to the research/dev dependency group (currently dev-only) for
Parquet writes.

Roles → components: Cheap Model Generator = outside the loop (produces JSONL drops);
Schema/Formula Compiler = `schema.py` + `grammar.py` + `validator.py` + `compiler.py`;
Batch Evaluator = `panels.py` + `primitives.py` + `evaluator.py` + `scoring.py`;
Failure Summarizer = `failure_summary.py` (deterministic); Fable 5 Governor = v1.1 procedure +
artifact contracts only — there is no code path through which it mutates the system.

## 5. Data Flow

```
prompts/v1/<family>.md ──(cheap model, outside loop)──► candidates/<gen_run>/family=<f>.jsonl
        │
        ▼  python -m research.candidate_loop run --batch <run_id>
[1] ingest: JSONL → Candidate structs → alpha_candidates rows (status=NEW)
[2] validate/compile: grammar + whitelist + args + complexity + dedupe
        → INVALID(+death_reason) | COMPILED
[3] panels: for each split day, load_or_build_panel(data_version, day)
        (cached npz; dir_coverage recorded; built once, shared by ALL candidates)
[4] evaluate: outer loop days, inner loop candidates → per-day metric rows
        (signal computed once per candidate-day; labels via local_ts asof; 0/1/5/10ms shifts)
[5] reduce per split (train/validation) → experiment_results rows + Parquet artifacts → EVALUATED
[6] score/gate → REJECTED(+death_reason) | WATCHLIST | PROMOTED (appended status rows)
[7] test-split pass: evaluate WATCHLIST+PROMOTED on test days; rows recorded, never summarized
[8] failure_summary.py (train+validation rows only) → runs/<run_id>/failure_summary.{json,md}
        │
        ▼  v1.1
Fable 5 reads failure_summary + prompts/v1 → next_generation_prompts/ + research_notes.md
        + pending_governor_actions.jsonl ──(human approval)──► prompts/v2/ → next batch
```

All event timing uses `local_ts` (availability time), never `exch_ts`, for label alignment.
Per `.agent/rules/70-research-data.md`: npz price scale is ×1,000,000 from the CH export;
panels convert to float points once at build time (offline research float math is allowed per
`.agent/rules/25-architecture-governance.md`).

## 6. Candidate JSON Schema

`schema.py`, msgspec.Struct (msgspec is a main dep; no pydantic):

```python
class ProposedPrimitive(msgspec.Struct, frozen=True):
    name: str
    reason: str
    required_data: list[str] = []
    not_executable_in_v1: bool = True     # must be true; validator rejects false

class Feature(msgspec.Struct, frozen=True):
    name: str          # ^[a-z][a-z0-9_]{2,48}$
    formula: str

class Candidate(msgspec.Struct, frozen=True):
    name: str          # ^[a-z][a-z0-9_]{2,64}$
    family: str        # ∈ {order_book_imbalance, microprice, depth_delta,
                       #    trade_flow, spread_regime, replenishment}
    hypothesis: str    # 20–500 chars
    features: list[Feature]            # 1–6 entries
    signal_formula: str                # over feature names + primitives + transforms
    label: str                         # exactly future_mid_return(horizon=...)
    horizon: str                       # 'Nms' | 'Ns' | 'N_events'
    expected_sign: str                 # 'positive' | 'negative'
    regime_filter: str = ""            # '' = always-on; else one comparison expr
    cost_risk: str = ""
    latency_risk: str = ""
    falsification_tests: list[str] = []
    proposed_new_primitives: list[ProposedPrimitive] = []   # recorded, NEVER executed
```

`alpha_id = sha256(canonical_json(candidate))[:16]` — content-addressed, so re-ingest is
idempotent. `formula_hash` (see §13/§14 dedupe) is separate: features are inlined into the
signal AST, args canonicalized, then hashed — renamed-but-identical formulas collide.

A JSON Schema document (`schema/candidate.schema.json`, generated from the structs) ships with
every generation prompt so cheap models target the exact contract.

## 7. Allowed Primitive Interface (`primitive_version = prim_v1`)

All primitives evaluate over a per-day `Panel` (event-clock rows = one row per depth-batch
flush, adapted from `snapshot_builder.py` ev-bit replay; columns: `local_ts`, L1–L5
`bid_px/bid_qty/ask_px/ask_qty`, `mid`, `microprice`, `spread_ticks`, cumulative
`trade_buy_qty`/`trade_sell_qty`, meta incl. `dir_coverage`). All windows are
trailing-inclusive (point-in-time safe).

| Primitive | Args | Semantics |
|---|---|---|
| `mid_price()` | — | (bid1+ask1)/2, points |
| `spread_ticks()` | — | (ask1−bid1)/tick_size (per-instrument constant in evaluator config) |
| `depth_sum(side, levels)` | side∈{bid,ask}; levels int 1..5 | Σ qty over levels 1..L |
| `book_imbalance(levels)` | levels 1..5 | (Σbid−Σask)/(Σbid+Σask), 0 when denom 0 |
| `microprice()` | — | (bid1·ask_qty1+ask1·bid_qty1)/(bid_qty1+ask_qty1) |
| `depth_delta(side, levels, window)` | window `'N_events'` (10–10000) or `'Nms'/'Ns'` (50ms–60s) | depth_sum[i] − depth_sum[idx(i,window)]; event window idx=i−N; time window via searchsorted(local_ts); NaN in warmup |
| `trade_imbalance(window)` | window as above | (Δbuy−Δsell)/(Δbuy+Δsell) over window from cumulative arrays; 0 when no trades. **Valid only on dir_clean days** |
| `future_mid_return(horizon)` | horizon `'Nms'/'Ns'` (100ms–30s) or `'N_events'` | LABEL ONLY. j = asof(local_ts[i]+h); mid[j]/mid[i]−1; NaN past end-of-day (no cross-day labels). Appearing in any feature/signal/regime AST → PRIMITIVE_INVALID |

Transforms (fixed whitelist, part of prim_v1; anything else fails compilation):
`zscore(x, window='2000_events')`, `negative_zscore(x, window='2000_events')`,
`ema(x, window)`, `clip(x, lo, hi)`. Rolling, trailing-inclusive.

Operators: `+ - * /` (safe divide: 0 where denom 0), unary `−`, parens. `regime_filter`
additionally allows exactly one top-level comparison (`<= >= < > ==`) compiling to a boolean
row mask.

Direction-clean masking (full-span data decision): the panel builder stamps
`dir_clean = (dir_coverage >= 0.95)` per day (true ≈ 2026-03-31 onward; B6/C6-era ≈ 0%,
D6-era partial → not clean). Candidates whose compiled AST references `trade_imbalance` get
`effective_days = split_days ∩ dir_clean_days`; all others use the full split day list.
`effective_day_count` per split is recorded in `experiment_results` and diagnostics; the
failure summary reports the reduced-coverage cohort per family.

## 8. ClickHouse Table Schemas

New `research` database. Both tables MergeTree, append-only; current state =
`argMax(status, inserted_at)` per alpha_id. Migrations follow the
`20260505_001_create_alpha_kill_ledger.sql` conventions (`-- Up`/`-- Down`, applied via
`recorder/schema.py::apply_schema()`); writes via `hft_platform.infra.ch_client.get_ch_client()`.

```sql
-- 20260612_001 -- Up
CREATE DATABASE IF NOT EXISTS research;
CREATE TABLE IF NOT EXISTS research.alpha_candidates (
    alpha_id        String,                          -- sha256(canonical candidate json)[:16]
    inserted_at     DateTime64(9,'UTC') DEFAULT now64(9,'UTC'),
    run_id          String,
    name            String,
    family          LowCardinality(String),
    status          Enum8('NEW'=1,'INVALID'=2,'COMPILED'=3,'EVALUATED'=4,
                          'REJECTED'=5,'WATCHLIST'=6,'PROMOTED'=7),
    death_reason    LowCardinality(String) DEFAULT '',
    hypothesis      String,
    candidate_json  String CODEC(ZSTD(3)),           -- verbatim original
    feature_formulas Array(String),
    signal_formula  String,
    label           String,
    horizon         String,
    expected_sign   Enum8('positive'=1,'negative'=2),
    regime_filter   String DEFAULT '',
    formula_hash    String,
    uses_trade_imbalance UInt8 DEFAULT 0,
    proposed_new_primitives Array(String),
    generation_model String, generation_prompt_id String, generation_run_id String,
    data_version LowCardinality(String), primitive_version LowCardinality(String),
    schema_version LowCardinality(String)
) ENGINE = MergeTree
PARTITION BY toYYYYMM(inserted_at)
ORDER BY (family, alpha_id, inserted_at);
```

```sql
-- 20260612_002 -- Up   (one row per alpha × split × version-tuple; result_id is the dedupe key)
CREATE TABLE IF NOT EXISTS research.experiment_results (
    result_id    String,   -- sha256(alpha_id:run_id:split:data_version:evaluator_version:scoring_version)
    inserted_at  DateTime64(9,'UTC') DEFAULT now64(9,'UTC'),
    experiment_id String, run_id String, alpha_id String, family LowCardinality(String),
    split Enum8('train'=1,'validation'=2,'test'=3),
    split_start Date, split_end Date,
    day_count UInt16, effective_day_count UInt16,
    ic Float64, rank_ic Float64, ic_tstat Float64,
    sign_consistency Float64, bucket_spread_pts Float64, bucket_monotonicity Float64,
    horizon_decay_halflife_ms Float64,
    day_stability Float64, one_day_concentration Float64,
    regime_ic_in Float64, regime_ic_out Float64,
    regime_ic_tight_spread Float64, regime_ic_wide_spread Float64, regime_stability Float64,
    train_validation_direction_match UInt8, validation_test_direction_match UInt8,
    turnover_proxy Float64, gross_pts_per_flip Float64,
    required_move_threshold_pts Float64, cost_survival_score Float64,
    latency_0ms_score Float64, latency_1ms_score Float64,
    latency_5ms_score Float64, latency_10ms_score Float64,
    final_score Float64 DEFAULT 0,
    gates_passed Array(String), gates_failed Array(String),
    status LowCardinality(String), death_reason LowCardinality(String) DEFAULT '',
    artifact_path String,
    data_version LowCardinality(String), primitive_version LowCardinality(String),
    evaluator_version LowCardinality(String), scoring_version LowCardinality(String),
    cost_assumption_version LowCardinality(String), latency_config_version LowCardinality(String),
    generation_model String, generation_prompt_id String, generation_run_id String
) ENGINE = MergeTree
PARTITION BY toYYYYMM(inserted_at)
ORDER BY (run_id, family, split, alpha_id);
```

ORDER BY rationale: scoreboard / survival-rate / death-reason queries are per-run grouped by
family → prefix scans.

No-overwrite rule: never UPDATE/DELETE; any new data/evaluator/scoring version produces new
`result_id` rows. `ch_writer.py` does an existence pre-check on the dedupe key and falls back
to `runs/<run_id>/_results_fallback.jsonl` when CH is unreachable (kill-ledger pattern), with a
replay command to flush the fallback.

## 9. Parquet Artifact Convention

Root `research/candidate_loop/artifacts/` (gitignored), written with pandas→pyarrow:

```
artifacts/
  panels/data_version=<V>/day=<D>/panel.npz + panel.meta.json     # shared primitive cache
  data_version=<V>/evaluator_version=<E>/alpha_id=<ID>/split=<S>/
      day_metrics.parquet            # per-day: ic, rank_ic, n_valid, signal_std, bucket_spread
      regime_metrics.parquet         # in/out of declared regime + wide/tight spread
      horizon_decay.parquet          # IC at {0.5,1,2,4}× declared horizon
      latency_stress.parquet         # per-day IC at 0/1/5/10ms
      signal_bucket_returns.parquet  # 5 quantile buckets × mean fwd return
      diagnostics.json               # gate trace, NaN/warmup accounting, effective_days,
                                     #   dir_clean coverage, panel meta refs, versions
```

Every directory level carries the version in the path (hive-style), so nothing is ever
overwritten across versions. `experiment_results.artifact_path` points at the `split=<S>/`
directory.

## 10. Evaluator Contract (`evaluator_version = eval_v1`)

Question answered: does this factor, point-in-time, show stable predictive structure for
short-horizon `future_mid_return`, surviving a crude cost proxy and a 1ms availability delay?
It does NOT answer tradability/execution/sizing.

Inputs: CompiledCandidate, data_version, split_definition, primitive_version,
evaluator_version (config `evaluator_v1.yaml`), cost_assumption_version
(`cost_profiles.yaml` ref), latency_config_version.

Mechanics: outer loop over days (one panel in memory), inner loop over candidates; signal
computed once per candidate-day; labels via local_ts asof join.

- Predictive: chunked Pearson IC + per-day IC series (reuse
  `research/backtest/metrics.py::compute_ic`), Spearman rank IC (reuse
  `batch_alpha_eval.py::information_coefficient`), `ic_tstat` over daily ICs
  (`compute_ic_ttest`), 5-bucket mean forward returns + monotonicity, sign_consistency =
  fraction of days where sign(daily IC) matches expected_sign, horizon decay at
  {0.5, 1, 2, 4}× declared horizon → halflife.
- Stability: day_stability = positive-IC-day fraction; one_day_concentration = max single-day
  share of Σ|bucket-spread contribution|; regime_stability from declared-regime mask + the
  canonical wide/tight-spread split (r33 pattern); train/validation and validation/test
  direction-match flags.
- Cost proxy (`cost_assumption_version = taifex_v1`): discretize signal to sign of zscore with
  ±0.5σ hysteresis; `turnover_proxy` = flips/day; `gross_pts_per_flip` = mean |mid move at
  declared horizon per flip|; `required_move_threshold_pts` = 2×(commission+tax pts/side from
  `config/research/cost_profiles.yaml`) + median spread pts;
  `cost_survival_score = gross_pts_per_flip / required_move_threshold_pts`. Deliberately
  crude, no fills.
- Latency stress (`latency_config_version = lat_shift_v1`): for δ∈{0,1,5,10}ms re-anchor the
  label at availability time `local_ts[i]+δ` (entry mid = mid at asof(t+δ), label horizon from
  there); recompute IC → `latency_<δ>ms_score = ic_δ / ic_0` (signed retention). Hard gate
  uses 1ms only; 5/10ms diagnostic.

Final score (`scoring_version = score_v1`, weights in `scoring_v1.yaml`):

```text
final_score =
  predictive_score
+ stability_score
+ cost_survival_score
+ latency_survival_score
- fragility_penalty
- turnover_penalty
- complexity_penalty
```

Each term is a normalized [0,1] component defined in the yaml. Any change to weights/terms
bumps scoring_version; old rows are never overwritten.

## 11. Generator Contract

v1.0: `python -m research.candidate_loop generate --run-id <gen_run> --family <f> --count 20
--prompt prompts/v1/<f>.md --from-jsonl <path>` — validates the drop's shape, prepends a
provenance header line
`{"_header":true, "prompt_id", "prompt_sha256", "generation_model", "generated_at",
"generation_run_id"}`, writes `candidates/<gen_run>/family=<f>.jsonl`. The cheap-model call
itself happens outside the loop (any model, any harness); v1.1 may add a thin `--model` path.

Generator MUST: emit strict JSON matching `candidate.schema.json`; stay inside its family's
boundary (family definitions live in the prompts); use only the 8 primitives + 4 transforms;
put anything else under `proposed_new_primitives`.

Generator MUST NOT: generate strategies/entries/exits/sizing; claim profitability; reference
future information; see test-split results ever.

Prompts are repo files `prompts/v1/<family>.md` with frontmatter
(`prompt_id: <family>__v1`, `schema_ref`, `primitive_version`); each prompt embeds the exact
primitive signatures and the JSON schema. Prompt content is part of provenance:
`generation_prompt_id` + sha256 recorded on every candidate.

## 12. Fable 5 Governor Contract (v1.1)

Procedure documented in `GOVERNOR.md`; the governor is an LLM session with a fixed input/output
contract, not a code path.

Reads (train+validation derived only): `runs/<run_id>/failure_summary.json`, scoreboard query
results (split ∈ train, validation), family survival rates, invalid-formula patterns,
death-reason distribution, WATCHLIST/PROMOTED lists, current prompt files.

Produces:

1. `governor/next_generation_prompts/<family>__v2.md` — each citing that family's failure
   patterns, repeated mistakes to avoid, preferred hypothesis shape, allowed primitives,
   candidate_count, JSON schema.
2. `governor/research_notes.md` — human-readable failure analysis (which families died from
   NO_SIGNAL vs LATENCY_KILLED, which formulas failed to compile, which primitive combinations
   survived, which prompts produced duplicates).
3. `governor/pending_governor_actions.jsonl` (§16).

Forbidden: editing evaluator/scoring/schema/promotion code or config directly; approving
anything as tradable; deleting/overwriting history; consuming test-split rows (enforced: the
summary artifact it receives is mechanically train+validation only — see §15).

Activation: a human reviews drafts, moves approved prompts to `prompts/v2/` (git commit = the
approval record), and stamps each action line with `approved_by`/`rejected_by`. Approved
schema/evaluator/scoring proposals are implemented by humans as normal PRs with version bumps.

## 13. Status and Death Reasons

Status machine (append-only rows; REJECTED/WATCHLIST/PROMOTED terminal for a given
version-tuple):

```
NEW → INVALID                        (validator)
NEW → COMPILED → EVALUATED → REJECTED | WATCHLIST | PROMOTED   (evaluator + scoring)
```

| death_reason | Assigned by | Trigger |
|---|---|---|
| SCHEMA_INVALID | validator | msgspec decode / field / enum / label-format failure |
| FORMULA_PARSE_ERROR | validator | grammar parse failure in any feature/signal/regime formula |
| PRIMITIVE_INVALID | validator | non-whitelisted call, or `future_mid_return` in a feature/signal/regime AST |
| UNSUPPORTED_NEW_PRIMITIVE | validator | a `proposed_new_primitives` name is actually used in a formula |
| ARGUMENT_INVALID | validator | side/levels/window/horizon domain violation |
| OVER_COMPLEX | validator | inlined signal AST > 64 nodes, or > 6 features, or call depth > 3 |
| DUPLICATE_ALPHA | validator | formula_hash collision within batch or vs prior runs at same primitive_version |
| NO_SIGNAL | evaluator | signal std ≈ 0 on >50% of days, or train \|ic_tstat\| < 1.0 |
| SIGN_UNSTABLE | evaluator | sign_consistency < 0.55, or train/val ICs both above floor with opposite signs |
| COST_KILLED | evaluator | cost_survival_score < 0.3 |
| LATENCY_KILLED | evaluator | 1ms gate fails (sign flip or retention < 0.5) |
| ONE_DAY_ONLY | evaluator | one_day_concentration > 0.6 |
| REGIME_ONLY | evaluator | out-of-regime IC ≈ 0 for a candidate that did not declare that regime |

All thresholds live in `scoring_v1.yaml` / `evaluator_v1.yaml` (versioned);
first-failure-wins for the primary death_reason, full gate trace kept in `gates_failed`.
Rejected candidates are inputs to the next generation, not waste.

WATCHLIST = weak but potentially informative; retest on new data/evaluator versions.
PROMOTED = passed hard gates + top 1% → stricter retest queue. Neither implies tradable.

## 14. Promotion / Rejection Rules

Hybrid rule, in order:

1. Hard gates (each failure → REJECTED with mapped death_reason): schema valid; compiled; no
   primitive violation; NO_SIGNAL floor passed; validation does not severely contradict train
   direction; cost proxy survives (≥0.3); 1ms latency survives; not single-day.
2. Rank survivors by `final_score` (validation split).
3. PROMOTED = top `ceil(1% × survivors)` (120 → ~1–2; 600 → ~6) → enters stricter retest queue
   (future work; out of v1 scope). PROMOTED ≠ tradable/profitable/strategy-ready.
4. WATCHLIST = next decile of survivors, or gate-survivors below the promotion cut with
   |validation IC tstat| ≥ 1.5.
5. Everything else → REJECTED.
6. Test-split pass runs only for WATCHLIST+PROMOTED, after statuses are assigned; recorded,
   never fed back.

## 15. failure_summary.json Schema

Produced by `failure_summary.py`; the CH query is hard-coded to
`split IN ('train','validation')` with no parameter to widen it, and a regression test asserts
no test-split leakage even when test rows exist.

```json
{
  "run_id": "smoke_001",
  "generated_at": "...",
  "versions": {"data_version": "...", "primitive_version": "prim_v1",
               "evaluator_version": "eval_v1", "scoring_version": "score_v1",
               "cost_assumption_version": "taifex_v1", "latency_config_version": "lat_shift_v1"},
  "splits_included": ["train", "validation"],
  "totals": {"candidates": 120, "invalid": 0, "compiled": 0, "evaluated": 0,
             "rejected": 0, "watchlist": 0, "promoted": 0},
  "per_family": {
    "<family>": {
      "candidates": 20, "survival_rate": 0.0,
      "status_funnel": {"NEW": 20, "INVALID": 0, "COMPILED": 0, "EVALUATED": 0,
                        "REJECTED": 0, "WATCHLIST": 0, "PROMOTED": 0},
      "death_reason_distribution": {"NO_SIGNAL": 0, "...": 0},
      "invalid_formula_rate": 0.0, "duplicate_rate": 0.0,
      "latency_failure_rate": 0.0, "cost_failure_rate": 0.0,
      "reduced_day_coverage_count": 0,
      "ic_distribution_survivors": {"p10": 0.0, "p50": 0.0, "p90": 0.0},
      "common_failure_patterns": ["<top recurring parse/arg error strings>"],
      "near_misses": [{"alpha_id": "...", "failed_gate": "...", "margin": 0.0}]
    }
  },
  "watchlist": [{"alpha_id": "...", "family": "...", "final_score": 0.0}],
  "promoted":  [{"alpha_id": "...", "family": "...", "final_score": 0.0}],
  "proposed_new_primitives_tally": {"cancel_intensity": 7},
  "prompt_ids_used": {"<family>": "<family>__v1"}
}
```

## 16. pending_governor_actions Schema

`governor/pending_governor_actions.jsonl`, one action per line:

```json
{
  "action_id": "ga_<run_id>_<seq>",
  "action_type": "propose_new_primitive | propose_schema_change | propose_scoring_change | propose_new_death_reason | propose_new_family | propose_evaluator_diagnostic",
  "proposal": "<concrete description / draft signature / draft field>",
  "reason": "<why>",
  "evidence_from_failure_summary": ["<json-pointer or quoted stat from failure_summary.json>"],
  "risk": "<what could go wrong if adopted>",
  "requires_human_approval": true,
  "status": "PENDING",
  "source_run_id": "smoke_001",
  "proposed_at": "...",
  "approved_by": null, "rejected_by": null, "resolution_note": null
}
```

Append-only; humans edit only `status/approved_by/rejected_by/resolution_note`. Nothing in the
pipeline reads this file to alter behavior — approved actions are implemented as
version-bumped PRs.

## 17. First Implementation Milestone

Smallest buildable v1.0 slice: migrations + `schema.py` + `grammar.py` + `validator.py`
against a hand-written 12-candidate JSONL covering every validator death reason, plus a
single-day panel, evaluator over 5 days, CH/Parquet writes, failure summary. No LLM anywhere.

First 10 engineering tasks (dependency order):

1. Migrations 20260612_001/002 (`research` db + 2 tables); verify via `apply_schema()`.
2. `schema.py` structs + alpha_id hashing + candidate.schema.json generation + tests.
3. `grammar.py` parser (Call/kwargs/strings/division/Compare) + parse/reject test matrix.
4. `validator.py` staged death-reason mapping, complexity limit, formula-hash dedupe + tests.
5. `panels.py` event-clock replay adapted from `snapshot_builder.py`, dir_coverage stamping,
   npz cache + golden-day cross-check vs `batch_alpha_eval.enrich_data` L1 fields.
6. `primitives.py` + `compiler.py` (8 primitives, 4 transforms, window semantics) +
   synthetic-stream tests with known answers.
7. `evaluator.py` wrapping existing IC/metric fns + latency shift + tests on synthetic signals
   with known IC.
8. Cost proxy + `scoring.py` gates/final_score/promotion + tests.
9. `ch_writer.py` (dual sink, idempotent) + `artifacts.py` + `failure_summary.py` +
   test-split-exclusion regression test.
10. `__main__.py` run/--resume + E2E smoke: 12 candidates × 5 days, then 120 × full split.

Runtime expectation (assumption, to be measured): panel build ≤30 min one-time (cached); 600
candidates × ~60 effective days of vectorized ops ≈ tens of minutes single-process, ~10 min
with day-parallel workers. v1.0 well under an hour on one box.

## 18. Risks and Non-Goals

Top 5 design risks:

1. L5 replay fidelity (batch-flush boundaries, one-sided batches) — mitigate by reusing
   `snapshot_builder.py` decoding + golden-day cross-check against `enrich_data`.
2. Trade-direction coverage on the full Feb–Jun span — mitigated by per-day `dir_clean`
   stamping, refusing `trade_imbalance` on dirty days, and surfacing `effective_day_count`
   everywhere; residual risk: trade_flow/replenishment families have ~⅓ fewer usable days and
   asymmetric splits.
3. Prompt/implementation semantic drift — prim_v1 signatures are frozen in this spec before any
   generation prompt is written; prompts must quote exact signatures.
4. Gate calibration killing 100% or 0% — every gate records pass/fail regardless of outcome;
   thresholds only in versioned yaml; budget one scoring_version bump after the first
   120-batch (a calibration change justified from train/validation only — never test).
5. NaN/warmup/end-of-day truncation silently biasing IC — explicit NaN accounting in
   diagnostics.json + minimum valid-row count per day before a day counts toward stability.

Non-goals (v1): strategy generation, fills/queues, paper/live trading, portfolio construction,
dashboards, distributed compute, dynamic primitive registry, executing proposed primitives,
automated governor code, Gate A–F integration.

Future Work (explicitly deferred): signal-correlation duplicate clustering (formula hash only
in v1); LLM API call inside `generate` (v1.1+); TMF as a second instrument; the stricter
retest queue for PROMOTED; promotion handoff into the governed alpha-package workflow; a
canned-query scoreboard doc (and nothing dashboard-shaped beyond it).

First 5 things NOT to build yet:

1. Correlation-based dedupe/clustering.
2. Any governor automation.
3. A dynamic/pluggable primitive registry.
4. Anything strategy-shaped (entries/exits/sizing/fill assumptions).
5. Dashboards/schedulers/distributed runners.
