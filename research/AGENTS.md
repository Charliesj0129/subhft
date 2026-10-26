# research/ — governed alpha research

Project-wide rules: `../AGENTS.md`. Process and stages: `README.md`. Only the
differences here.

- Research ClickHouse raw prices are **x1,000,000**; platform event prices are
  x10000. Convert explicitly; option strikes use a different scale again.
  Details: `.agent/rules/70-research-data.md`.
- Book depth is variable (0-5). Filter depth-N features on `length(bids_price)`.
- `research/experiments/**` is immutable evidence: append new runs, never edit
  existing artifacts.
- Strict runs (`make research`) are governed: they need `HFT_RESEARCH_EVIDENCE_ROOT`
  (absolute, outside the repo), a clean tree and a resolved profile, write to
  that root, seal each run with a `manifest.json` and append to
  `ledger/verdicts.jsonl` (`src/hft_platform/alpha/evidence.py`). Promotion of a
  strict run is governed too (sealed scorecard in, `promotions/` and ledger events
  out). Sealed directories are never rewritten. Triage runs are exploratory and
  never reach the ledger.
- Verdicts are faithful: KILL / NEEDS-MORE-DAYS / INCONCLUSIVE. Never relax a
  pre-registered floor or gate to improve an outcome; name the backtest method
  behind every PnL claim; check recent data before claiming an edge holds.
- Float is allowed here for offline metrics only. Nothing in `research/`
  enables live trading; promotion goes through Gates A-F and the frozen
  registry rules in `../AGENTS.md`.
- `research/` is excluded from the platform ruff and pytest gates. Its own gate
  is `make research-check` (bug-class ruff, `tests/research`,
  `tests/unit/research`, the alpha `tests/`, a coverage floor); a test that
  cannot run goes in `tests/research/quarantine.yaml` with its reason.
