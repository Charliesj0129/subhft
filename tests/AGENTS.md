# tests/ — test suites

Project-wide rules (including "Done means"): `../AGENTS.md`. Only the differences here.

- Run one file or node with `make test-file FILE=...` / `make test-node NODE=...`
  (no coverage gate); `make test` is the quick unit suite.
- Unit tests never touch the network or a real broker; mock (`pytest-mock`,
  `respx`) or use local containers. Restore any global state you change.
- Async tests use `pytest-asyncio`. No fixed sleeps: wait on an event or poll
  (<= 50 ms only if unavoidable, with a comment).
- Cover the HFT edges: scaled ints, monotonic time, fail-closed paths, state
  transitions, one-sided and zero-price books.
- A test that passes is not proof it can fail: break the behavior (or revert
  the fix) and confirm the test goes red; a test that passes against the bug is
  worse than none.
- `tests/golden/**` and the Shioaji surface goldens are contracts; regenerate
  only deliberately and with justification.
- Patching `time.sleep` or `os.environ` can leak across tests; scope it to the
  module under test and use `monkeypatch`.
- `make test-hygiene-check` must stay clean (assertions present, behavior names).
