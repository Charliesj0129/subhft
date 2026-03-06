# Research SOP (Paper to Live Factory)

This SOP codifies one strict factory lane from paper idea to live deployment.

## Single Entrance

```bash
make research ALPHA=<alpha_id> OWNER=<owner> DATA='<path1.npy [path2.npy ...]>' [ARGS='...']
```

The entrance runs:

1. `factory optimize` preflight (`init -> converge-tools -> clean -> audit(strict)`)
2. Gate A/B/C validation
3. Gate D/E promotion
4. `factory index` finalization

### UL6 profile entry (higher bar)

```bash
make research ALPHA=<alpha_id> OWNER=<owner> DATA='<path.npy>' ARGS='--validation-profile vm_ul6'
```

`vm_ul6` enforces stricter Gate C/D/E/F defaults (stress, promotion, paper-trade), and
requires dataset provenance metadata keys (`source/generator/seed/...`) in strict Gate A.

## Target Pipeline (8 Stages)

```
論文(MCP) → 基礎原型(Python) → 資料 → 回測(延遲+成本) → 因子有效(統計) → 參數優化 → Paper trade(1w) → Live(Rust)
```

### Stage 1: 論文 Paper Intake (MCP)

- **Folder**: `research/knowledge/`
- **Role**: `planner` — scope, data plan, acceptance criteria
- **Skill**: `iterative-retrieval` — paper and code context retrieval before implementation
- **Checklist**: Read `.agent/agents/planner.md`, use `iterative-retrieval` SKILL.md
- **MCP Server**: `arxiv` — 已透過 project root `.mcp.json` 激活 (Claude Code 自動載入)
- **Installation** (一次性，若 `arxiv-mcp-server` 尚未安裝):
  ```bash
  uv tool install arxiv-mcp-server
  # 或: pip install arxiv-mcp-server
  ```
- **Storage path**: `~/.arxiv-mcp-server/papers`。覆蓋方式: 編輯 `.mcp.json` 的 args。
- **MCP Tools available**: `search_papers`, `download_paper`, `read_paper`
- **Fallback**: `python -m research fetch-paper <arxiv_id>` (fetch_paper.py 直接呼叫 arXiv HTTP API，與 MCP 無關)

### Stage 2: 基礎原型 Python Prototype

- **Folder**: `research/alphas/<alpha_id>/`
- **Role**: `architect` — gate criteria, latency/cost assumptions, promotion boundaries
- **Skill**: `iterative-retrieval`, `hft-backtester`
- **Checklist**: Read `.agent/agents/architect.md`, scaffold with `research/tools/alpha_scaffold.py`
- **Enforced Bridge**:
  - `python -m research paper-to-prototype <paper_ref> [--alpha-id ...]`
  - Gate A(strict) requires `manifest.paper_refs` and paper_index linkage.

### Stage 3: 資料 Data

- **Folder**: `research/data/` (`raw/`, `interim/`, `processed/`, `models/`)
- **Role**: `planner`
- **Skill**: `hft-backtester` (ingestion section)
- **Checklist**: Read `.agent/skills/hft-backtester/SKILL.md` §Data Ingestion
- **Recommended synthetic lane (UL-governed metadata)**:
  - `make research-gen-synth-lob OUT=research/data/processed/<alpha_id>/synthetic_lob_v2_train.npy ARGS='--version v2 --rng-seed 42 --symbols TXF,MXF --split train'`
  - `make research-validate-data-meta DATA_PATH=research/data/processed/<alpha_id>/synthetic_lob_v2_train.npy`
- **Enforced Governance**:
  - each `.npy/.npz` dataset under `research/data/{raw,interim,processed}` must have sidecar metadata
  - generate metadata: `python -m research stamp-data-meta <dataset.npy>`
  - validate metadata: `python -m research validate-data-meta <dataset.npy>`
  - Gate A(strict) enforces allowed data roots + metadata contract.

### Stage 4: 回測 Backtest (Latency + Cost)

- **Folder**: `research/backtest/`
- **Role**: `architect`
- **Skill**: `hft-backtester`, `validation-gate`
- **Checklist**: Read `.agent/skills/hft-backtester/SKILL.md`, `.agent/skills/validation-gate/SKILL.md`

### Stage 5: 因子有效 Statistical Validation

- **Folder**: `research/experiments/validations/`
- **Role**: `code-reviewer` — regression, governance bypass, operational risk checks
- **Skill**: `validation-gate` — mandatory gate checks before acceptance/promotion
- **Checklist**: Read `.agent/agents/code-reviewer.md`

### Stage 6: 參數優化 Parameter Optimization

- **Folder**: `research/experiments/runs/`
- **Role**: `architect`
- **Skill**: `hft-backtester`
- **Checklist**: Anti-overfit trap detection, robustness sweep

### Stage 7: Paper Trade (1 Week)

- **Folder**: `research/experiments/promotions/`
- **Role**: `code-reviewer`
- **Skill**: `paper_trader` — Digital Twin simulation
- **Checklist**: Read `skills/paper_trader/SKILL.md`, min 5 shadow sessions (Gate E)
- **Operational loop**:
  - record each session: `make research-record-paper ALPHA=<alpha_id> ARGS='--trading-day 2026-03-05 --started-at 2026-03-05T01:00:00+00:00 --ended-at 2026-03-05T03:00:00+00:00 --execution-reject-rate 0.004 --reject-rate-p95 0.008 --regime trending'`
  - summarize sessions: `make research-summarize-paper ALPHA=<alpha_id> ARGS='--out outputs/paper_summary_<alpha_id>.json'`
  - pre-check Gate E governance: `make research-check-paper-governance ALPHA=<alpha_id> ARGS='--strict --out outputs/paper_governance_<alpha_id>.json'`
  - promotion artifact linkage: `hft alpha promote` 會在 `research/experiments/promotions/<alpha_id>/<stamp>/` 自動輸出 `paper_governance_report.json`

### Stage 8: Live Promotion (Rust)

- **Folder**: `rust_core/src/`
- **Role**: `architect`
- **Skill**: `rust_feature_engineering`
- **Checklist**: Read `.agent/skills/rust_feature_engineering/SKILL.md`, profile Python baseline first

## Roles (Execution Contract)

| Role               | Agent File                          | Responsibility                                                |
| ------------------ | ----------------------------------- | ------------------------------------------------------------- |
| `planner`          | `.agent/agents/planner.md`          | scope, data plan, acceptance criteria                         |
| `architect`        | `.agent/agents/architect.md`        | gate criteria, latency/cost assumptions, promotion boundaries |
| `refactor-cleaner` | `.agent/agents/refactor-cleaner.md` | folder hygiene, tool stratification, contract enforcement     |
| `code-reviewer`    | `.agent/agents/code-reviewer.md`    | regression, governance bypass, operational risk checks        |

## Skills (Execution Contract)

| Skill                      | SKILL.md Location                                 | Usage                            |
| -------------------------- | ------------------------------------------------- | -------------------------------- |
| `iterative-retrieval`      | `.agent/skills/iterative-retrieval/SKILL.md`      | paper and code context retrieval |
| `validation-gate`          | `.agent/skills/validation-gate/SKILL.md`          | mandatory gate checks            |
| `hft-backtester`           | `.agent/skills/hft-backtester/SKILL.md`           | tick-level backtest simulation   |
| `paper_trader`             | `skills/paper_trader/SKILL.md`                    | Digital Twin paper trading       |
| `rust_feature_engineering` | `.agent/skills/rust_feature_engineering/SKILL.md` | Rust alpha optimization          |

## Gate Mapping

- Gate A: data/causality/complexity feasibility
  - strict extension: paper linkage + data governance metadata
- Gate B: correctness tests
- Gate C: backtest metrics + statistical significance + stress resilience + parameter robustness
- Gate D: portfolio integration thresholds
- Gate E: shadow-paper trade readiness (`min_shadow_sessions=5` default)

## Artifact Policy

- Never write reports under `research/alphas/<alpha_id>/`
- Validation outputs: `research/experiments/validations/<alpha_id>/<stamp>/`
- Run outputs: `research/experiments/runs/<run_id>/`
- Promotion outputs: `research/experiments/promotions/<alpha_id>/<stamp>/`
- Pipeline summaries: `outputs/research_pipeline/`

## Internal Debug Mode (Non-Official)

```bash
export HFT_RESEARCH_ALLOW_TRIAGE=1
make research-triage ALPHA=<alpha_id> OWNER=<owner> DATA='<path.npy>' ARGS='--skip-gate-b-tests --no-promote'
```

`triage` output is always non-promotable.
