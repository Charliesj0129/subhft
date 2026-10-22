---
name: hft-test-hft
description: "HFT-specific test patterns: scaled-int assertions, monotonic time, Rust fail-closed fallback, queue-drain async tests, StormGuard state matrix, isolation fixtures, factories. Use when writing or reviewing platform tests. Not for generic pytest usage or research code."
---

# HFT test patterns

Naming, coverage targets, no fixed sleeps, and break-probes are in `AGENTS.md`
and `tests/AGENTS.md`. Patterns specific to this platform:

1. **Scaled ints are exact.** Assert `event.price == 1001500` and
   `isinstance(event.price, int)`; never an epsilon. Parametrize float inputs
   that stress rounding (`100.15 -> 1001500`, `0.1 -> 1000`).
2. **Monotonic time.** Compare against `time.monotonic_ns()`; assert a deadline
   is below an epoch threshold (`100_000_000_000_000_000`) to catch epoch leaks.
3. **Fail-closed Rust fallback.** Make the Rust validator or kernel raise and
   assert the Python path still decides correctly. Test both Rust-enabled and
   Rust-disabled for any hot-path component (`HFT_RUST_ACCEL=0`,
   `HFT_FUSED_NORMALIZER=0`).
4. **Queue drain.** Put an item, run the task, wait on an `asyncio.Event` or
   poll (<= 50 ms), assert the downstream queue, then cancel and await the task.
5. **StormGuard matrix.** `StormGuardState` is `NORMAL / WARM / STORM / HALT`.
   Parametrize state x intent type: HALT blocks `NEW` but allows `CANCEL`.
6. **Isolation.** `tests/unit/conftest.py` autouse fixtures disable ClickHouse and
   the live monitor; opt in with `@pytest.mark.integration`. Real YAML config
   in `tmp_path`, never a mocked config loader.
7. **Factories.** `tests/conftest.py` and `tests/factories/` provide
   `make_order_intent`, `make_fill_event`, `make_order_command`,
   `make_tick_event`, `make_bidask_event`; prices default to scaled ints and the
   default symbol is `"2330"`.

## Run

```bash
make test-file FILE=tests/unit/test_risk_engine.py
make test-assertion-check    # every test asserts
make test-name-check         # behavior-oriented names
make coverage
```

## Anti-patterns

Float assertions on prices; `time.time()` in assertions; fixed sleeps over
50 ms; skipping the Rust-fallback test on a hot-path change; tests that pass
against the bug (revert the fix and demand the failure); zero-assert tests.

## Done when

The tests assert scaled-int, monotonic-time, and fail-closed behavior where they
apply, fail when the guarded behavior is broken, and `make test-hygiene-check`
is clean.
