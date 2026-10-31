# Research Factory — Operational Handbook

`research/` is the governed pipeline workspace for alpha development.
Official process is intentionally reduced to **one entrance and one promotable flow**.

This file is the **canonical operational handbook** for the factory (entrance, 8 stages,
folder layout, data governance, paper-trade fast path, triage). For the gate/promotion
contract and profile semantics, the canonical source is
[`docs/runbooks/alpha-development-workflow.md`](../docs/runbooks/alpha-development-workflow.md).

> `research/SOP.md` is now a thin pointer to this file — do not duplicate process detail there.

## Canonical docs (open the one that matches what you are doing)

| Topic | Canonical source |
| --- | --- |
| Lifecycle + Gates A–F + profile semantics | `docs/runbooks/alpha-development-workflow.md` |
| Factory operations (this handbook) | `research/README.md` |
| Replay-parity gate (why / enable / schema / fail-closed) | `docs/runbooks/replay-parity-gate.md` |
| Research data source, L1/L2 formats, **book-depth contract (0-5, never assume L5)** | `.agent/rules/70-research-data.md` |
| Governed L2+tick export contract | `research.data_pipeline`, `make research-export-l2-ticks`, `make research-validate-l2-ticks` |

## Factor Factory Pipeline (8 Stages)

```
論文(MCP) → 基礎原型(Python) → 資料 → 回測(延遲+成本) → 因子有效(統計) → 參數優化 → Paper trade(1w) → Live(Rust)
```

| #   | Stage                  | Folder                     | Role            | Skill / Tooling                     | Checklist |
| --- | ---------------------- | -------------------------- | --------------- | ----------------------------------- | --------- |
| 1   | 論文 (Paper Intake)    | `knowledge/`               | `planner`       | `iterative-retrieval` + `arxiv` MCP | scope, data plan, acceptance criteria; `make research-fetch-paper ARXIV=...` |
| 2   | 基礎原型 (Prototype)   | `alphas/<id>/`             | `architect`     | `hft-architect`                   | scaffold via `research/tools/alpha_scaffold.py`; gate/latency/cost assumptions |
| 3   | 資料 (Data)            | `data/`                    | `planner`       | `hft-alpha-research` (data)             | dataset under allowed roots + metadata sidecar |
| 4   | 回測 (Backtest)        | `backtest/`                | `architect`     | `hft-backtest`, `validation-gate` | engine auto-selected from `manifest.strategy_type` |
| 5   | 因子有效 (Statistical) | `experiments/validations/` | `code-reviewer` | `validation-gate`                   | regression, governance-bypass, operational-risk checks |
| 6   | 參數優化 (Param Opt)   | `experiments/runs/`        | `architect`     | `hft-backtest`                           | anti-overfit trap detection, robustness sweep |
| 7   | Paper Trade            | `experiments/promotions/`  | `code-reviewer` | Gate-E paper-trade `make` targets   | min 5 shadow sessions (Gate E `min_shadow_sessions=5`) |
| 8   | Live (Rust)            | `rust_core/src/`           | `architect`     | `hft-rust`                          | profile Python baseline before porting |

The Role column is a historical label for the kind of work (planning,
architecture, review); delegation rules are in `AGENTS.md`. Skills are under
`.agent/skills/<name>/SKILL.md` (native in Claude Code and Codex).

### Stage-4 engine selection (automatic, via `manifest.yaml`)

- `strategy_type: taker` → `TakerEngine` (wraps `HftNativeRunner`, `PowerProbQueueModel`)
- `strategy_type: maker` → `MakerEngine` (CK-direct, `QueueDepletionFill(qf=0.5)`)
- Cost model: `config/research/cost_profiles.yaml`; Gate C thresholds: `config/research/profiles/vm_ul6_strict.yaml`.
- MakerEngine needs local ClickHouse: `docker compose up -d clickhouse`.
- MakerEngine (`ClickHouseSource`) and the queue calibration read ClickHouse only through `research.data_pipeline.reader.GovernedReader`. It applies the session rule (TAIFEX windows plus the XTAI calendar; no calendar is an error), dedup `content_multiplicity_v1` for ticks and the BidAsk snapshot rule, and orders rows by `exch_ts, ingest_ts, seq_no`. A date is a trading day: `[previous trading day 14:00, D 14:00)` Taipei, so the night session belongs to the next trading day. `ClickHouseSource.reads` records what each read fetched, filtered and deduplicated. Prices stay at the ClickHouse x1,000,000 scale.
- `research.backtest.taker_sim.TakerSim` is the taker simulator that books exchange-model fills: the `HftNativeRunner` signal ladder sent as IOC orders at the touch, per-contract costs from the cost profile, one dated row per trading day, Sharpe on the daily series. Gate C's taker `gate_c.sharpe_oos` (and the scorecard `sharpe_oos` Gate D reads) comes from it; `HftNativeRunner`'s mid-to-mid Sharpe is screening only (`gate_c.screening_sharpe_oos`). Costs are per contract from the cost profile everywhere; the bps fee and rebate parameters are gone.
- A maker `instrument: <ROOT>_frontmonth` (e.g. `TMF_frontmonth`) runs on one series rolled across contract months (ADR-004, `MakerEngine.run_rolled`). The roll rule is pre-registered in `config/research/protocols/front_month_roll_v1.yaml` and matches the live R1 binding: trading day D runs on the contract with the earliest delivery date on or after D and covers `[previous trading day 14:00, D 14:00)` Taipei, so `n_days` counts trading days across contracts. On a delivery day the series moves to the next month at 13:30, as live R1 does, and that day's row sums its two segments. Each segment starts flat (the strategy is flattened too). Each run records the protocol's sha256 in its RunStamp and the contracts in `details.roll`; a protocol or calendar that cannot be used, or a scheduled segment with no events of its contract, makes the run INVALID. Never edit a protocol a run has used; add a `_v2`.
- Each Gate C run is written once by `ExperimentTracker.log_run` to `<experiments_dir>/runs/<run_id>/` (`meta.json` with the RunStamp, `backtest_report.json`, `scorecard.json`). Strict runs use the evidence root (`HFT_RESEARCH_EVIDENCE_ROOT`) and seal the run with a `manifest.json`; see `src/hft_platform/alpha/evidence.py`.
- Ad-hoc scripts in `research/tools/legacy/` are archived — **do not use for official results**.

## Paper -> Prototype Bridge

```bash
make research-paper-prototype PAPER_REF=120 ARGS='--alpha-id ofi_p120 --complexity O1'
```

This command scaffolds `research/alphas/<alpha_id>/` and writes reverse mapping into
`research/knowledge/paper_index.json` (`alphas` field), making paper-to-prototype traceable.
Gate A(strict) requires `manifest.paper_refs` and paper_index linkage.

## Data Governance (Enforced in Strict Gate A)

```bash
make research-stamp-data-meta DATA_PATH=research/data/raw/example.npy ARGS='--source-type real --owner charlie --symbols 2330'
make research-validate-data-meta DATA_PATH=research/data/raw/example.npy
```

Strict Gate A enforces:
- dataset path must be under allowed roots
- dataset must carry metadata sidecar (`.meta.json` / `.metadata.json`)
- metadata contract keys and row-count consistency

Data source, scaling (CH x1,000,000 vs platform x10,000), and the governed L2+tick export
contract are documented in `.agent/rules/70-research-data.md` and
`research.data_pipeline` — do not reimplement the dtype/sidecar/validation rules.

### Synthetic Data Fast Path (OU-Hawkes-Markov v2)

```bash
make research-gen-synth-lob OUT=research/data/processed/queue_imbalance/synthetic_qi_v2_train.npy \
  ARGS='--version v2 --rng-seed 42 --symbols TXF,MXF --split train'
make research-validate-data-meta DATA_PATH=research/data/processed/queue_imbalance/synthetic_qi_v2_train.npy
```

This generator writes a sidecar with provenance fields (`source`, `generator`, `seed`, `symbols`, `split`) plus UL metadata.

## One Entrance

```bash
make research ALPHA=<alpha_id> OWNER=<owner> DATA='<path1.npy [path2.npy ...]>' [ARGS='...']
```

This runs strict `research.pipeline run`, which includes the factory optimize preflight by default:

1. `factory optimize` preflight (`init -> converge-tools -> clean -> audit(strict)`)
2. Gate A/B/C validation
3. Gate D/E promotion
4. `factory index` finalization

Strict runs are **governed** (ADR-005): they stop with rc 2 unless
`HFT_RESEARCH_EVIDENCE_ROOT` is set to an absolute path outside the repository, the
tree is clean and the validation profile resolves. Runs and validation artifacts
go to that root (`runs/<run_id>/`, `validations/<alpha_id>/<stamp>/`), each sealed
by a `manifest.json` of sha256s, and every gate that ran appends one event to
`ledger/verdicts.jsonl`. Governed promotion reads only a sealed scorecard under that
root, writes its Gate D-F reports to `promotions/` there and appends their events;
the run must start and publish on the same commit. The correlation tools skip
sealed scorecards.

No run rewrites `impl.py` status. `hft alpha list` and `factory index` show the
status derived from the ledger (`src/hft_platform/alpha/status.py`, rules v1):
the highest of Gates A-E whose latest event passed, each no older than the one
below it; an INVALID verdict is ignored; any KILL makes the alpha KILLED. An
alpha with no ledger events, or whose manifest declares PRODUCTION, DEPRECATED
or KILLED, shows the manifest status; a ledger KILL still wins. A malformed
ledger stops both commands (rc 2). Triage runs
(`research.pipeline triage`) stay exploratory: labelled in the RunStamp, written to
`--experiments-dir`, never in the ledger.

```bash
python -m research evidence init            # layout + local git init (no remote)
python -m research evidence import-kills    # archive pre-ledger kills; --dry-run, --from jsonl
python -m research evidence audit-stale     # mark runs a registered defect affects; --dry-run, --json
python -m research status [--alpha ID] [--json]
```

`status` prints each alpha's effective status, its source, the declared status,
governed runs (`<evidence root>/runs`), legacy runs (`research/experiments/runs`)
and archived pre-ledger kills. `factory index` records the same run counts.
Legacy evidence is read-only (`research/registry/legacy_evidence.py`): rows that
tests wrote into the real sinks (a pytest temp path, or an automated row for an
alpha with no package under `alphas/` or `archive/`) are quarantined and counted,
never deleted. `import-kills` copies `_kill_ledger.jsonl` and ClickHouse
`audit.alpha_kill_ledger` (365-day TTL) into `legacy/kills.jsonl`; those kills
never derive a status, because they predate verdict rules v1. A ClickHouse
failure stops the import (rc 2) unless `--from jsonl`.

Stale results are computed from `config/research/defects.yaml`, which names each
defect by its fix commit and the components it touched. `audit-stale` appends
one mark per (run, defect) to `ledger/stale.jsonl` when the run carries one of
those components and its commit does not contain the fix, it recorded no
commit, or the defect is still open; a dataset defect also needs every real
dataset's sidecar to record a dedup rule. It audits governed runs from the
ledger and every legacy run except pytest fixtures. Marks never change a status
and never edit evidence; `status` shows `stale_runs` and flags an alpha whose
status rests on a marked event (`status_on_stale_run`). The audit needs full
git history (rc 2 when a fix commit is missing). Add a defect when it is fixed
or found, then rerun the audit.

### VM-UL6 strict profile (institutional/near-live strictness)

```bash
make research ALPHA=<alpha_id> OWNER=<owner> DATA='<path.npy>' ARGS='--validation-profile vm_ul6_strict_v2'
```

`vm_ul6_strict` tightens latency/cost assumptions, stress multipliers, promotion thresholds,
paper-trade requirements, the Rust benchmark gate, and data provenance metadata
requirements. Since Stage 2 (2026-05-28) every entrypoint (`make research`,
`python -m research.pipeline run`, `hft alpha pipeline run`, `hft alpha validate|promote`)
file-resolves the same `config/research/profiles/vm_ul6_strict.yaml`. The legacy `vm_ul6`
token is accepted with a `DeprecationWarning` for one release.

### Backtest engine selection

For the bias matrix (14× pessimistic to 577× optimistic across engines), and calibration
requirements, see
[`docs/runbooks/backtest-engine-selection.md`](../docs/runbooks/backtest-engine-selection.md).

### Statistics (one module)

Every multiple-testing correction, resampler, Sharpe deflation and IC estimate a gate
or a generator uses is defined once, in `src/hft_platform/alpha/stats.py`; no gate or
generator keeps a private copy. Deflated Sharpe has one definition, the Bailey/López de
Prado probability against `SR0` (the Sharpe the best of N null trials is expected to
reach). Gates with Sharpe-unit thresholds (`deflated_sharpe_min`,
`opt_min_deflated_sharpe`) compare `SR - SR0` against the unchanged threshold, and report
the probability beside it. Benjamini-Hochberg takes the test count from the trials
ledger when it knows tests whose p-values are not in hand. Every Gate C read of an
out-of-sample holdout is recorded in `ledger/holdout_access.jsonl` under the evidence
root; a second read by the same alpha is flagged (`holdout_access.reused`), never gated.

Definitions retired by the move (research v2 C3):

| Retired | Difference | Replacement |
|---|---|---|
| `_param_opt` selection haircut `sqrt(2 ln N / T)` | annualized Sharpe less a per-observation penalty over T ticks: about 0 | `stats.selection_sr0`, trials' spread floored at the null spread; never smaller |
| `deflated_sharpe_maker` haircut, same formula | same unit mismatch; 0 at the profile's `deflated_n_trials: 1` | `stats.selection_sr0`; still 0 at one trial |
| `research/tools/bayesian_opt.py` haircut, same formula | same unit mismatch | `stats.selection_sr0` |
| `_stat_tests._bh_correction`, `smma_validation.benjamini_hochberg` | identical values | `stats.benjamini_hochberg` (adds `n_tests`) |
| `_resampling` bootstraps, `smma_validation` block/cluster bootstraps, `deflated_sharpe`, `effective_test_count` | identical values | `stats.*` (`block_bootstrap_mean` is now `moving_block_bootstrap_mean`) |
| `_safe_spearmanr`, `smma_validation.spearman_ic`, `candidate_loop` `_rank_ic`, `screener._ic` | identical except on near-constant input: the old rules (std at most 1e-12, std exactly 0, range at most 1e-12) are now one, range at most 1e-12 means undefined | `stats.rank_ic` / `stats.pearson_ic`; callers keep their own undefined-as-0 or NaN |

Retired in research v2 C4:

| Retired | Difference | Replacement |
|---|---|---|
| `taker_sim.daily_sharpe`, `metrics.compute_sharpe`, `search_engine._signal_sharpe` bodies | identical except on a constant series, where `np.std` can return ~1e-16 instead of 0 and the old copies reported a Sharpe around 1e16; now 0 | `stats.sharpe_ratio`; each caller keeps its own series and annualization |
| `metrics.compute_ic` / `compute_ic_incremental` per-chunk Pearson, `metrics.compute_ic_ttest` | identical values | `stats.chunked_ic`, `stats.chunk_pearson_ic`, `stats.ic_ttest` (the `metrics` names remain as wrappers) |

`search_engine`'s score is a screening scale, not a Sharpe: one observation is one
row of the feature arrays, multiplied by sqrt(252) only so scores stay comparable
with earlier runs. The engine reports how many expressions it scored
(`AlphaSearchEngine.trial_summary()`, also in `save_results` and `hft alpha search
--out`), and `hft alpha mine promote` records it as
`experiment_metadata.search_trials`. The summary also carries `effective`, the Li-Ji
count of those trials' signals (`stats.effective_test_count`), recorded as
`experiment_metadata.search_trials_effective`. Gate C's `selection_deflated_sharpe`
sub-gate (maker and taker) reports the daily Sharpe less `stats.selection_sr0` of that
effective count against `deflated_sharpe_min`; it is NOT_APPLICABLE when the alpha has no
search record, and advisory until a profile lists it in `blocking_sub_gates`.

### Generators (research v2 C4)

Mining (`research/combinatorial`), the candidate loop (`research/candidate_loop`) and
queue calibration (`research/calibration`) produce candidates or calibration inputs;
none of them promotes or writes a KILL.

- The candidate loop's top status is `SHORTLISTED` (the v1 spec's `PROMOTED`; old rows
  are read as `SHORTLISTED`). Every test-split read is logged in the evidence root's
  `ledger/holdout_access.jsonl`, the same log Gate C writes.
- A shortlisted candidate reaches Gate C as `research.candidate_loop.gate_c_alpha.
  CandidateGateCAlpha`: its validator-canonical signal is evaluated once on the day
  panels (`panels.build_panel`) of the same `*_l2.hftbt.npz` files Gate C replays, and
  each tick reads the latest batch at or before the tick's `local_ts`. Regime-off rows,
  `expected_sign: negative` and days that are not `dir_clean` for a `trade_imbalance`
  candidate follow the evaluator. Nothing is hand-ported; no CLI wires it to Gate C yet.
- A calibration profile in `config/research/calibration_profiles.yaml` carries a
  `provenance` block: the RunStamp, the sweep configuration, the train and held-out
  days, and how many candidates the sweep scored. A fallback profile also says why it
  is one.

### `hft alpha` research subcommands (research v2 C5)

`hft alpha {scaffold,search,mine,screen,validate,validate-batch,pipeline}` parse their
arguments in the platform CLI, then run as `python -m research alpha <same arguments>`
(`src/hft_platform/cli/_research.py` forwards; the bodies are in
`research/registry/alpha_cli.py`). The exit code is the child's. Gate C runs only with an
injected runtime (`research/backtest/gate_c_runtime.py:build_gate_c_runtime`); called
without one, `run_gate_c` raises `GateCRuntimeMissingError` instead of importing research.
Where `research/` is absent (production images) these subcommands exit 2 with a message.

## One Factory Flow

The factory preflight sequence is fixed:

1. `init`
2. `converge-tools`
3. `clean`
4. `audit --fail-on-warning`
5. `index` (post-pipeline finalization)

Standalone command:

```bash
uv run python -m research.factory optimize
```

## Canonical Layout

```
research/
├── alphas/              # Stage 2: governed alpha packages (<alpha_id>/)
├── backtest/            # Stage 4: shared backtest runtime and metrics
├── combinatorial/       # expression search and operators
├── data/                # Stage 3: datasets
│   ├── raw/
│   ├── interim/
│   ├── processed/
│   └── models/          # trained model binaries (.onnx, .zip)
├── experiments/         # Stage 5-7: run artifacts
│   ├── runs/
│   ├── comparisons/
│   ├── validations/
│   └── promotions/
├── knowledge/           # Stage 1: notes, summaries, paper index
│   ├── notes/
│   ├── papers/
│   ├── summaries/
│   └── reports/
├── registry/            # alpha registry and scorecard schemas
├── tools/               # core tools only
│   └── legacy/          # non-core and historical tools
├── archive/             # legacy non-pipeline assets
│   └── implementations/ # moved execution/simulation/rl code
├── logs/
├── reports/             # factory audit/optimize/index outputs
└── results/
```

## Artifact Policy

- Never write reports under `research/alphas/<alpha_id>/`.
- Validation outputs: `research/experiments/validations/<alpha_id>/<stamp>/`
- Run outputs: `research/experiments/runs/<run_id>/`
- Promotion outputs: `research/experiments/promotions/<alpha_id>/<stamp>/`
- Pipeline summaries: `outputs/research_pipeline/`
- Keep binary artifacts out of source zones (`alphas/`, `registry/`, `tools/`, `backtest/`).
- Do not place runnable scripts at `research/` root.

## Paper-Trade Governance Fast Path (Gate E)

```bash
make research-record-paper ALPHA=queue_imbalance ARGS='--trading-day 2026-03-05 --execution-reject-rate 0.004 --reject-rate-p95 0.008 --regime trending'
make research-summarize-paper ALPHA=queue_imbalance ARGS='--out outputs/paper_summary_queue_imbalance.json'
make research-check-paper-governance ALPHA=queue_imbalance ARGS='--strict --out outputs/paper_governance_queue_imbalance.json'
```

Use `research-check-paper-governance` before promotion to fail fast on Gate E readiness
(`min_shadow_sessions=5` default). During `hft alpha promote` / `research.pipeline run`,
promotion artifacts also include `paper_governance_report.json` under
`research/experiments/promotions/<alpha_id>/<stamp>/`.

## Internal Debug Path (Non-Official)

`triage` is available only for internal debugging and is disabled by default:

```bash
export HFT_RESEARCH_ALLOW_TRIAGE=1
make research-triage ALPHA=<alpha_id> OWNER=<owner> DATA='<path.npy>' ARGS='--skip-gate-b-tests --no-promote'
```

Outputs from `triage` are always non-promotable.

## Governance Rules

- Keep `paper_refs` mapped in `research/knowledge/paper_index.json`.
- Keep each paper-backed alpha linked via `paper-to-prototype` (`paper_index.json` -> `alphas`).
- Keep governed dataset metadata sidecars with each `.npy/.npz` file.
- Keep binary artifacts out of source zones (`alphas/`, `registry/`, `tools/`, `backtest/`).
- Keep generated artifacts in `research/experiments/` or `outputs/`.
- Do not place runnable scripts at `research/` root.
