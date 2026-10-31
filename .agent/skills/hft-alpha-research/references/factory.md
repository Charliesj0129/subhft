# Factory reference

| # | Stage | Where | Command |
|---|---|---|---|
| 1 | Generator: paper intake | `research/knowledge/` | arXiv MCP tools; `make research-fetch-paper ARXIV=...`; `make research-paper-prototype PAPER_REF=...` |
| 2 | Prototype | `research/alphas/<id>/` | `uv run python -m research scaffold <id>` |
| 3 | Data | `research/data/` | `make research-gen-synth-lob`, `make research-validate-data-meta` |
| 4 | Gates A-C (backtest, statistics, parameter robustness) | evidence root `validations/`, `runs/` | `uv run hft alpha validate --alpha-id <id> --profile vm_ul6_strict_v2 --data <path>` |
| 5 | Gates D-E (promotion, paper trade) | evidence root `promotions/` | `make research-record-paper`, `research-summarize-paper`, `research-check-paper-governance`; `uv run hft alpha promote --alpha-id <id> --owner <you>` |
| 6 | Canary, shadow, live | promotion YAML, `config/loops/` | user-driven; replay-parity obligation at Shadow exit; live registry frozen |

There is no Rust stage: Rust is paused and `vm_ul6_strict_v2` reports Gate F as
NOT_APPLICABLE. Gate checks, thresholds and the verdict vocabulary are in the
handbook, `docs/runbooks/alpha-development-workflow.md` (sections 2, 6 and 7).

| Gate | Checks |
|---|---|
| A | manifest, data fields, causality, complexity; strict: paper linkage, data governance metadata |
| B | per-alpha pytest; strict: coverage threshold |
| C | backtest scorecard and sub-gates (20 blocking under `vm_ul6_strict_v2`); strict: tightened latency and cost |
| D | Sharpe, drawdown, turnover, correlation, latency profile, equity source, replay-parity audit |
| E | shadow sessions, execution quality, paper governance report |
| F | not applicable while Rust is paused |

Stage 7: record each shadow session (`--trading-day`, start/end, reject rates,
regime), summarize, then run the strict governance check. Minimum sessions come
from the profile (`min_shadow_sessions`).

Artifacts: source `research/alphas/<id>/` (`signal.py`, `manifest.yaml`, `README.md`,
`tests/`); governed validations, runs and promotions under `HFT_RESEARCH_EVIDENCE_ROOT`
(`validations/<id>/<stamp>/`, `runs/<run_id>/`, `promotions/<id>/<stamp>/`,
`ledger/verdicts.jsonl`); `research/experiments/**` is frozen legacy evidence; promotion configs under the
`strategy_promotions` directory in `config/` (created by `hft alpha promote`); summaries `outputs/research_pipeline/`; audit
tables `audit.alpha_*` in ClickHouse.

Batch and maintenance (via `make help` / `uv run hft alpha ...`):
`research-batch-correlation`, `research-paper-trade-batch`, `research-promote-batch`,
`research-hypothesis-ingest`, `research-hypothesis-top`, `research-auto-scaffold`,
`experiment-gc` (dry-run by default; `APPLY=1` deletes `*.npy`/`*.npz` payloads > 90 days,
keeps latest 3; run metadata is never deleted).

Related references: `docs/runbooks/alpha-development-workflow.md`,
`docs/runbooks/alpha-lifecycle-state.md`, `docs/runbooks/replay-parity-gate.md`,
`docs/runbooks/maker-realism-gate.md`.
