# Testing

- `make test` for quick regression; `make test-all` for broader suite; `make ci` before merge-level confidence.
- `feat:`/`fix:` require focused tests. New code target: >=80% line coverage; hot path target: >=90%.
- Test business logic, financial calcs, risk gates, edge books, one-sided quotes, zero prices, fail-closed behavior, monotonic time, scaled ints, and state transitions.
- Test names describe behavior: `test_<behavior>_<scenario>`. No `test_covers_*`.
- Every test has assertions.
- Avoid fixed sleeps; prefer events/polling. If unavoidable, <=50 ms and explain why.
- Do not claim passing tests without command output.
