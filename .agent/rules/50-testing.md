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

### Test Naming Convention
- Format: `test_<behavior>_<scenario>` (e.g., `test_rejects_order_when_halt`)
- Forbidden patterns: `test_covers_*`, `test_line_*`, `test_cov_*` — tests must describe behavior, not coverage targets
- Every test MUST contain at least one `assert` statement. "Must not raise" tests should assert a postcondition (e.g., state unchanged, no side effects).
- Advisory zero-assert threshold: 30 (enforced by `make test-assertion-check`)

### Test Sleep Discipline
- Prefer `threading.Event`, `asyncio.Event`, or polling helpers over fixed `time.sleep()` / `asyncio.sleep()` in tests.
- If sleep is unavoidable, keep duration ≤ 50ms. Document why event-based waiting is not possible.
- Use `_wait_processed()` or similar poll-with-timeout helpers for thread-based workers.
