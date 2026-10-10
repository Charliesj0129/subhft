# research/ — governed alpha research

Project-wide rules: `../AGENTS.md`. Process and stages: `README.md`. Only the
differences here.

- Research ClickHouse raw prices are **x1,000,000**; platform event prices are
  x10000. Convert explicitly; option strikes use a different scale again.
  Details: `.agent/rules/70-research-data.md`.
- Book depth is variable (0-5). Filter depth-N features on `length(bids_price)`.
- `research/experiments/**` is immutable evidence: append new runs, never edit
  existing artifacts.
- Verdicts are faithful: KILL / NEEDS-MORE-DAYS / INCONCLUSIVE. Never relax a
  pre-registered floor or gate to improve an outcome; name the backtest method
  behind every PnL claim; check recent data before claiming an edge holds.
- Float is allowed here for offline metrics only. Nothing in `research/`
  enables live trading; promotion goes through Gates A-F and the frozen
  registry rules in `../AGENTS.md`.
- `research/` is excluded from the ruff and pytest gates, so run the relevant
  `make research-*` targets and the gate tooling yourself.
