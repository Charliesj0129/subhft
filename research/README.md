# Research Factory — Operational Handbook

`research/` is the governed pipeline workspace for alpha development.
Official process is intentionally reduced to **one entrance and one promotable flow**.

This file is the **canonical operational handbook** for the factory (entrance, 8 stages,
folder layout, data governance, paper-trade fast path, triage). For the gate/promotion
contract and profile semantics, the canonical source is
[`docs/runbooks/alpha-development-workflow.md`](../docs/runbooks/alpha-development-workflow.md).

> `research/SOP.md` is now a thin pointer to this file — do not duplicate process detail there.

## Canonical docs (read before acting)

| Topic | Canonical source |
| --- | --- |
| Lifecycle + Gates A–F + profile semantics | `docs/runbooks/alpha-development-workflow.md` |
| Factory operations (this handbook) | `research/README.md` |
| Replay-parity gate (why / enable / schema / fail-closed) | `docs/runbooks/replay-parity-gate.md` |
| Research data source + L1/L2 formats | `.agent/rules/70-research-data.md` |
| Governed L2+tick export contract | `docs/runbooks/research-data-pipeline.md` |
| Constrained hypothesis ideation | `.agent/teams/alpha-research/factor-ideation-pipeline.md` |

## Factor Factory Pipeline (8 Stages)

```
論文(MCP) → 基礎原型(Python) → 資料 → 回測(延遲+成本) → 因子有效(統計) → 參數優化 → Paper trade(1w) → Live(Rust)
```

| #   | Stage                  | Folder                     | Role            | Skill / Tooling                     | Checklist |
| --- | ---------------------- | -------------------------- | --------------- | ----------------------------------- | --------- |
| 1   | 論文 (Paper Intake)    | `knowledge/`               | `planner`       | `iterative-retrieval` + `arxiv` MCP | scope, data plan, acceptance criteria; `make research-fetch-paper ARXIV=...` |
| 2   | 基礎原型 (Prototype)   | `alphas/<id>/`             | `architect`     | `hft-architect`, `python-pro`       | scaffold via `research/tools/alpha_scaffold.py`; gate/latency/cost assumptions |
| 3   | 資料 (Data)            | `data/`                    | `planner`       | `hft-backtest-engine` (ingestion)        | dataset under allowed roots + metadata sidecar |
| 4   | 回測 (Backtest)        | `backtest/`                | `architect`     | `hft-backtest-engine`, `hft-backtest-validation`, `validation-gate` | engine auto-selected from `manifest.strategy_type` |
| 5   | 因子有效 (Statistical) | `experiments/validations/` | `code-reviewer` | `validation-gate`                   | regression, governance-bypass, operational-risk checks |
| 6   | 參數優化 (Param Opt)   | `experiments/runs/`        | `architect`     | `hft-backtest-engine`                    | anti-overfit trap detection, robustness sweep |
| 7   | Paper Trade            | `experiments/promotions/`  | `code-reviewer` | Gate-E paper-trade `make` targets   | min 5 shadow sessions (Gate E `min_shadow_sessions=5`) |
| 8   | Live (Rust)            | `rust_core/src/`           | `architect`     | `rust_feature_engineering`          | profile Python baseline before porting |

Roles map to agent definitions under `.agent/agents/` (`planner.md`, `architect.md`,
`code-reviewer.md`, `refactor-cleaner.md`). Skills are under `.agent/skills/<name>/SKILL.md`.

### Stage-4 engine selection (automatic, via `manifest.yaml`)

- `strategy_type: taker` → `TakerEngine` (wraps `HftNativeRunner`, `PowerProbQueueModel`)
- `strategy_type: maker` → `MakerEngine` (CK-direct, `QueueDepletionFill(qf=0.5)`)
- Cost model: `config/research/cost_profiles.yaml`; Gate C thresholds: `config/research/profiles/vm_ul6_strict.yaml`.
- MakerEngine needs local ClickHouse: `docker compose up -d clickhouse`.
- Results auto-saved by `ResultStore` to `research/experiments/runs/<run_id>/backtest_report.json`.
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
`docs/runbooks/research-data-pipeline.md` — do not reimplement the dtype/sidecar/validation rules.

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

### VM-UL6 profile (institutional/near-live strictness)

```bash
make research ALPHA=<alpha_id> OWNER=<owner> DATA='<path.npy>' ARGS='--validation-profile vm_ul6'
```

`vm_ul6` tightens latency/cost assumptions, stress multipliers, promotion thresholds,
paper-trade requirements, the Rust benchmark gate, and data provenance metadata requirements.

> **Profile token differs by entrypoint.** The `make research` / `research.pipeline` entrance
> uses `--validation-profile vm_ul6` (override token), while `hft alpha validate|promote` uses
> `--profile vm_ul6_strict` (file-resolved). These are not typos of each other — see the
> **Profile Reference** in `docs/runbooks/alpha-development-workflow.md`.

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
