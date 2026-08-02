# Alpha Mining v3 Wave 2 — scale design

## Packet

Read-only Wave 2 design handoff; do not edit/git/network/access final_holdout. Based on current /tmp/hft-alpha-mining-v3-wave1 plus completed performance findings in conversation, specify the smallest safe performance/governance patch sequence after Wave 1 lands. Prioritize selection context/signal caching, horizon target/detrend caching, quantile hoisting, effective-count vectorization, aggregate worker RSS, and bounded-memory ledger prerequisites. For each: exact functions/files, semantic equivalence invariant, benchmark/test, expected mechanism, dependency order, stop condition. Explicitly decide which changes are required before 20k, 100k, and 1M; do not invent speed multipliers. Include how to benchmark discovery-only without touching locked/final and how cross-leg multiplicity should be deferred. Cite file:line. No implementation. <=130 lines.

## Executor final report

## Wave 2 read-only design

Verdict: do not jump directly from Wave 1 to 100k/1M. Land six independently reversible patches, preserving statistical outputs exactly. Current evidence supports priorities, not a promised multiplier:

- 2k candidates: selection 111.53/212.18s (52.6%).
- 200 candidates: workers=12 slower than 1 due ~8.8s/group startup.
- 2k candidates: workers=12 faster; semantic digest identical.
- Discovery repetition: targets 2.269s, detrend 0.680s versus simulator 0.082s.
- Quantiles: 4,800 calls although only 1,600 unique inputs.
- Effective-count: 1.43s/200 expressions.
- Ledger append: 6.42s/2k; only ~3% now.
- 1M JSONL estimate: ~1.57GB before Python object copies.

### Patch sequence

1. Quantile-resolution hoist — required before 20k

- Surface: `enumerate_candidates`, `research/combinatorial/smma_runner.py:638-691`.
- Compute each `(expression, direction, quantile)` resolution once; emit candidates in the existing expression→horizon→direction→quantile order.
- Preserve horizon-local `canonical_by_cut`, candidate IDs, thresholds, `ThresholdResolution`, `duplicate_of`, and list order exactly.
- Test: spy on `resolve_quantile_threshold`; expect 1/3 current calls and byte-equivalent serialized candidates.
- Benchmark: existing group fixture, 3 repetitions, report calls and wall time.
- Mechanism: removes three identical `np.quantile`/`np.unique` passes.
- Stop: any semantic candidate digest differs.

2. Selection group/signal cache — required before 20k

- Surface: `_selection`, `research/combinatorial/smma_runner.py:2526-2611`; feature adapters at `:153-239`.
- Add one `SelectionGroupContext` per `(root,timeframe)` containing bars, plan, selection mask/timestamps, one feature build, and lazy signal memo keyed by expression.
- Do not retain or reuse discovery signals across the stage boundary.
- Preserve `_result_sort_key` order, correlation-disposition order, root caps, ledger rows, unlock order, and checkpoint contents.
- Test: adapter spy expects one feature build/group and one expression evaluation/unique expression; compare selection semantic digest after removing timestamps/hash-chain fields.
- Benchmark: replay the measured 94-candidate/46-expression selection set only.
- Mechanism: eliminates full family-feature rebuild and expression reevaluation for each horizon/direction/cut.
- Stop: any disposition, metric, selected ID/order, or unlock differs.

3. Horizon target/detrend context — required before 20k

- Surface: `_exact_horizon_inputs` and `_evaluate_candidate`, `smma_runner.py:790-953`; `evaluate_recent_kill_criteria`, `smma_validation.py:458-515`.
- Add immutable context keyed by `(root,timeframe,horizon,split,evaluation_fraction)` with execution grid, labels, split/recent indices, target indices, returns, and trailing-detrended recent target.
- For 120/240m→60m, cache only the alignment map/grid; project each expression signal through that exact map.
- Install contexts once in the parent/single-worker path and once per process through `_initialize_discovery_worker`, `smma_runner.py:968-994`.
- Simulation, activation, execution prices/costs, and signal ranking remain candidate-specific.
- Tests: all three horizons, reset/session boundaries, 120/240→60 projection, both directions; exact `KillMetrics` equality with uncached reference.
- Benchmark: 24 candidates over the same 16,586 discovery bars, separately timing target, detrend, projection, simulator.
- Mechanism: replaces identical per-candidate target scans and detrends with per-horizon work.
- Stop: target arrays or metrics differ, including NaN placement.

4. Aggregate worker RSS governance — required before any 20k multiworker run

- Surface: `_current_rss_bytes`, heartbeat, stop loop, `smma_runner.py:1149-1158,1552-1569,1617-1641`; process pool at `:1012-1033`.
- Replace parent-only measurement with a recursive `/proc` process-tree snapshot: parent RSS, child RSS by PID, aggregate RSS, completeness flag.
- Keep the existing 18/20GiB thresholds unchanged; compare them to conservative aggregate RSS and record all components in heartbeat/resource evidence.
- Treat disappearing children as normal races; on Linux, inability to measure the parent/process tree is fail-closed for multiworker scale runs.
- Tests: synthetic proc tree, nested child, PID disappearance, malformed status, no double-count/cycle.
- Benchmark: workers 1/12, record peak parent/children/aggregate RSS alongside semantic digest.
- Mechanism: makes the resource guard observe the memory it governs.
- Stop: aggregate measurement unavailable, threshold crossed, or pause cannot make progress; do not loosen thresholds.

5. Chunked/vectorized effective-count estimator — required before 100k

- Surface: `_effective_trigger_test_count`, `smma_runner.py:694-723`; `effective_test_count`, `smma_validation.py:813-845`.
- Precompute ordered discovery-day codes/counts once; aggregate activation profiles in fixed-size candidate chunks.
- Standardize profile rows exactly as today, then incrementally accumulate the float64 observation-side Gram matrix; use `eigvalsh` on that small matrix instead of materializing/SVD-ing all test rows.
- Preserve filters: horizon=`1h`, nonduplicates only, ordered days, horizon multiplier, caps, and Li–Ji rounding.
- Freeze chunk size and estimator version in code/search-space evidence.
- Tests: perfect correlation, orthogonal, constants, NaNs, random seeded matrices; new count must equal current estimator for every fixture and all six saved discovery datasets.
- Benchmark: 200/2k/20k-expression synthetic profiles; report wall time and peak RSS.
- Mechanism: vectorized day aggregation and memory proportional to days² rather than tests×days.
- Stop: any effective count changes or result depends on chunk size.

6. Bounded ledger/finalization — Phase A before 100k, Phase B before 1M

- Current blockers: all rows retained/copied in `HashChainLedger`, `smma_runner.py:511-582`; per-row open/fsync at `:548-568`; full discovery materialization at `:2199-2242`.
- Phase A: add streaming `iter_rows(stage)`, cached counters, incremental funnel/histograms, bounded per-root top-K, and persistent append handle while retaining flush+fsync durability.
- Replace repeated `rows()` tuple copies/scans and reconstruct checkpoint discovery passes from the authoritative ledger.
- Preserve row order, row hashes, fsync-per-row durability, resume results, and every conservation check.
- Phase B: replace Python row/stage sets with a rebuildable SQLite offset index keyed by `(candidate_id,stage)`; JSONL remains sole authority.
- JSONL must be fsynced before index update; startup validates the chain and replays any unindexed tail.
- Stream `_finalize_discovery`; never build `discovery_results` for 1M rows.
- Tests: kill between JSONL append/index update, truncated/corrupt tail, resume, duplicate append, exact old/new chain and report equivalence.
- Benchmark: 20k/100k/1M synthetic rows; report append latency, resume latency, raw disk, and peak RSS.
- Stop: memory grows with retained row payloads, recovery changes a row, or durability weakens.

7. Signal batching prerequisite — required for 100k 2m and all 1M runs

- `_build_group_context` currently retains every expression signal and sends the mapping to every worker, `smma_runner.py:1643-1723,1012-1033`.
- Introduce fixed-byte expression batches and a read-only shared/memory-mapped signal slab; keep expression/candidate/ledger order unchanged.
- Feed effective-count profiles incrementally before discarding each batch.
- Stop 100k 2m/1M if aggregate RSS shows replicated worker mappings or if peak memory is not bounded by configured batch bytes.

### Scale gates

- 20k/leg: patches 1–4 required; current ledger remains acceptable only if measured RSS/output stay below unchanged limits.
- 100k/leg: patches 1–5, ledger Phase A, and signal batching required for 2m; run discovery-only first.
- 1M/leg: all patches, ledger Phase B, bounded signal slabs, successful resume/crash probe, and aggregate-RSS evidence required.
- No locked/final 1M campaign until the separate cross-leg multiplicity policy is frozen.

### Discovery-only benchmark protocol

- Derive discovery-day assignments from the immutable partition manifest, then materialize a discovery-only governed-bars copy before feature/target computation.
- Invoke only context generation and discovery evaluation; never call `MiningRun.run`, `_selection`, `_locked`, `_final_holdout`, or robustness slices.
- Use a temporary run directory; assert `split_access.jsonl` is absent or contains zero access rows.
- Compare semantic digests after removing timing/resource/hash-chain timestamp fields.
- Run workers=1 and 12 at 200, 2k, then 20k; median of at least three warm repetitions plus peak aggregate RSS.
- Do not auto-select workers from the present two-point crossover; first obtain a bounded crossover profile.

Cross-leg multiplicity is intentionally deferred from Wave 2 because aligning correlated activation profiles across families/timeframes changes research semantics, not performance. Wave 3 must preregister the union estimator/campaign-level BH using discovery evidence only, then freeze it before campaign locked validation. Per-leg performance work must not silently alter current floors or interpret campaign-wide alpha evidence.

No files were edited.

## Review verdict

APPROVE the staged Wave 2 design; block 100k/1M escalation until the stated memory, integrity, and multiplicity prerequisites are verified.
