# Testing Rules

- `make test` for quick regression; `make test-all` for unit+integration.
- All `feat:` and `fix:` commits MUST include unit tests.

## Coverage Roadmap

| Milestone | Line | Branch | Target    |
|-----------|------|--------|-----------|
| **Current** | **70%** | **55%** | now     |
| Phase 2   | 80%  | 65%    | +2 months |
| Phase 3   | 90%  | 75%    | +4 months |

### Coverage Rules
1. New code ≥80% line coverage.
2. Hot-path (`normalizer`, `lob_engine`, `risk`) ≥90%.
3. Regressions blocked by CI (`--cov-fail-under=70`).
4. Branch coverage enforced separately (`--fail-under=55`).

### What to Test
- **Always**: business logic, financial calcs, risk checks.
- **Prefer**: edge cases (empty books, one-sided quotes, zero prices).
- **Skip**: pure logging, third-party wrappers, CLI glue.

### Test Naming
- Format: `test_<behavior>_<scenario>` (e.g., `test_rejects_order_when_halt`).
- Forbidden: `test_covers_*`, `test_line_*`, `test_cov_*` — describe behavior, not coverage targets.
- Every test MUST have ≥1 `assert`. Advisory zero-assert threshold: 30 (`make test-assertion-check`).

### Test Sleep Discipline
- Prefer `threading.Event`, `asyncio.Event`, or polling helpers over fixed `time.sleep()`.
- If sleep unavoidable: ≤50ms, document why event-based waiting isn't possible.
