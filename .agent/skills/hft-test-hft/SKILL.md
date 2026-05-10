---
name: hft-test-hft
description: Use when writing tests for HFT platform components. Covers HFT-specific patterns — scaled int assertions, monotonic time validation, fail-closed Rust fallback, async queue tests, StormGuard state matrices.
---

# HFT Test Patterns

Domain-specific testing patterns for the HFT platform. Standard pytest patterns apply; this skill covers what's unique to trading systems.

## Test Naming

```python
# REQUIRED format: test_<behavior>_<scenario>
def test_rejects_order_when_halt():        # Good
def test_normalizes_tick_ieee754_rounding(): # Good

# FORBIDDEN:
def test_covers_line_42():                  # Coverage-chasing
def test_cov_risk():                        # No behavior description
```

Every test MUST contain at least one `assert` statement.

## Pattern 1: Scaled Integer Assertions

All price assertions must validate scaled int (x10000):

```python
@pytest.mark.parametrize("price_float,expected_scaled", [
    (100.15, 1001500),   # IEEE 754: float(100.15)*10000 rounds to 1001500
    (100.05, 1000500),
    (0.1, 1000),         # Edge: 0.1*10000 = 999.999... → round → 1000
])
def test_normalize_tick_price_scaling(normalizer, price_float, expected_scaled):
    event = normalizer.normalize_tick(make_raw_tick(price=price_float))
    assert event.price == expected_scaled
    assert isinstance(event.price, int)  # Never float
```

**Rule**: Never `assert abs(price - expected) < epsilon`. Prices are exact integers.

## Pattern 2: Monotonic Time Validation

```python
EPOCH_THRESHOLD_NS = 100_000_000_000_000_000  # ~3 years from epoch

def test_order_deadline_is_monotonic(adapter):
    intent = make_intent(ttl_ns=5_000_000_000)  # 5s TTL
    before = time.monotonic_ns()
    cmd = adapter.to_command(intent)
    assert cmd.deadline_ns < EPOCH_THRESHOLD_NS, "must be monotonic, not epoch"
    assert cmd.deadline_ns >= before + intent.ttl_ns - 1_000_000  # 1ms tolerance
```

**Rule**: Use `time.monotonic_ns()` in assertions, never `time.time()` or `datetime.now()`.

## Pattern 3: Fail-Closed Rust Fallback

When Rust acceleration fails, Python path must still work:

```python
def test_rust_validator_fail_closed(engine):
    """Rust error → fall through to Python validators (not crash)."""
    mock_rv = MagicMock()
    mock_rv.check.side_effect = RuntimeError("segfault simulation")
    engine._rust_validator = mock_rv

    decision = engine.evaluate(make_intent())
    assert decision.approved is True  # Python fallback succeeded
```

**Rule**: Test both `Rust enabled` and `Rust disabled` paths for any hot-path component.

## Pattern 4: Async Queue Drain Tests

```python
@pytest.mark.asyncio
async def test_risk_engine_drains_queue(engine):
    engine.intent_queue.put_nowait(make_intent())
    task = asyncio.create_task(engine.run())

    await asyncio.sleep(0.05)  # Yield to let reactor drain
    assert not engine.order_queue.empty()

    engine.running = False
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task
```

**Rule**: Use `asyncio.sleep()` sparingly (max 50ms). Prefer `threading.Event` or `asyncio.Event` for synchronization.

## Pattern 5: StormGuard State Matrix

Exhaustive FSM coverage via parametrized matrix:

```python
_MATRIX = [
    # (current_state, intent_type, expected_approved)
    (StormGuardState.NORMAL, IntentType.NEW, True),
    (StormGuardState.NORMAL, IntentType.CANCEL, True),
    (StormGuardState.HALT, IntentType.NEW, False),     # HALT blocks new
    (StormGuardState.HALT, IntentType.CANCEL, True),   # HALT allows cancel
    (StormGuardState.DEGRADE, IntentType.NEW, True),   # Degrade still allows
    # ... all state x intent combinations
]

@pytest.mark.parametrize("state,intent_type,expected_ok", _MATRIX)
def test_stormguard_matrix(state, intent_type, expected_ok):
    fsm = make_fsm(initial_state=state)
    decision = fsm.validate(make_intent(intent_type=intent_type))
    assert decision.approved == expected_ok
```

## Pattern 6: Isolation Fixtures

Standard isolation (autouse in `tests/unit/conftest.py`):

```python
@pytest.fixture(autouse=True)
def _disable_clickhouse(monkeypatch):
    monkeypatch.setenv("HFT_CLICKHOUSE_ENABLED", "0")

@pytest.fixture(autouse=True)
def _disable_live_monitor(monkeypatch):
    monkeypatch.setenv("HFT_MONITOR_LIVE_ENABLED", "0")

@pytest.fixture
def normalizer(monkeypatch):
    monkeypatch.setenv("HFT_RUST_ACCEL", "0")
    monkeypatch.setenv("HFT_FUSED_NORMALIZER", "0")
    # ... construct with mocked metrics
```

**Rule**: Disable external systems by default. Opt-in for integration tests via `@pytest.mark.integration`.

## Pattern 7: Config-Driven Tests

Use real YAML config, not mocked config objects:

```python
@pytest.fixture
def engine(tmp_path):
    cfg = tmp_path / "risk.yaml"
    cfg.write_text(textwrap.dedent("""\
        risk:
          max_order_size: 10
          max_position: 20
          max_notional: 1000000
    """))
    return RiskEngine(str(cfg), asyncio.Queue(), asyncio.Queue())
```

**Rule**: Real config parsing exercises schema validation. Never mock the config loader.

## Factory Fixtures

Use the project's standard factories from `tests/conftest.py`:

```python
# Available factories:
make_order_intent(price=1001500, qty=1, side=Side.BUY, ...)
make_fill_event(price=1001500, qty=1, fee=20, tax=0, ...)
make_order_command(cmd_id=1, deadline_ns=..., ...)
make_tick_event(price=1001500, volume=100, ...)
make_bidask_event(bids=np.array(...), asks=np.array(...), ...)
```

All factory prices default to scaled int (x10000). Symbol default: `"2330"`.

## Coverage Rules

| Module category | Minimum coverage |
|-----------------|-----------------|
| Hot-path (`normalizer`, `lob_engine`, `risk`) | 90% |
| New code (any) | 80% |
| Overall project | 70% (CI gate) |

```bash
make coverage                        # Full coverage report
make test-file FILE=tests/unit/test_risk_engine.py  # Single file
make test-assertion-check            # Verify all tests have asserts
make test-name-check                 # Verify behavior-oriented names
```

## Anti-Patterns

- Do NOT use `time.sleep()` > 50ms in tests (use Event-based synchronization)
- Do NOT test coverage targets — test behaviors
- Do NOT skip Rust fallback tests when modifying hot-path code
- Do NOT use `float` assertions for price values (`assert price == 100.15` is WRONG)
- Do NOT create tests without `assert` statements (advisory limit: 30 zero-assert tests)
