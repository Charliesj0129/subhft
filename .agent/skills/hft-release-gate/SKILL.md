---
name: hft-release-gate
description: Use when checking if the current state is ready to deploy, before going live with a strategy, or when performing release readiness checks. Unifies all quality gates into a single pass/fail checklist.
---

# HFT Release Gate

Unified release readiness check combining all quality, safety, and operational gates.

## When to Use

- Before deploying to production (Docker Compose update)
- Before enabling a new strategy for live trading
- Before `git push` to main with runtime changes
- Weekly/monthly release confidence check

## Gate Summary

| # | Gate | Command | Pass criteria |
|---|------|---------|---------------|
| 1 | Code Quality | `make check` | lint + typecheck + discipline + dep-boundary all green |
| 2 | Test Suite | `make coverage` | 70%+ line coverage, 55%+ branch |
| 3 | Test Hygiene | `make test-hygiene-check` | Naming, assertions, quality patterns |
| 4 | Architecture | `make arch-gate` | No forbidden imports, no boundary violations |
| 5 | Security | `make security-audit` | No known CVEs in dependencies |
| 6 | Pre-Market | `make pre-market-check` | Docker, ClickHouse, Redis, WAL, metrics healthy |
| 7 | Latency | `make hotpath-profile` | No regression vs baseline |

## Full Gate Sequence

### Gate 1: Code Quality

```bash
make check
# Runs: format-check → lint → typecheck → discipline → dependency-boundary → test-hygiene-check
```

Must pass with **zero errors**. Key discipline rules:
- HFT-D001: No silent exception swallow
- HFT-A001: No broker SDK imports outside adapter
- HFT-P001: No `datetime.now()` on hot path

### Gate 2: Test Coverage

```bash
make coverage
# Enforces: --cov-fail-under=70 --cov-branch
```

Check per-module coverage for hot-path files:
```bash
make coverage-html
# Open htmlcov/index.html, verify:
# - normalizer.py: ≥90%
# - lob_engine.py: ≥90%
# - risk/engine.py: ≥90%
```

### Gate 3: Test Hygiene

```bash
make test-assertion-check    # All tests have asserts
make test-name-check         # Behavior-oriented names (not test_covers_*)
make test-quality-pattern-check  # No tautological patterns
```

### Gate 4: Architecture Conformance

```bash
make arch-gate
make dependency-boundary
```

Verifies:
- No `contracts` importing runtime services
- No `events.py` importing strategy/execution
- No broker-specific code outside `feed_adapter/<broker>/`

### Gate 5: Security

```bash
make security-audit
# pip-audit or pip check fallback
```

Check manually:
- [ ] No hardcoded secrets in diff (`grep -r "API_KEY\|SECRET\|PASSWORD" src/`)
- [ ] `.env` is in `.gitignore`
- [ ] No new `# type: ignore` without justification

### Gate 6: Pre-Market Health

```bash
make pre-market-check
```

Verifies:
- Docker services healthy (`docker compose ps`)
- ClickHouse responds (`SELECT 1`)
- Redis responds (`redis-cli ping`)
- WAL directory clean (no orphan `.tmp` files)
- Prometheus metrics endpoint live
- Health endpoint returns 200

### Gate 7: Latency Regression

```bash
make hotpath-profile
# Compare against baseline:
make benchmark-compare
```

No stage should regress by more than 20% vs baseline.

## Strategy-Specific Gates (for live trading enablement)

Additional checks when enabling `HFT_ORDER_MODE=live`:

| # | Check | How |
|---|-------|-----|
| 8 | Shadow session reviewed | At least 1 full trading day in shadow mode |
| 9 | Latency profile documented | Entry in `config/research/latency_profiles.yaml` |
| 10 | Max position conservative | `max_pos=1` for first live day |
| 11 | Canary config exists | `config/strategy_promotions/YYYYMMDD/<alpha>.yaml` |
| 12 | Rollback plan documented | Can disable via config change + restart |

## Drift Detection

```bash
make deploy-drift-snapshot    # Capture baseline
# ... deploy changes ...
make deploy-drift-check       # Compare against baseline
```

## Quick One-Shot

For the impatient (runs all automated gates):

```bash
make ci && make pre-market-check && make hotpath-profile
```

## Output

Report gate results in release notes:

```
Release Readiness: 7/7 gates PASS
- Code Quality: PASS (0 errors)
- Coverage: PASS (72.3% line, 58.1% branch)
- Test Hygiene: PASS (0 zero-assert, 0 bad names)
- Architecture: PASS (0 boundary violations)
- Security: PASS (0 known CVEs)
- Pre-Market: PASS (all services healthy)
- Latency: PASS (no regression > 20%)
```

## Anti-Patterns

- Do NOT skip Gate 6 (pre-market) for "code-only" changes — config drift happens
- Do NOT rely on CI alone — run `make pre-market-check` on the actual deployment host
- Do NOT go live without shadow session data for new strategies
- Do NOT release on Friday (Taiwan market opens Monday 09:00 — no recovery window)
