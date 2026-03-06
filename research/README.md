# Research Factory

`research/` is the governed pipeline workspace for alpha development.
Official process is intentionally reduced to one entrance and one promotable flow.

## Factor Factory Pipeline (8 Stages)

```
論文(MCP) → 基礎原型(Python) → 資料 → 回測(延遲+成本) → 因子有效(統計) → 參數優化 → Paper trade(1w) → Live(Rust)
```

| #   | Stage                  | Folder                     | Role            | Skill                               |
| --- | ---------------------- | -------------------------- | --------------- | ----------------------------------- |
| 1   | 論文 (Paper Intake)    | `knowledge/`               | `planner`       | `iterative-retrieval`, `mcporter`   |
| 2   | 基礎原型 (Prototype)   | `alphas/<id>/`             | `architect`     | `hft-architect`, `python-pro`       |
| 3   | 資料 (Data)            | `data/`                    | `planner`       | `hft-backtester` (ingestion)        |
| 4   | 回測 (Backtest)        | `backtest/`                | `architect`     | `hft-backtester`, `validation-gate` |
| 5   | 因子有效 (Statistical) | `experiments/validations/` | `code-reviewer` | `validation-gate`                   |
| 6   | 參數優化 (Param Opt)   | `experiments/runs/`        | `architect`     | `hft-backtester`                    |
| 7   | Paper Trade            | `experiments/promotions/`  | `code-reviewer` | `paper_trader`                      |
| 8   | Live (Rust)            | `rust_core/src/`           | `architect`     | `rust_feature_engineering`          |

## Paper -> Prototype Bridge

```bash
make research-paper-prototype PAPER_REF=120 ARGS='--alpha-id ofi_p120 --complexity O1'
```

This command scaffolds `research/alphas/<alpha_id>/` and writes reverse mapping into
`research/knowledge/paper_index.json` (`alphas` field), making paper-to-prototype traceable.

## Data Governance (Enforced in Strict Gate A)

```bash
make research-stamp-data-meta DATA_PATH=research/data/raw/example.npy ARGS='--source-type real --owner charlie --symbols 2330'
make research-validate-data-meta DATA_PATH=research/data/raw/example.npy
```

Strict Gate A enforces:
- dataset path must be under allowed roots
- dataset must carry metadata sidecar (`.meta.json` / `.metadata.json`)
- metadata contract keys and row-count consistency

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

This command runs strict `research.pipeline run`, which now includes factory optimize preflight by default.

### VM-UL6 profile (institutional/near-live strictness)

```bash
make research ALPHA=<alpha_id> OWNER=<owner> DATA='<path.npy>' ARGS='--validation-profile vm_ul6'
```

`vm_ul6` tightens latency/cost assumptions, stress multipliers, promotion thresholds,
paper-trade requirements, Rust benchmark gate, and data provenance metadata requirements.

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

## Internal Debug Path

`triage` remains available only for internal debugging and is disabled by default:

```bash
export HFT_RESEARCH_ALLOW_TRIAGE=1
make research-triage ALPHA=<alpha_id> OWNER=<owner> DATA='<path.npy>' ARGS='--skip-gate-b-tests --no-promote'
```

Outputs from `triage` are always non-promotable.

## Paper-Trade Governance Fast Path

```bash
make research-record-paper ALPHA=queue_imbalance ARGS='--trading-day 2026-03-05 --execution-reject-rate 0.004 --reject-rate-p95 0.008 --regime trending'
make research-summarize-paper ALPHA=queue_imbalance ARGS='--out outputs/paper_summary_queue_imbalance.json'
make research-check-paper-governance ALPHA=queue_imbalance ARGS='--strict --out outputs/paper_governance_queue_imbalance.json'
```

Use `research-check-paper-governance` before promotion to fail fast on Gate E readiness.
During `hft alpha promote` / `research.pipeline run`, promotion artifacts now also include
`paper_governance_report.json` under `research/experiments/promotions/<alpha_id>/<stamp>/`.

## Governance Rules

- Keep `paper_refs` mapped in `research/knowledge/paper_index.json`
- Keep each paper-backed alpha linked via `paper-to-prototype` (`paper_index.json` -> `alphas`)
- Keep governed dataset metadata sidecars with each `.npy/.npz` file
- Keep binary artifacts out of source zones (`alphas/`, `registry/`, `tools/`, `backtest/`)
- Keep generated artifacts in `research/experiments/` or `outputs/`
- Do not place runnable scripts at `research/` root
