# Alpha Research Handbook

**Status:** the single handbook for alpha research: lifecycle, verdicts, gates, the
governed pipeline, evidence and factory operations. Rebuilt in research v2 C6
(ADR-005); it replaces `research/SOP.md`, `docs/runbooks/alpha-factory.md` and the
operational half of `research/README.md`.
**Audience:** researchers proposing alphas, agents running research, reviewers of
promotion candidates.

> **The live registry is frozen.** Under `loop_v1` step L11 the live registry is
> locked to `r47_tmf_v1`. A new alpha may finish its gates, canary and shadow, but it
> cannot replace the active loop. See `docs/loop_v1_stabilization_charter.md` and
> `.github/workflows/freeze-guard.yml`.

The gate tables in sections 6 and 7 are copied from
`config/research/profiles/vm_ul6_strict_v2.yaml` and the sub-gate registry;
`tests/unit/alpha/test_research_handbook.py` fails when they drift apart.

---

## 1. Lifecycle

```
 generators: paper | mining | candidate_loop | manual    (rank, never KILL)
      |
      v
 Gate A (manifest, data) -> Gate B (alpha tests) -> Gate C (backtest sub-gates)
      |                                                  |
      |                         derive_verdict -> PASS | KILL | NEEDS-MORE-DAYS
      |                                           | INCONCLUSIVE | INVALID
      v
 Gate D (promotion eligibility) -> Gate E (paper trade, shadow sessions)
      |
      v
 Gate F: NOT_APPLICABLE while Rust is paused (profile not_applicable_gates)
      |
      v
 canary -> shadow (replay-parity obligation, ADR-003) -> live registry (FROZEN)
```

There is no Rust stage. Rust is paused (2026-10-10): no alpha is ported, and
`vm_ul6_strict_v2` reports Gate F as NOT_APPLICABLE with reason `rust_paused`.
Performance work happens in Python/numba.

Only a PASS proceeds. Generators shortlist; only the judge
(`src/hft_platform/alpha/outcomes.py:derive_verdict`) writes a verdict, and a KILL
is written only through `KillRecord.from_verdict` (`src/hft_platform/alpha/kill_ledger.py`).

---

## 2. Verdict vocabulary

Each word means one thing and is written by one place.

| Word | Kind | Written by | Meaning |
|---|---|---|---|
| `pass` | gate outcome | each sub-gate / check | measured and met its threshold |
| `fail` | gate outcome | each sub-gate / check | measured and missed its threshold |
| `insufficient_sample` | gate outcome | each sub-gate / check | too little data to measure |
| `not_applicable` | gate outcome | each sub-gate / check | does not apply at this stage or to this strategy type |
| `error` | gate outcome | each sub-gate / check | input missing or unusable; never a PASS |
| `PASS` | run verdict | `derive_verdict` | every blocking check passed (or was deferred by the profile) |
| `KILL` | run verdict | `derive_verdict` | a blocking FAIL that is powered |
| `NEEDS-MORE-DAYS` | run verdict | `derive_verdict` | a blocking `insufficient_sample` with a reachable required n |
| `INCONCLUSIVE` | run verdict | `derive_verdict` | an underpowered FAIL, or a sample more days cannot fix |
| `INVALID` | run verdict | `derive_verdict` | a blocking `error`, or a `not_applicable` the profile does not defer; rerun, no verdict |
| `DRAFT` ... `GATE_E` | alpha status | `src/hft_platform/alpha/status.py` | highest gate whose latest ledger event passed |
| `KILLED` | alpha status | `status.py` | any KILL in the ledger; a retest is a new candidate |
| `PRODUCTION`, `DEPRECATED` | alpha status | manifest, by a manual config PR | never derived from the ledger |
| `pass` / `kill` / `unknown` | cheap-screen result | `src/hft_platform/alpha/screener.py` | pre-Gate-A triage; `kill` is reported as `SCREENED_OUT` |
| `SCREENED_IN` / `SCREENED_OUT` | screen outcome | `hft alpha cheap-screen` | triage only; never a KILL, never in the kill ledger |
| `screened_out` | mining status | `research/combinatorial/smma_runner.py` | mined expression dropped by its screen |
| `NEW` ... `SHORTLISTED` | candidate status | `research/candidate_loop/schema.py` | the loop's own ranking (`REJECTED`, `WATCHLIST`, `SHORTLISTED`); `PROMOTED` in old rows reads as `SHORTLISTED` |
| `governed` / `exploratory` | run mode | `src/hft_platform/alpha/evidence.py` | only governed runs reach the ledger and count as evidence |

Verdict rules v1 (`VERDICT_RULES_VERSION = "v1"`), over blocking checks only:

```
any ERROR, or NOT_APPLICABLE the profile does not defer -> INVALID
FAIL, powered                                          -> KILL
INSUFFICIENT_SAMPLE that more days cannot fix          -> INCONCLUSIVE
INSUFFICIENT_SAMPLE with a reachable required n        -> NEEDS-MORE-DAYS
FAIL underpowered, or INSUFFICIENT_SAMPLE without n    -> INCONCLUSIVE
otherwise                                              -> PASS
```

A FAIL is powered when the check says so; otherwise it counts as powered only when
`min_sample_size` passed. Gate D runs only after a Gate C PASS, so its FAILs are
powered. Never relax a pre-registered floor or gate to change a verdict.

---

## 3. Pre-flight constraints

| Rule | Source |
|---|---|
| Laws 1-5 and the Precision Law for anything that crosses into risk/order/execution | `AGENTS.md` |
| HFT-P004: no `: float` money fields in contracts/order/execution/risk | `scripts/check_discipline.py`, `make discipline-hft` |
| Latency realism: P95 for promotion, P99 for stress; never assume sub-broker-RTT | `docs/architecture/latency-baseline-shioaji-sim-vs-system.md` |
| Name the backtest method behind every PnL claim | `docs/runbooks/backtest-engine-selection.md` |
| TAIFEX costs, sessions, price scales | `.agent/skills/taifex-market-structure/SKILL.md` |
| Kill criteria before starting a direction | `.agent/skills/taifex-alpha-kill-criteria/SKILL.md` |
| Research data, scales (ClickHouse x1,000,000 vs platform x10,000), book depth 0-5 | `.agent/rules/70-research-data.md` |

---

## 4. Generators and authoring

### Paper intake and scaffold

```bash
make research-search-papers QUERY="order flow imbalance"
make research-fetch-paper ARXIV=2408.03594
make research-paper-prototype PAPER_REF=120 ARGS='--alpha-id ofi_p120 --complexity O1'
hft alpha scaffold <alpha_id>        # or: make research-scaffold ALPHA=<id>
```

`research-paper-prototype` scaffolds `research/alphas/<alpha_id>/` and writes the
reverse mapping into `research/knowledge/paper_index.json` (`alphas` field). Strict
Gate A requires `manifest.paper_refs` and that linkage. A scaffolded alpha has no
live registry entry: loop binding (`config/loops/<loop_id>.yaml`) is the only source
of truth for live strategies.

### Generators (research v2 C4)

Mining (`research/combinatorial`), the candidate loop (`research/candidate_loop`) and
queue calibration (`research/calibration`) produce candidates or calibration inputs;
none of them promotes or writes a KILL.

- The candidate loop's top status is `SHORTLISTED`. Every test-split read is logged
  in the evidence root's `ledger/holdout_access.jsonl`, the same log Gate C writes.
- A shortlisted candidate reaches Gate C as
  `research.candidate_loop.gate_c_alpha.CandidateGateCAlpha`: its validator-canonical
  signal is evaluated once on the day panels (`panels.build_panel`) of the same
  `*_l2.hftbt.npz` files Gate C replays, and each tick reads the latest batch at or
  before the tick's `local_ts`. No CLI wires it to Gate C yet.
- A calibration profile in `config/research/calibration_profiles.yaml` carries a
  `provenance` block: the RunStamp, the sweep configuration, the train and held-out
  days, and how many candidates the sweep scored.
- `search_engine`'s score is a screening scale, not a Sharpe. `hft alpha mine promote`
  records the search size as `experiment_metadata.search_trials` and its Li-Ji
  effective count as `search_trials_effective`; Gate C's advisory
  `selection_deflated_sharpe` deflates by that count.

### Screens before Gate A

| Command | Stage | Result |
|---|---|---|
| `hft alpha cheap-screen <alpha_id> [--threshold-ic 0.005] [--threshold-turnover 2.0]` | pre-Gate-A IC, turnover and cost-floor triage, 60 s budget | `SCREENED_IN` / `SCREENED_OUT`; never a kill (`--write-kill` is a no-op) |
| `hft alpha screen --alpha-id <id> --data <paths>` | Gates A-C without a strict profile | scorecard stamped `screen_only=true`; `hft alpha promote` refuses it |
| `hft alpha validate --alpha-id <id> --profile vm_ul6_strict_v2 --data <paths>` | strict Gates A-C | promotion-eligible; refuses non-strict profiles |

### Data governance (strict Gate A)

```bash
make research-stamp-data-meta DATA_PATH=research/data/raw/example.npy ARGS='--source-type real --owner <owner> --symbols 2330'
make research-validate-data-meta DATA_PATH=research/data/raw/example.npy
make research-gen-synth-lob OUT=research/data/processed/queue_imbalance/synthetic_qi_v2_train.npy \
  ARGS='--version v2 --rng-seed 42 --symbols TXF,MXF --split train'
```

Strict Gate A requires the dataset under an allowed root, a metadata sidecar
(`.meta.json` / `.metadata.json`), the contract keys and a consistent row count;
`vm_ul6_strict_v2` also requires the provenance fields listed in section 7. The
governed L2+tick export contract is `research.data_pipeline`
(`make research-export-l2-ticks`, `make research-validate-l2-ticks`).

---

## 5. The governed pipeline

### One entrance

```bash
make research ALPHA=<alpha_id> OWNER=<owner> DATA='<path1.npy [path2.npy ...]>' [ARGS='...']
```

This runs strict `research.pipeline run` with `vm_ul6_strict_v2` by default (a strict
run refuses `standard`, which stays exploratory):

1. `factory audit` preflight (read-only; warnings fail a strict run)
2. Gate A/B/C validation
3. Gate D/E promotion
4. `factory index` finalization

`hft alpha {scaffold,search,mine,screen,validate,validate-batch,pipeline,batch-correlation,promote-batch}`
parse their arguments in the platform CLI and run as `python -m research alpha <same
arguments>` (`src/hft_platform/cli/_research.py` forwards; the bodies are in
`research/registry/alpha_cli.py`). Gate C runs only with the runtime injected from
`research/backtest/gate_c_runtime.py:build_gate_c_runtime`. Where `research/` is
absent (production images) these subcommands exit 2 with a message.

### Evidence root (ADR-005)

A governed run stops with rc 2 unless `HFT_RESEARCH_EVIDENCE_ROOT` is an absolute
path outside the repository, the tree is clean and the profile resolves. Runs and
validation artifacts go to that root (`runs/<run_id>/`,
`validations/<alpha_id>/<stamp>/`), each sealed by a `manifest.json` of sha256s, and
every gate that ran appends one event to `ledger/verdicts.jsonl`. Governed promotion
reads only a sealed scorecard under that root, writes its Gate D-F reports to
`promotions/` there and appends their events; the run must start and publish on the
same commit. Sealed directories are never rewritten. Triage runs stay exploratory:
labelled in the RunStamp, written to `--experiments-dir`, never in the ledger.

```bash
python -m research evidence init            # layout + local git init (no remote)
python -m research evidence import-kills    # archive pre-ledger kills; --dry-run, --from jsonl
python -m research evidence audit-stale     # mark runs a registered defect affects; --dry-run, --json
python -m research status [--alpha ID] [--json]
```

- **Status.** No run rewrites `impl.py`. `hft alpha list`, `factory index` and
  `python -m research status` show the status derived from the ledger (section 2). A
  malformed ledger stops them (rc 2).
- **Legacy evidence.** `research/experiments/**` is frozen and read only through
  `research/registry/legacy_evidence.py`, which quarantines rows tests wrote into the
  real sinks. `import-kills` copies `_kill_ledger.jsonl` and ClickHouse
  `audit.alpha_kill_ledger` into `legacy/kills.jsonl`; those kills never derive a
  status, because they predate verdict rules v1.
- **Stale results.** `config/research/defects.yaml` names each defect by its fix
  commit and components. `audit-stale` appends one mark per (run, defect) to
  `ledger/stale.jsonl`; marks flag evidence, never change a status, never edit it.
- **Protocols.** Pre-registered protocols (`config/research/protocols/*.yaml`) are
  frozen once a run has used them: add a `_v2`, never edit one; runs record their
  sha256.

### Backtest engines (Gate C)

- `strategy_type: taker` uses `research.backtest.taker_sim.TakerSim`: the
  `HftNativeRunner` signal ladder sent as IOC orders at the touch, per-contract costs
  from `config/research/cost_profiles.yaml`, one row per trading day, Sharpe on the
  daily series. Gate C's `sharpe_oos` comes from it; `HftNativeRunner`'s mid-to-mid
  Sharpe is screening only (`gate_c.screening_sharpe_oos`).
- `strategy_type: maker` uses `MakerEngine` (ClickHouse-direct,
  `QueueDepletionFill(qf=0.5)`; needs local ClickHouse: `docker compose up -d clickhouse`).
- ClickHouse is read only through `research.data_pipeline.reader.GovernedReader`:
  the session rule (TAIFEX windows plus the XTAI calendar), dedup
  `content_multiplicity_v1`, ordering by `exch_ts, ingest_ts, seq_no`. A date is a
  trading day `[previous trading day 14:00, D 14:00)` Taipei. Prices stay at x1,000,000.
- A maker `instrument: <ROOT>_frontmonth` runs on one series rolled across contract
  months (ADR-004) under `config/research/protocols/front_month_roll_v1.yaml`.
- Engine bias and calibration: `docs/runbooks/backtest-engine-selection.md`.
- `research/tools/legacy/` is archived; never use it for official results.

### Statistics (one module)

Every multiple-testing correction, resampler, Sharpe deflation and IC estimate is
defined once, in `src/hft_platform/alpha/stats.py`. Deflated Sharpe is the
Bailey/López de Prado probability against `SR0`; gates with Sharpe-unit thresholds
(`deflated_sharpe_min`, `opt_min_deflated_sharpe`) compare `SR - SR0` against the
unchanged threshold. Every Gate C read of an out-of-sample holdout is recorded in
`ledger/holdout_access.jsonl`; a second read by the same alpha is flagged, never gated.

Definitions retired by the move:

| Retired | Difference | Replacement |
|---|---|---|
| `_param_opt`, `deflated_sharpe_maker` and `research/tools/bayesian_opt.py` haircut `sqrt(2 ln N / T)` | annualized Sharpe less a per-observation penalty: about 0 | `stats.selection_sr0`; still 0 at one trial |
| `_stat_tests._bh_correction`, `smma_validation.benjamini_hochberg` | identical values | `stats.benjamini_hochberg` (adds `n_tests`) |
| `_resampling` and `smma_validation` bootstraps, `deflated_sharpe`, `effective_test_count` | identical values | `stats.*` (`block_bootstrap_mean` is now `moving_block_bootstrap_mean`) |
| `_safe_spearmanr`, `smma_validation.spearman_ic`, `candidate_loop` `_rank_ic`, `screener._ic` | identical except on near-constant input (range at most 1e-12 is now undefined) | `stats.rank_ic` / `stats.pearson_ic` |
| `taker_sim.daily_sharpe`, `metrics.compute_sharpe`, `search_engine._signal_sharpe` | identical except a constant series is now 0, not ~1e16 | `stats.sharpe_ratio` |
| `metrics.compute_ic` / `compute_ic_incremental`, `metrics.compute_ic_ttest` | identical values | `stats.chunked_ic`, `stats.chunk_pearson_ic`, `stats.ic_ttest` |

---

## 6. Gates A, B and C

- **Gate A** (`src/hft_platform/alpha/_gate_a.py`): manifest schema, `data_fields`,
  `paper_refs` and paper-index linkage, data governance (section 4).
- **Gate B** (`src/hft_platform/alpha/_gate_b.py`): the alpha's own tests under
  `research/alphas/<id>/tests/`. A failure stops Gate C.
- **Gate C** (`src/hft_platform/alpha/_gate_c.py`, sub-gates in
  `src/hft_platform/alpha/_sub_gates/`): the backtest scorecard and the sub-gates
  below. Profile thresholds win over run defaults; for keys with a known direction the
  stricter value is kept, so no loose default can relax a gate.

Each Gate C run is written once by `ExperimentTracker.log_run` to
`<experiments_dir>/runs/<run_id>/` (`meta.json` with the RunStamp,
`backtest_report.json`, `scorecard.json`).

### Sub-gates

`vm_ul6_strict_v2` keeps `vm_ul6_strict`'s thresholds and `blocking_sub_gates`
unchanged (`tests/unit/alpha/test_research_v2_profile.py`) and only repairs
references: the measured Shioaji P95 latency id, no maker rebate, Gate F not
applicable while Rust is paused, and `replay_parity` deferred to Shadow exit.

<!-- handbook:subgates:begin -->
| Sub-gate | Module | Applies to | Under `vm_ul6_strict_v2` |
|---|---|---|---|
| `sharpe_threshold` | `common.py` | maker + taker | blocking |
| `max_drawdown` | `common.py` | maker + taker | blocking |
| `winning_day_pct` | `common.py` | maker + taker | blocking |
| `fill_quality` | `maker.py` | maker | blocking |
| `fill_rate_validation` | `maker.py` | maker | blocking |
| `ic_evaluation` | `taker.py` | taker | blocking |
| `min_sample_size` | `min_sample_size.py` | maker + taker | blocking |
| `single_day_dominance` | `single_day_dominance.py` | maker + taker | blocking |
| `loo_day_sensitivity` | `loo_day_sensitivity.py` | maker + taker | blocking |
| `outlier_trade_removal` | `outlier_trade_removal.py` | maker + taker | blocking |
| `day_bootstrap_ci` | `day_bootstrap_ci.py` | maker + taker | blocking |
| `stationary_block_bootstrap` | `stationary_block_bootstrap.py` | maker + taker | blocking |
| `deflated_sharpe_maker` | `deflated_sharpe_maker.py` | maker | blocking |
| `replay_parity` | `replay_parity.py` | maker + taker | blocking, deferrable to `shadow_exit` |
| `inventory_mtm` | `inventory_mtm.py` | maker | blocking |
| `cost_uncertainty` | `cost_uncertainty.py` | maker + taker | blocking |
| `monthly_distribution` | `monthly_distribution.py` | maker + taker | blocking |
| `edge_per_round_trip` | `edge_per_round_trip.py` | maker + taker | blocking |
| `trade_concentration` | `trade_concentration.py` | maker + taker | blocking |
| `force_flat_residual` | `force_flat_residual.py` | maker + taker | blocking |
| `selection_deflated_sharpe` | `selection_deflated_sharpe.py` | maker + taker | advisory |
<!-- handbook:subgates:end -->

`replay_parity` is deferrable: with no live intents to compare against, a missing
parity report is NOT_APPLICABLE and recorded as a Shadow-exit obligation (section 8),
not an INVALID run. `selection_deflated_sharpe` is NOT_APPLICABLE when the alpha has
no search record.

### Gate C thresholds (`thresholds:` in the profile)

<!-- handbook:thresholds:begin -->
| Key | Taker | Maker |
|---|---|---|
| `sharpe_oos_min` | `1.8` | `1.0` |
| `max_drawdown_pct` | `10` | `15` |
| `winning_day_pct_min` | `58` | `60` |
| `min_oos_trades` | `200` | — |
| `min_oos_days` | `30` | — |
| `replay_parity_match_pct_min` | `95.0` | `95.0` |
| `cost_uncertainty_p95_lower_bound_min_pts` | `0.0` | `0.0` |
| `top_month_contribution_max_pct` | `50.0` | `50.0` |
| `drawdown_to_avg_monthly_max_ratio` | `2.0` | `2.0` |
| `monthly_distribution_min_months` | `2` | `2` |
| `mean_net_edge_pts_per_trade_min` | `10.0` | `10.0` |
| `top_trade_share_max_pct` | `40.0` | `40.0` |
| `worst_loss_share_max_pct` | `50.0` | `50.0` |
| `force_flat_trip_share_max_pct` | `30.0` | `30.0` |
| `sharpe_is_min` | — | `1.0` |
| `pnl_per_fill_min_pts` | — | `2.0` |
| `pnl_per_fill_min_pts_multiplier` | — | `0.5` |
| `edge_to_cost_ratio_min` | — | `1.5` |
| `adverse_fill_pct_max` | — | `40` |
| `fill_rate_deviation_max` | — | `0.5` |
| `min_fills` | — | `300` |
| `min_days` | — | `60` |
| `outlier_day_contribution_max_pct` | — | `25` |
| `loo_day_sign_preserved` | — | `true` |
| `outlier_trade_removal_pct` | — | `5` |
| `bootstrap_ci_lower_bound_min` | — | `0` |
| `bootstrap_n_resamples` | — | `2000` |
| `bootstrap_alpha` | — | `0.05` |
| `block_bootstrap_ci_lower_bound_min` | — | `0` |
| `block_bootstrap_block_size_days` | — | `5` |
| `block_bootstrap_n_resamples` | — | `1000` |
| `block_bootstrap_alpha` | — | `0.05` |
| `deflated_sharpe_min` | — | `0.5` |
| `deflated_n_trials` | — | `1` |
| `cost_floor_per_fill_pts` | — | `0.5` |
<!-- handbook:thresholds:end -->

---

## 7. Gates D, E and F

### Gate D: promotion eligibility

```bash
hft alpha promote --alpha-id <id> --profile vm_ul6_strict_v2
```

Source: `src/hft_platform/alpha/promotion.py:_evaluate_gate_d_verdict`. Checks:
`sharpe_oos`, `max_drawdown`, `turnover`, `correlation_pool_max` (required),
`latency_profile` (required), `equity_source` (`require_real_equity: true`; allowed
`real`, `real_no_trade`; `synthetic` is rejected), `latency_audit_strict` (P95 for
place/update/cancel, `hft_platform.alpha.latency_audit --strict`),
`replay_parity_audit` (a `match_pct` below 95.0 fails, a report without a numeric
`match_pct` is an ERROR, a missing report is deferred), and re-checks of the
`inventory_mtm` and `cost_uncertainty` sub-gate payloads.

### Gate E: paper trade and shadow sessions

```bash
make research-record-paper ALPHA=<id> ARGS='--trading-day 2026-03-05 --execution-reject-rate 0.004 --reject-rate-p95 0.008 --regime trending'
make research-summarize-paper ALPHA=<id> ARGS='--out outputs/paper_summary_<id>.json'
make research-check-paper-governance ALPHA=<id> ARGS='--strict --out outputs/paper_governance_<id>.json'
```

Run `research-check-paper-governance` before promotion to fail fast on Gate E.
Promotion artifacts include `paper_governance_report.json`. Sources:
`src/hft_platform/alpha/canary.py`, `src/hft_platform/alpha/paper_trade_batch.py`.

### Gate F

NOT_APPLICABLE under `vm_ul6_strict_v2` (`not_applicable_gates: gate_f: rust_paused`,
`enforce_rust_benchmark_gate: false`). Gate F has no status of its own; PRODUCTION
is set by the manual config PR.

### Run settings the profile applies (`pipeline_overrides:`)

`research.pipeline run` applies these when the user has not set a non-default value.
The `*_gate_d` keys feed Gate D, the paper-trade and shadow keys feed Gate E.

<!-- handbook:pipeline:begin -->
| Key | `vm_ul6_strict_v2` | Run default |
|---|---|---|
| `latency_profile_id` | `r47_maker_shioaji_p95_v2026-04-24_measured` | `sim_p95_v2026-02-26` |
| `local_decision_pipeline_latency_us` | `1000` | `250` |
| `submit_ack_latency_ms` | `395.0` | `36.0` |
| `modify_ack_latency_ms` | `395.0` | `43.0` |
| `cancel_ack_latency_ms` | `70.0` | `47.0` |
| `live_uplift_factor` | `1.8` | `1.5` |
| `maker_fee_bps` | `0.0` | `-0.2` |
| `taker_fee_bps` | `0.35` | `0.2` |
| `stat_pvalue_threshold` | `0.05` | `0.1` |
| `min_stat_tests_pass` | `3` | `2` |
| `bootstrap_samples` | `3000` | `1000` |
| `opt_signal_threshold_steps` | `12` | `8` |
| `opt_max_is_oos_gap` | `0.5` | `1.0` |
| `opt_min_neighbor_objective_ratio` | `0.8` | `0.6` |
| `opt_min_deflated_sharpe` | `0.2` | `-0.1` |
| `required_data_provenance_fields` | `source, generator, seed, created_at, data_file, split, symbols` | `[]` |
| `data_ul` | `6` | `2` |
| `stress_latency_multiplier` | `2.0` | `1.5` |
| `stress_fee_multiplier` | `2.0` | `1.5` |
| `min_stress_sharpe_ratio` | `0.7` | `0.5` |
| `stress_drawdown_limit_multiplier` | `1.0` | `1.25` |
| `min_shadow_sessions` | `20` | `5` |
| `max_execution_reject_rate` | `0.005` | `0.01` |
| `min_paper_trade_calendar_days` | `28` | `7` |
| `min_paper_trade_trading_days` | `20` | `5` |
| `min_paper_trade_session_minutes` | `60` | `30` |
| `min_sharpe_oos_gate_d` | `1.8` | `1.0` |
| `max_abs_drawdown_gate_d` | `0.1` | `0.2` |
| `max_turnover_gate_d` | `1.2` | `2.0` |
| `max_correlation_gate_d` | `0.5` | `0.7` |
| `enforce_rust_benchmark_gate` | `false` | `false` |
<!-- handbook:pipeline:end -->

---

## 8. Canary, Shadow, Live (frozen)

- **Canary:** `hft alpha canary status|evaluate|auto-evaluate`;
  scheduler `src/hft_platform/alpha/canary_scheduler.py`.
- **Shadow:** the locked production compose (`docker-compose.prod.locked.yml`,
  loop_v1 step L3a), with `HFT_INTENT_RECORDER_ENABLED=1` so the session can be replayed.
- **Replay parity before Live (ADR-003).** A `replay_parity_audit` obligation in the
  promotion YAML holds the canary: its weight does not rise and it does not graduate
  until `report.json` files from `hft run --mode replay`
  (`src/hft_platform/replay/cli_runner.py`) certify parity. Pass them with
  `hft alpha canary evaluate --replay-parity-report <path>` (repeatable; `--apply`
  records the certification) or at promotion with
  `hft alpha promote --replay-parity-report <path>`. Promotion must name the runtime
  strategy with `--replay-strategy module:Class`; the obligation pins the sha256 of
  that module's source, so a code change after promotion means promoting again. Each
  report must be `eligible`, name the obligation's `strategy_id`, match the pinned
  fingerprint, compare at least one live intent and reach its `min_match_pct`. A
  typed-in match percentage is not evidence. Checks:
  `src/hft_platform/alpha/shadow_parity.py`; deep dive:
  `docs/runbooks/replay-parity-gate.md`.
- **Live:** the PR that adds `config/loops/<id>.yaml` cites the promotion YAML, which
  lists no `obligations` and records the replay reports under
  `certified_obligations`. While the freeze holds, `config/loops/` holds only
  `r47_tmf_v1`. After it: `docs/runbooks/loop_v1_migration_l7.md`.

---

## 9. Kill ledger and clustering

- **Schema:** ClickHouse `audit.alpha_kill_ledger`
  (`src/hft_platform/migrations/clickhouse/20260505_001_create_alpha_kill_ledger.sql`)
  with the git-ignored `research/alphas/_kill_ledger.jsonl` fallback. Under research v2
  the governed record is the evidence-root ledger; the ClickHouse table is a
  rebuildable index.
- **Writes:** `promote_alpha` records a kill only for a KILL verdict (the Gate C
  verdict in the run's `backtest_report.json`, or the Gate D verdict), through
  `KillRecord.from_verdict`, which refuses PASS, NEEDS-MORE-DAYS, INCONCLUSIVE and
  INVALID. `HFT_KILL_LEDGER_ENABLED=0` turns the writes off (default on).
- **Idempotency:** `kill_id = sha256(alpha_id ":" gate ":" stable_artifact_hash)`,
  with `":" run_id` appended for kills from `KillRecord.from_verdict`. Both sinks
  dedupe on `(alpha_id, kill_id)`.
- **Manual kill:** `hft alpha kill <alpha_id> --reason "<text>" [--gate manual] [--killed-by <id>]`
  (gates `A`-`F`, `pre_screen`, `cluster`, `manual`).
- **Cluster:** `hft alpha cluster [--base-dir research/experiments] [--threshold 0.7] [--metric pearson|spearman] [--write-artifact] [--json]`:
  single-linkage clustering on `1 - |corr|` over signals under `<base_dir>/runs/`
  (`src/hft_platform/alpha/cluster.py`). `--write-artifact` merges into the
  git-ignored `research/alphas/_cluster_assignments.json`. Smoke test:
  `uv run python scripts/generate_slice_d_signal_corpus.py`, then
  `hft alpha cluster --base-dir research/experiments/_slice_d_fixtures --json`.
- **Expressions:** the one expression language is
  `research/combinatorial/expression_lang.py` (an `ast` whitelist over
  `research/combinatorial/operator_library.py`; no string eval).
  `AlphaManifest.dsl_formula` is descriptive text that nothing parses.

---

## 10. Factory, layout and artifacts

`research.factory` is read-only (research v2 C6):

```bash
uv run python -m research.factory audit [--fail-on-warning] [--data <path> ...]  # reports/factory_audit.json
uv run python -m research.factory index                                          # reports/pipeline_index.json
```

The governed pipeline runs `audit` as its preflight and `index` as its finalization.

```
research/
├── alphas/          governed alpha packages (<alpha_id>/)
├── backtest/        shared backtest runtime, Gate C runtime
├── calibration/     queue calibration (generator)
├── candidate_loop/  LLM candidate loop (generator)
├── combinatorial/   expression search and operators (generator)
├── data/            datasets (raw/, interim/, processed/, models/); not in git
├── data_pipeline/   governed ClickHouse reader and exports
├── experiments/     frozen legacy evidence (append only)
├── knowledge/       notes, summaries, paper index
├── registry/        schemas, alpha CLI bodies, legacy evidence reader
├── tools/           core tools; tools/legacy/ is archived
├── archive/         retired alphas and implementations
└── reports/         factory audit and index outputs
```

- Never write reports under `research/alphas/<alpha_id>/`.
- Keep binary artifacts out of source zones (`alphas/`, `registry/`, `tools/`, `backtest/`).
- Keep generated artifacts in the evidence root, `research/experiments/` (legacy) or `outputs/`.
- Do not place runnable scripts at the `research/` root.
- Keep each `.npy/.npz` dataset with its metadata sidecar.

### Internal debug path

```bash
export HFT_RESEARCH_ALLOW_TRIAGE=1
make research-triage ALPHA=<alpha_id> OWNER=<owner> DATA='<path.npy>' ARGS='--skip-gate-b-tests --no-promote'
```

Triage output is exploratory and never promotable.

---

## 11. CI gates

| Target | Purpose |
|---|---|
| `make research-check` | research ruff (bug classes), `tests/research`, `tests/unit/research`, alpha `tests/`, coverage floor |
| `make discipline-hft` | HFT-P004 money-field rule |
| `make latency-gate-ci` | `hft_platform.alpha.latency_audit --strict` over scorecards |
| `make latency-audit` | standalone Gate D latency tolerance check |
| `make coverage-domain` | per-package coverage floors |
| `make ci` | format, lint, typecheck, dependency boundary, test hygiene, coverage |
| `.github/workflows/freeze-guard.yml` | blocks new strategies in the live registry under L11 |

---

## 12. Cross-references

- Decisions: `docs/adr/003_replay_parity_stage.md`, `docs/adr/005_research_v2.md`.
- Runbooks: `docs/runbooks/replay-parity-gate.md`, `docs/runbooks/backtest-engine-selection.md`,
  `docs/runbooks/research-feature-promotion.md`, `docs/runbooks/forced_promotion.md`,
  `docs/runbooks/loop_v1_migration_l7.md`.
- Skills: `.agent/skills/hft-alpha-research/`, `.agent/skills/validation-gate/`,
  `.agent/skills/hft-backtest/`.
- Tree rules: `research/AGENTS.md`.
