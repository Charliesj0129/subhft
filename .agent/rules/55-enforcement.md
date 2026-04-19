# Enforcement Rules

Automated enforcement gates that block violations from entering the codebase.

## Pre-commit Hooks

Pre-commit runs on every commit via `.pre-commit-config.yaml`:

| Hook | Tool | What it catches |
|------|------|-----------------|
| `ruff` | Ruff | Lint violations (E/F/I/W/BLE/T20/UP/SIM) |
| `ruff-format` | Ruff | Formatting violations |
| `hft-discipline` | `scripts/check_discipline.py` | HFT-specific anti-patterns |

Install: `pre-commit install`

## Governance Gates

Repo-level governance is versioned through:

- `importlinter.ini` + `make dependency-boundary` for import-layer contracts
- `semgrep/rules/` + `.github/workflows/semgrep.yml` for anti-pattern/security gates
- `.github/workflows/codeql.yml` for deep code scanning on Python
- `.github/CODEOWNERS` for high-risk path ownership
- `merge_group` triggers in governance workflows for merge queue compatibility

## Discipline Check Rules

`scripts/check_discipline.py` enforces platform-specific invariants:

| Rule ID | Severity | Description |
|---------|----------|-------------|
| HFT-D001 | CRITICAL | `except Exception: pass` (silent exception swallowing) |
| HFT-D002 | CRITICAL | `"pytest" in sys.modules` in production code |
| HFT-D003 | WARNING | Broad `except Exception` without re-raise (WARNING if logged) |
| HFT-A001 | HIGH | Broker SDK imports outside `feed_adapter/<broker>/` |
| HFT-A002 | HIGH | `contracts` importing runtime services |
| HFT-A003 | HIGH | `events.py` importing strategy/execution |
| HFT-P001 | HIGH | `datetime.now()`/`time.time()` on hot path |
| HFT-P002 | HIGH | `import pandas` on hot path |
| HFT-P003 | HIGH | `requests.get/post` on hot path |

### Running

```bash
make discipline          # Default: fail on HIGH/CRITICAL
make discipline-strict   # Strict: fail on WARNING+
```

### Adding New Rules

1. Add check function in `scripts/check_discipline.py`
2. Assign rule ID following convention: `HFT-{D|A|P|S}{NNN}`
3. CI runs `make discipline` on every push

## Coverage Honesty Policy

Previously excluded files have been restored to coverage tracking:
- `normalizer.py`, `bootstrap.py`, `system.py` (control plane)
- `gateway.py`, `positions.py`, `router.py` (execution plane)
- `recorder/loader.py`, `recorder/writer.py` (persistence plane)

Current honest baseline: 55%. Ratchet schedule:

| Quarter | Target | Notes |
|---------|--------|-------|
| Q2 2026 | 55% | Honest baseline established |
| Q3 2026 | 65% | Focus on risk + execution tests |
| Q4 2026 | 75% | Full hot-path coverage |
| Q1 2027 | 80% | Maintenance mode |

## Ruff Rule Expansion Roadmap

Currently selected but ignored (to be enabled after cleanup):
- `BLE001` — Blind except (~650 instances to fix)
- `T201` — Print statements (~19 files to migrate to structlog)

Target: Enable `BLE001` by end of Q3 2026, `T201` by end of Q2 2026.

## CI Enforcement

CI (`make ci`) runs: `format-check` + `lint` + `discipline` + `dependency-boundary` + `typecheck` + `coverage`

All gates are fail-closed: any violation blocks the pipeline.
