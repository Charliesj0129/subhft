# Enforcement Rules

Automated gates that block violations. Fail-closed.

## Pre-commit (`.pre-commit-config.yaml`)

Install: `pre-commit install`. Hooks: `ruff` (E/F/I/W/BLE/T20/UP/SIM), `ruff-format`, `hft-discipline`.

## Governance Gates

- `importlinter.ini` + `make dependency-boundary` — import-layer contracts
- `semgrep/rules/` + `.github/workflows/semgrep.yml` — anti-pattern/security
- `.github/workflows/codeql.yml` — deep code scan
- `.github/CODEOWNERS` — high-risk path ownership
- `merge_group` triggers for merge queue compatibility

## Discipline Rules (`scripts/check_discipline.py`)

| Rule | Severity | Catches |
|------|----------|---------|
| HFT-D001 | CRITICAL | `except Exception: pass` (silent swallow) |
| HFT-D002 | CRITICAL | `"pytest" in sys.modules` in production |
| HFT-D003 | WARNING | Broad `except Exception` without re-raise |
| HFT-A001 | HIGH | Broker SDK import outside `feed_adapter/<broker>/` |
| HFT-A002 | HIGH | `contracts` importing runtime services |
| HFT-A003 | HIGH | `events.py` importing strategy/execution |
| HFT-P001 | HIGH | `datetime.now()`/`time.time()` on hot path |
| HFT-P002 | HIGH | `import pandas` on hot path |
| HFT-P003 | HIGH | `requests.get/post` on hot path |

Run: `make discipline` (HIGH/CRITICAL) / `make discipline-strict` (WARNING+). New rule ID convention: `HFT-{D|A|P|S}{NNN}`.

## Coverage Ratchet

Honest baseline 55%. Targets: Q2 2026=55%, Q3=65%, Q4=75%, Q1 2027=80%.

## Ruff Expansion

Selected-but-ignored (enable after cleanup): `BLE001` (~650 instances, target end Q3 2026), `T201` (~19 files, end Q2 2026).

## CI (`make ci`)

`format-check` + `lint` + `discipline` + `dependency-boundary` + `typecheck` + `coverage`. Any violation blocks merge.
