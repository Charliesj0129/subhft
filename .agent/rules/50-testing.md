# Testing Rules

- Run `make test` for quick regression.
- Add tests for new logic where feasible.
- All new `feat:` and `fix:` commits MUST include corresponding unit tests.

## Coverage Roadmap

| Milestone | Line Coverage | Branch Coverage | Target Date |
|-----------|--------------|-----------------|-------------|
| **Current** | **70%** | **55%** | Now |
| Phase 2 | 80% | 65% | +2 months |
| Phase 3 | 90% | 75% | +4 months |

### Coverage Rules
1. New code MUST have ≥80% line coverage.
2. Hot-path code (`normalizer`, `lob_engine`, `risk`) MUST have ≥90% coverage.
3. Coverage regressions are blocked by CI (`--cov-fail-under=70`).
4. Branch coverage is enforced separately (`--fail-under=55`).

### What to Test
- **Always**: Business logic, financial calculations, risk checks.
- **Prefer**: Edge cases (empty books, one-sided quotes, zero prices).
- **Skip**: Pure logging, third-party wrappers, CLI glue code.
