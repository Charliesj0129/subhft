---
name: research-factory
description: Use when running the alpha research pipeline end-to-end, from paper intake through live promotion. Covers the 8-stage factory workflow, gate validation, artifact policy, and factory commands.
---

# Research Factory

The governed end-to-end pipeline for turning academic papers into live trading alphas. Every alpha passes through 8 stages with mandatory gate checks before promotion.

## Overview

### 8-Stage Pipeline

```
論文(MCP) → 基礎原型(Python) → 資料 → 回測(延遲+成本) → 因子有效(統計) → 參數優化 → Paper trade(1w) → Live(Rust)
```

### Single Entrance

```bash
make research ALPHA=<alpha_id> OWNER=<owner> DATA='<path.npy>'
```

### Factory Preflight Sequence

The factory runs these steps before entering the pipeline:

1. **init** — resolve alpha_id, owner, data path, validation profile
2. **converge-tools** — ensure research toolchain is installed and compatible
3. **clean** — remove stale intermediate artifacts from previous runs
4. **audit** — verify manifest, sidecar metadata, and dataset integrity
5. **index** — update paper_index.json and alpha registry

## Stage-by-Stage Reference

| # | Stage | Folder | Role | Skill | Key Command |
| --- | --- | --- | --- | --- | --- |
| 1 | 論文 Paper Intake | `research/knowledge/` | planner | iterative-retrieval | `make research-paper-prototype PAPER_REF=<ref>` |
| 2 | 基礎原型 Prototype | `research/alphas/<id>/` | architect | hft-architect | `uv run python -m research scaffold <alpha_id>` |
| 3 | 資料 Data | `research/data/` | planner | hft-backtest-engine | `make research-gen-synth-lob`, `make research-validate-data-meta` |
| 4 | 回測 Backtest | `research/backtest/` | architect | hft-backtest-engine + validation-gate | `uv run python -m research.factory run-gate-c` |
| 5 | 因子有效 Statistical | `research/experiments/validations/` | code-reviewer | validation-gate | `uv run hft alpha validate <alpha_id>` |
| 6 | 參數優化 Param Opt | `research/experiments/runs/` | architect | hft-backtest-engine | `uv run python -m research.factory optimize` |
| 7 | Paper Trade | `research/experiments/promotions/` | code-reviewer | validation-gate | `make research-record-paper`, `make research-check-paper-governance` |
| 8 | Live Rust | `rust_core/src/` | architect | rust_feature_engineering | `uv run hft alpha promote <alpha_id>` |

### Stage 1: Paper Intake

Use MCP arxiv tools (`search_papers`, `download_paper`, `read_paper`) to locate and retrieve papers. Store notes and extracted formulas in `research/knowledge/`. Scaffold with:

```bash
make research-paper-prototype PAPER_REF=<ref> ARGS='--alpha-id <id> --complexity O1'
```

### Stage 2: Prototype

Scaffold the alpha package and implement `signal.py`:

```bash
uv run python -m research scaffold <alpha_id>
```

This creates the canonical layout under `research/alphas/<alpha_id>/` with manifest, tests, and signal stub.

### Stage 3: Data

Prepare governed datasets with metadata sidecars. Generate synthetic data when real data is unavailable:

```bash
make research-gen-synth-lob OUT=research/data/processed/<id>/synthetic_lob_v2_train.npy \
  ARGS='--version v2 --rng-seed 42 --symbols TXF,MXF --split train'
make research-validate-data-meta DATA_PATH=research/data/processed/<id>/synthetic_lob_v2_train.npy
```

### Stage 4: Backtest

Run Gate C with realistic latency and cost assumptions:

```bash
uv run python -m research.factory run-gate-c <alpha_id> \
  --data research/data/processed/<id>/<file>.npy \
  --latency-profile <profile>
```

### Stage 5: Statistical Validation

Evaluate factor effectiveness, statistical significance, and stress robustness. Outputs land in `research/experiments/validations/<alpha_id>/<stamp>/`.

### Stage 6: Parameter Optimization

Sweep parameter grid for robustness. Results in `research/experiments/runs/<run_id>/`.

```bash
uv run python -m research.factory optimize
```

### Stage 7: Paper Trade

Record minimum 5 shadow sessions, then run governance checks:

```bash
make research-record-paper ALPHA=<id> ARGS='--trading-day YYYY-MM-DD --execution-reject-rate 0.004 ...'
make research-summarize-paper ALPHA=<id> ARGS='--out outputs/paper_summary_<id>.json'
make research-check-paper-governance ALPHA=<id> ARGS='--strict --out outputs/paper_governance_<id>.json'
```

### Stage 8: Live (Rust)

Promote to live after all gates pass. Rust kernel migration for hot-path alphas via `rust_feature_engineering` skill.

## Factory Commands

```bash
# Full pipeline
make research ALPHA=<id> OWNER=<owner> DATA='<path.npy>'

# UL6 strict mode
make research ALPHA=<id> OWNER=<owner> DATA='<path.npy>' ARGS='--validation-profile vm_ul6'

# Factory optimize (standalone)
uv run python -m research.factory optimize

# Debug triage (non-promotable)
export HFT_RESEARCH_ALLOW_TRIAGE=1
make research-triage ALPHA=<id> OWNER=<owner> DATA='<path.npy>' ARGS='--skip-gate-b-tests --no-promote'
```

## Gate Mapping

| Gate | Checks | Strict Mode Additions |
| --- | --- | --- |
| A | manifest + data fields + causality + complexity | paper linkage in paper_index.json + data governance metadata |
| B | per-alpha pytest execution | coverage threshold enforcement |
| C | backtest metrics + statistical significance + stress + parameter robustness | UL6 tightened latency/cost + extra stress scenarios |
| D | Sharpe/drawdown thresholds + feature-set parity | tighter thresholds under vm_ul6 |
| E | shadow sessions (min 5) + execution quality + paper governance report | extended session requirements |

## Roles (Execution Contract)

Roles map onto Agent System v2 (`AGENTS.md` §Roles); the legacy
`.agent/agents/` generation is DEPRECATED (see `.agent/00-MANIFEST.md`).

| Role | v2 equivalent | Responsibility |
| --- | --- | --- |
| planner | Orchestrator | Implementation planning, paper intake coordination |
| architect | Orchestrator, or Sonnet under a tight packet | System design, Rust migration, runtime integration |
| refactor-cleaner | Coding Executor (Sonnet) | Dead code cleanup, alpha package hygiene |
| code-reviewer | Reviewer Agent | Code review, statistical validation interpretation |

## Skills (Execution Contract)

| Skill | Purpose |
| --- | --- |
| iterative-retrieval | Paper search and knowledge extraction via MCP arxiv tools |
| validation-gate | Gate A-E pass/fail interpretation and promotion blockers |
| hft-backtest-engine | Latency-realistic backtest configuration and adapter behavior (v3 feature parity) |
| hft-strategy-dev | Live strategy code, FeatureEngine v3 (27 features), StrategyContext API |
| hft-execution | Execution optimizer, imbalance timer, regime classifier, TCA tracking |
| hft-recorder | Persistence pipeline, WAL durability, ClickHouse schema (15 migrations) |
| hft-ops | Session governor, autonomy, pre/post market checks for live deployment |
| research-data-governance | Dataset sidecars, synthetic LOB, UL6 provenance |
| rust_feature_engineering | Rust kernel migration for live-promoted alphas |

## Artifact Policy

| Artifact Type | Path | Notes |
| --- | --- | --- |
| Alpha source | `research/alphas/<alpha_id>/` | signal.py, manifest.yaml, tests/ |
| Validation outputs | `research/experiments/validations/<alpha_id>/<stamp>/` | Gate A-D results |
| Run outputs | `research/experiments/runs/<run_id>/` | Parameter sweep results |
| Promotion outputs | `research/experiments/promotions/<alpha_id>/<stamp>/` | Gate E, canary config |
| Promotion configs | `config/strategy_promotions/YYYYMMDD/<alpha_id>.yaml` | StrategyRunner loads |
| Pipeline summaries | `outputs/research_pipeline/` | Aggregated reports |
| Daily reports | `reports/` pipeline → Telegram/file via `reports/distributor.py` | collector → facts → reasoner → composer |
| ClickHouse audit | `audit.alpha_*` tables | Validation/promotion audit trail |

NEVER write reports under `research/alphas/<alpha_id>/`. That directory is reserved for source code and manifest only.

## Canonical Layout

```
research/
├── alphas/                          # Alpha implementations (8 surviving)
│   └── <alpha_id>/
│       ├── __init__.py
│       ├── signal.py                # Alpha signal logic
│       ├── manifest.yaml            # Metadata, paper_refs, complexity
│       ├── README.md                # Hypothesis, formula, status
│       └── tests/
│           └── test_signal.py
├── backtest/                        # Backtest runtime and adapters
├── data/                            # Governed datasets
│   ├── raw/                         # Unprocessed market data
│   ├── interim/                     # Intermediate transformations
│   ├── processed/                   # Ready for backtest
│   └── models/                      # Trained model binaries
├── experiments/
│   ├── validations/<alpha_id>/      # Gate A-D outputs per stamp
│   ├── runs/<run_id>/               # Parameter sweep results
│   └── promotions/<alpha_id>/       # Gate E + canary outputs
├── knowledge/                       # Paper notes and formulas
├── registry/                        # Alpha registry and schemas
└── tools/                           # Synthetic data, latency profiles
```

## Paper-Trade Governance

```bash
# Record a single paper-trade session
make research-record-paper ALPHA=<id> ARGS='--trading-day YYYY-MM-DD \
  --started-at <ISO8601> --ended-at <ISO8601> \
  --execution-reject-rate 0.004 --reject-rate-p95 0.008 --regime trending'

# Summarize all recorded sessions
make research-summarize-paper ALPHA=<id> ARGS='--out outputs/paper_summary_<id>.json'

# Pre-check governance before promotion
make research-check-paper-governance ALPHA=<id> ARGS='--strict --out outputs/paper_governance_<id>.json'
```

## Governance Rules

1. `paper_refs` in manifest must map to entries in `paper_index.json`.
2. Every `.npy`/`.npz` dataset must have a `.meta.json` sidecar with provenance fields.
3. Binary artifacts (model weights, large arrays) stay outside alpha source directories.
4. Generated artifacts go to `research/experiments/` or `outputs/`, never into `research/alphas/`.
5. Promotion is config-driven and reversible (canary + rollback).
6. No direct production enable from research-only artifacts.

## Verdict Evidence Commit Cadence

Every verdict (KILL / NEEDS-MORE-DAYS / RESCUED / INCONCLUSIVE / PROMOTED)
triggers an immediate narrow-gate commit of that candidate's evidence in the
same session the verdict is reached — the commit is part of the verdict, not
housekeeping after it. A verdict whose evidence exists on only one disk is
not "recorded".

1. Scope: the candidate's `research/experiments/validations/` subtree (new
   files only — artifacts are append-only, never mutated) plus its matching
   `tests/unit/research/` test files.
2. Stage untracked evidence only; never stage modified files that belong to
   concurrent user work. `__pycache__`/`.pyc` stay ignored.
3. Gate: `ALLOWED_PATHS="<files enumerated from git status --porcelain>" bash
   scripts/check_git_preconditions.sh --narrow-commit`; commit type `alpha:`.
4. Verdicts are faithful — never relax pre-registered floors or gates in the
   committed artifacts to improve an outcome's look.

## Post-Promotion Integration

After Gate E approval:
1. **Promotion config** written to `config/strategy_promotions/YYYYMMDD/<alpha_id>.yaml`
2. **StrategyRunner** loads config at startup → instantiates strategy from registry
3. **Notifications**: `NotificationDispatcher` sends Telegram alert on promotion event
4. **Ops integration**: `SessionGovernor` controls trading phases, `AutonomyMonitor` watches for degradation
5. **Recording**: All fills/orders flow through `RecorderService` → ClickHouse (15 migrations)
6. **TCA tracking**: `ExecutionRouter` enriches fills with `decision_price`/`arrival_price` for slippage analysis
7. **Daily reports**: `reports/pipeline.py` → collector → facts → reasoner → composer → distributor (Telegram/file)

## Batch Operations

```bash
make research-batch-correlation           # Pool correlation matrix
make research-paper-trade-batch           # Batch paper-trade sessions
make research-promote-batch               # Batch promotion (dry-run default)
make research-batch-search QUERIES="..."  # Batch arXiv search
make research-hypothesis-ingest           # Ingest hypotheses from paper_index
make research-hypothesis-top              # Show top-N pending hypotheses
make research-auto-scaffold               # Auto-scaffold from hypothesis queue
make experiment-gc                        # Delete artifacts >90 days (keep latest 3)
```
