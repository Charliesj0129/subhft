# Alpha Candidate Loop v1.0 + Maker-Aware Cost Gate — Implementation Design

Date: 2026-06-12
Status: implemented (branch `research-flow/edge-evidence-parity-hardening`)
Spec: `docs/research/alpha_candidate_loop_v1_spec.md` (approved 2026-06-11, frozen contract)
Plan: `~/.claude/plans/repo-brief-majestic-quasar.md`

## What was built

The approved batch candidate loop, end-to-end, plus exactly ONE extension:
a maker-aware cost gate (`taifex_maker_qhat_v1`) built on the calibrated
`QHatTable` (`research/backtest/q_hat_data/txfd6_q_hat.parquet`). Everything
else in the spec is implemented as frozen: `cand_v1` / `prim_v1` / `eval_v1` /
`lat_shift_v1` / `taifex_v1` / `score_v1` / `split_v1` / `panel_v1` /
`txf_l2_2026H1_v1`.

## Module map (`research/candidate_loop/`)

| Module | Responsibility |
| --- | --- |
| `schema.py` | msgspec candidate contract, Status/DeathReason enums, `alpha_id = sha256(canonical_json)[:16]`, JSON-Schema export for prompts |
| `grammar.py` | tokenizer + recursive-descent parser (no `eval`) |
| `validator.py` | staged validation, first-failure-wins death reasons, formula_hash dedupe (within batch + vs prior runs), complexity caps (64 nodes / 6 features / depth 3) |
| `panels.py` | per-day event-clock panel from L2 NPZ (batch-flush replay, canonical event bits DEPTH=1/TRADE=2), cached by data_fingerprint+panel_version; `dir_coverage` from CH `trade_direction`, fail-closed to 0.0 |
| `primitives.py` / `compiler.py` | 8 prim_v1 primitives + 4 transforms, trailing-inclusive windows (event index offsets / `searchsorted(local_ts)`), safe divide |
| `evaluator.py` | per-day ICs (Pearson + NaN-aware Spearman), latency re-anchor `label0[asof(t+δ)]`, horizon decay, 5-bucket spreads, taifex_v1 hysteresis flip cost proxy; `aggregate_split` mirrors `experiment_results` columns |
| `maker_cost.py` | the extension (see below); pure functions, no I/O |
| `scoring.py` | hard gates in frozen order, `final_score` (score_v1), §14 promotion (top-ceil(1%) / watchlist decile / tstat rescue) |
| `splits.py` | explicit (day, symbol) split pairs, NPZ inventory pathing |
| `ch_writer.py` | dual-sink writer (CH primary, `runs/<run_id>/_results_fallback.jsonl` fallback, `replay-fallback` CLI); dedupe keys `(alpha_id, run_id, status)` and `result_id`; Date column coercion for the jsonl replay path |
| `artifacts.py` | hive Parquet tree under `artifacts/` (gitignored), per (data_version, evaluator_version, alpha_id, split) |
| `failure_summary.py` | §15 summary; `SPLITS_INCLUDED = (train, validation)` frozen, CH fetch SQL hard-codes the split filter |
| `runner.py` | §5 orchestration: ingest → validate → outer-days/inner-candidates evaluation (one panel in memory) → reduce → gate → statuses → test pass (WATCHLIST+PROMOTED only) → failure summary |
| `generate.py` + `__main__.py` | §11 provenance header (`prompt_id`, `prompt_sha256`, model, run id) + CLI: `generate` / `run` / `summarize` / `promote` / `replay-fallback` |
| `prompts/v1/` | 6 family prompts + `candidate.schema.json`, RENDERED from `tools/render_prompts.py` (single source of truth = `schema.py` signatures; freshness-tested byte-for-byte) |
| `fixtures/validator_matrix_12.jsonl` | §17 fixture: 5 valid + 7 invalid, one per validator death reason |
| `tools/make_smoke_batch.py` | deterministic 6×20 grid expander (`generation_model="template_v1"`), pre-validates all 120 and asserts hash-disjointness from the fixture |

Config: `config/research/candidate_loop/{split_definition_v1,evaluator_v1,scoring_v1}.yaml`.
Migrations: `src/hft_platform/migrations/clickhouse/20260612_001/002_*.sql`
(applied via `recorder/schema.py::apply_schema`).

## Maker gate (taifex_maker_qhat_v1) — the one extension

```
p_fill_i = QHatTable.lookup(q_hat_symbol, hour_utc(flip_ts_i), near_side_L1_qty_i)
maker_required_move_pts = 2*(comm+tax pts/side) + (1 − mean_p_fill) * median_spread_pts
maker_cost_survival_score = gross_pts_per_flip / maker_required_move_pts
```

Invariants (unit-tested):

- hour key is UTC epoch-modulo, identical to `calibrate_queue_fill._hour_of_day`
  — converting to Taipei hours would silently swap day/night liquidity regimes;
- `maker_required ≤ taker_required` (equal at p_fill=0) and `≥ 2*(comm+tax)`
  (zero-spread bound) → at the same 0.3 threshold the maker gate can never
  kill a candidate the taker gate passed; its value is the trace:
  `gates_failed` distinguishes `cost_proxy_taker` vs `cost_proxy_maker`,
  failure of both = dead under any execution, taker-only = maker-rescuable;
- missing q_hat cells fall back to 0.5 (calibration drops n<30 cells); a
  missing table degrades to an empty `QHatTable()` with a warning;
- 4 additive columns on `experiment_results` (`maker_fill_prob_mean`,
  `maker_required_move_threshold_pts`, `maker_cost_survival_score`,
  `maker_cost_assumption_version`); frozen taker fields and `score_v1`
  composition byte-identical to the spec; failures map to existing
  `COST_KILLED` (no new death reasons).

failure_summary additions: per-family `maker_cost_failure_rate` and
`maker_rescuable_count`.

## Key decisions and their reasons

1. **Prompts are rendered, not hand-written.** They are functional inputs
   (frontmatter `prompt_id` + `prompt_sha256` go into provenance), so
   `tools/render_prompts.py` derives them from `PRIMITIVE_SIGNATURES` and
   the window/horizon domain constants; `test_prompts.py` fails if a
   committed prompt or `candidate.schema.json` drifts from a fresh render.
2. **Idempotency is structural, not procedural.** Candidate rows dedupe on
   `(alpha_id, run_id, status)`, results on `result_id`, panels on
   `data_fingerprint+panel_version`, and the cross-run `formula_hash` pool
   EXCLUDES the current `run_id` (otherwise a re-run would mark every
   candidate `DUPLICATE_ALPHA` against its own first pass). `--resume` is
   therefore literally `run`.
3. **Fail-closed dir_coverage.** Without ClickHouse (or with no trade rows),
   `dir_coverage = 0.0` and every `trade_imbalance` candidate loses the day
   (`dir_dirty` skip, visible in `effective_day_count`). NPZ tick-rule trade
   sides are never used as the dir_clean basis.
4. **Test-split isolation is mechanical.** `SPLITS_INCLUDED` is a frozen
   module constant, the fetch SQL hard-codes `split IN ('train','validation')`
   with no widening parameter, and `build_failure_summary` drops test rows
   unconditionally; the runner additionally never feeds test rows into the
   summary (belt + braces, both regression-tested).
5. **Ranking-only rejections carry empty death_reason.** §14's "everything
   else → REJECTED" includes gate-survivors below the promotion cut; the
   frozen death-reason taxonomy has no category for them, so they keep `''`
   while every gate-failed rejection carries its first failing gate's reason.
6. **gitignore whitelist.** `research/*` ignores everything not re-included;
   `!research/candidate_loop/` was added with `runs/`, `candidates/`,
   `artifacts/` re-ignored beneath it.

## Incidental platform fix

`recorder/schema.py::_extract_up_statements` split migration files on `;`
inside `--` comments (pre-existing; 20260504_001's header comment broke
`apply_schema` and blocked all later migrations). Statements now terminate
only on `;` outside comments (commit e008b7e7, regression-tested).

## Verification

- `uv run pytest tests/unit/research/candidate_loop -q` — 319 tests pass;
  package coverage 95%, every module ≥86%.
- `uv run mypy research/candidate_loop` / `ruff check` — clean.
- §17 E2E in-tree: 12-candidate fixture × synthetic 5-day TXFD6 inventory,
  no CH (jsonl fallback), asserts terminal statuses, maker columns, missing
  NPZ handling, test-split isolation, idempotent re-run (zero new rows).
- smoke_001: 120 template candidates over the real `split_v1` inventory
  (40 train / 14 validation days; test split only for survivors) with live
  ClickHouse. Results: 75 REJECTED (70 `COST_KILLED`, taker or maker gate;
  5 ranking-only survivors below the promotion cut, empty `death_reason` per
  decision 5), 44 WATCHLIST, 1 PROMOTED (`order_book_imbalance`,
  alpha_id `ca664cd69fc36d02`, `final_score=2.240`). `research.experiment_results`
  holds 285 rows (120 train + 120 validation + 45 test for the 45
  WATCHLIST+PROMOTED survivors), all four maker columns non-null with
  `maker_cost_assumption_version='taifex_maker_qhat_v1'`. `trade_flow` produced
  2 maker-rescuable near-misses (taker-gate margins -0.0137 / -0.0139, both
  pass the maker gate). Re-running `run --batch smoke_001` reproduced the
  identical totals and left both tables unchanged (480 `alpha_candidates`
  rows, 285 `experiment_results` rows) — idempotency confirmed. Full detail
  in `research/candidate_loop/runs/smoke_001/failure_summary.json` and the
  session memory entry.

## Out of scope (unchanged)

v1.1 governor automation, v1.2 600-candidate scale-up, queue simulation /
market impact, any change to the >10pt/trade floor, anything touching the
FROZEN live registry (loop_v1 L11, `r47_tmf_v1`).
