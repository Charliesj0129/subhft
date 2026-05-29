# Enforcement

Fail-closed gates:

- Pre-commit: `ruff`, `ruff-format`, `hft-discipline`.
- Discipline: silent exception swallowing, production pytest imports, broker SDK leakage, bad import boundaries, hot-path time/pandas/requests/print.
- Architecture: `make dependency-boundary`.
- Security/static: Semgrep, CodeQL.
- CI: format, lint, discipline, dependency-boundary, typecheck, coverage.

Run `make discipline` for HIGH/CRITICAL checks; `make discipline-strict` includes WARNING. New discipline IDs use `HFT-{D|A|P|S}{NNN}`.
