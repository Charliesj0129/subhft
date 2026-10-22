# Factory reference

| # | Stage | Folder | Command |
|---|---|---|---|
| 1 | Paper intake | `research/knowledge/` | arXiv MCP tools; `make research-fetch-paper ARXIV=...`; `make research-paper-prototype PAPER_REF=...` |
| 2 | Prototype | `research/alphas/<id>/` | `uv run python -m research scaffold <id>` |
| 3 | Data | `research/data/` | `make research-gen-synth-lob`, `make research-validate-data-meta` |
| 4 | Backtest | `research/backtest/` | `uv run python -m research.factory run-gate-c ...` |
| 5 | Statistical validation | `research/experiments/validations/` | `uv run hft alpha validate <id>` |
| 6 | Parameter optimization | `research/experiments/runs/` | `uv run python -m research.factory optimize` |
| 7 | Paper trade | `research/experiments/promotions/` | `make research-record-paper`, `research-summarize-paper`, `research-check-paper-governance` |
| 8 | Live (Rust) | `rust_core/src/` | `uv run hft alpha promote --alpha-id <id> --owner <you>` (user-driven; frozen registry) |

Gate mapping (thresholds come from the validation profile, for example
`config/research/profiles/vm_ul6_strict.yaml`):

| Gate | Checks |
|---|---|
| A | manifest, data fields, causality, complexity; strict: paper linkage, data governance metadata |
| B | per-alpha pytest; strict: coverage threshold |
| C | backtest metrics, statistical significance, stress, parameter robustness; strict: tightened latency and cost |
| D | Sharpe, drawdown, turnover, correlation thresholds; feature-set parity |
| E | shadow sessions, execution quality, paper governance report |
| F | Rust readiness (optional): manifest, parity tests, optional benchmark gate |

Stage 7: record each shadow session (`--trading-day`, start/end, reject rates,
regime), summarize, then run the strict governance check. Minimum sessions come
from the profile (`min_shadow_sessions`).

Artifacts: source `research/alphas/<id>/` (`signal.py`, `manifest.yaml`, `README.md`,
`tests/`); validations `research/experiments/validations/<id>/<stamp>/`; runs
`research/experiments/runs/<run_id>/`; promotions
`research/experiments/promotions/<id>/<stamp>/`; promotion configs under the
`strategy_promotions` directory in `config/` (created by `hft alpha promote`); summaries `outputs/research_pipeline/`; audit
tables `audit.alpha_*` in ClickHouse.

Batch and maintenance (via `make help` / `uv run hft alpha ...`):
`research-batch-correlation`, `research-paper-trade-batch`, `research-promote-batch`,
`research-hypothesis-ingest`, `research-hypothesis-top`, `research-auto-scaffold`,
`experiment-gc` (artifacts > 90 days, keeps latest 3).

Related references: `docs/runbooks/alpha-factory.md`,
`docs/runbooks/alpha-lifecycle-state.md`, `docs/runbooks/replay-parity-gate.md`,
`docs/runbooks/maker-realism-gate.md`.
