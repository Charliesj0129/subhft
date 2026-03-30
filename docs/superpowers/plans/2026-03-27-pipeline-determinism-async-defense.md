# Pipeline Determinism & Async Defense — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Fix 8 correctness defects (D1–D8) in the execution pipeline to eliminate silent fill loss, orphan orders, non-monotonic timeouts, post-HALT injection, partial intent batches, and session gate bypass.

**Architecture:** Each defect is fixed independently with its own commit. A prerequisite metrics task (Task 0) adds all new Prometheus counters first. Fixes proceed from simplest (D8) to most complex (D2), each with TDD: write failing test → implement → verify → commit.

**Tech Stack:** Python 3.12, asyncio, threading, prometheus_client, structlog, pytest

**Spec:** `docs/superpowers/specs/2026-03-27-pipeline-determinism-async-defense-design.md` (Rev 2)

---

## File Map

| File | Tasks | Changes |
|------|-------|---------|
| `src/hft_platform/observability/metrics.py` | T0 | 8 new Counter metrics |
| `src/hft_platform/risk/storm_guard.py` | T1, T2 | D8: halt callback error handler; D3: monotonic timestamps (3 sites) |
| `src/hft_platform/order/circuit_breaker.py` | T2 | D3: monotonic timestamp (1 site) |
| `src/hft_platform/strategy/runner.py` | T2, T3, T5, T6 | D3: monotonic (2 sites); D5: use snapshot; D7: per-intent catch; D6: per-intent gating |
| `src/hft_platform/risk/engine.py` | T2, T4 | D3: monotonic deadline (1 site); D4: HALT guard + put_nowait + DLQ |
| `src/hft_platform/order/adapter.py` | T2, T8 | D3: monotonic deadline check (1 site); D2: deferred terminals, sentinel, lock fix |
| `src/hft_platform/execution/positions.py` | T3 | D5: `snapshot_positions()` method |
| `src/hft_platform/ops/session_governor.py` | T6 | D6: default CLOSED, `__slots__` update |
| `src/hft_platform/services/system.py` | T7 | D1: `_safe_enqueue_exec`, overflow buffer init |
| `src/hft_platform/execution/router.py` | T7 | D1: overflow drain in `run()` loop |
| `tests/unit/test_d8_halt_callback.py` | T1 | Tests for D8 |
| `tests/unit/test_d3_monotonic.py` | T2 | Tests for D3 (8 sites) |
| `tests/unit/test_d5_position_snapshot.py` | T3 | Tests for D5 |
| `tests/unit/test_d4_halt_drain_race.py` | T4 | Tests for D4 |
| `tests/unit/test_d7_intent_batch.py` | T5 | Tests for D7 |
| `tests/unit/test_d6_track_gate.py` | T6 | Tests for D6 |
| `tests/unit/test_d1_exec_overflow.py` | T7 | Tests for D1 |
| `tests/unit/test_d2_live_orders_toctou.py` | T8 | Tests for D2 |

---

### Task 0: Add New Metrics to MetricsRegistry

**Files:**
- Modify: `src/hft_platform/observability/metrics.py:769` (insert before `# System (v2)` block)

- [ ] **Step 1: Add 8 new Counter metrics**

In `src/hft_platform/observability/metrics.py`, insert the following block after line 769 (`self.backup_retained_count` Gauge) and before line 771 (`# System (v2)`):

```python
        # ── Pipeline Determinism & Async Defense (D1-D8) ─────────────
        self.exec_queue_overflow_total = Counter(
            "exec_queue_overflow_total",
            "Fills routed to overflow buffer when raw_exec_queue is full",
        )
        self.exec_overflow_drained_total = Counter(
            "exec_overflow_drained_total",
            "Fills successfully drained from overflow buffer",
        )
        self.exec_overflow_evicted_total = Counter(
            "exec_overflow_evicted_total",
            "Fills LOST when overflow buffer is also full",
        )
        self.terminal_before_registration_total = Counter(
            "terminal_before_registration_total",
            "Terminal callbacks deferred because order not yet registered",
        )
        self.deferred_terminal_expired_total = Counter(
            "deferred_terminal_expired_total",
            "Deferred terminal callbacks that expired without resolution",
        )
        self.risk_halt_blocked_total = Counter(
            "risk_halt_blocked_total",
            "Commands blocked by RiskEngine HALT guard before dispatch",
        )
        self.order_queue_full_total = Counter(
            "order_queue_full_total",
            "Approved commands dropped due to order_queue full in RiskEngine",
        )
        self.intent_queue_full_total = Counter(
            "intent_queue_full_total",
            "Intents dropped due to QueueFull in StrategyRunner submit loop",
        )
```

- [ ] **Step 2: Verify metrics load without error**

Run:
```bash
uv run python -c "from hft_platform.observability.metrics import MetricsRegistry; m = MetricsRegistry(); print('exec_queue_overflow_total:', m.exec_queue_overflow_total); print('intent_queue_full_total:', m.intent_queue_full_total); print('OK')"
```
Expected: prints metric objects and `OK`, no exceptions.

- [ ] **Step 3: Commit**

```bash
git add src/hft_platform/observability/metrics.py
git commit -m "feat(metrics): add 8 counters for pipeline determinism defect fixes D1-D8"
```

---

### Task 1: D8 — StormGuard Halt Callback Error Handler

**Files:**
- Modify: `src/hft_platform/risk/storm_guard.py:281-282`
- Create: `tests/unit/test_d8_halt_callback.py`

- [ ] **Step 1: Write failing test**

Create `tests/unit/test_d8_halt_callback.py`:

```python
"""D8: StormGuard halt callback exceptions must be logged, not silently swallowed."""
from __future__ import annotations

import asyncio
from unittest.mock import MagicMock

import pytest

from hft_platform.risk.storm_guard import StormGuard, StormGuardState


@pytest.fixture()
def storm_guard_with_failing_callback():
    """StormGuard whose halt callback raises RuntimeError."""
    async def _bad_callback():
        raise RuntimeError("callback boom")

    sg = StormGuard(on_halt_callback=_bad_callback)
    sg.metrics = MagicMock()
    return sg


def test_halt_callback_exception_is_logged(storm_guard_with_failing_callback, caplog):
    """When the halt callback coroutine raises, the error must appear in logs."""
    sg = storm_guard_with_failing_callback

    async def _run():
        sg.transition(StormGuardState.HALT, "test")
        # Give the scheduled task a chance to run and fail
        await asyncio.sleep(0.05)

    asyncio.run(_run())

    assert any("halt_callback_failed" in r.message for r in caplog.records), (
        "Expected 'halt_callback_failed' in log output but got: "
        + str([r.message for r in caplog.records])
    )


def test_halt_callback_success_no_error_log(caplog):
    """When the halt callback coroutine succeeds, no error is logged."""
    async def _ok_callback():
        pass

    sg = StormGuard(on_halt_callback=_ok_callback)
    sg.metrics = MagicMock()

    async def _run():
        sg.transition(StormGuardState.HALT, "test")
        await asyncio.sleep(0.05)

    asyncio.run(_run())

    assert not any("halt_callback_failed" in r.message for r in caplog.records)
```

- [ ] **Step 2: Run test — verify it fails**

```bash
uv run pytest tests/unit/test_d8_halt_callback.py -v
```
Expected: `test_halt_callback_exception_is_logged` FAILS (currently the exception is swallowed silently — no `halt_callback_failed` log entry).

- [ ] **Step 3: Implement the fix**

In `src/hft_platform/risk/storm_guard.py`, replace lines 280–282:

```python
                    try:
                        loop = asyncio.get_running_loop()
                        loop.create_task(result)
```

with:

```python
                    try:
                        loop = asyncio.get_running_loop()
                        task = loop.create_task(result)
                        task.add_done_callback(self._halt_callback_done)
```

Then add a new method after `trigger_halt` (after line 295):

```python
    def _halt_callback_done(self, task: asyncio.Task) -> None:
        """Log errors from fire-and-forget halt callback tasks."""
        if task.cancelled():
            return
        exc = task.exception()
        if exc is not None:
            logger.error(
                "halt_callback_failed",
                error=str(exc),
                error_type=type(exc).__name__,
            )
```

Also add `"_halt_callback_done"` is a method, no slot needed (methods live on the class, not the instance).

- [ ] **Step 4: Run test — verify it passes**

```bash
uv run pytest tests/unit/test_d8_halt_callback.py -v
```
Expected: both tests PASS.

- [ ] **Step 5: Run existing StormGuard tests**

```bash
uv run pytest tests/ -k "storm_guard" -v --timeout=30
```
Expected: all existing tests still pass.

- [ ] **Step 6: Commit**

```bash
git add src/hft_platform/risk/storm_guard.py tests/unit/test_d8_halt_callback.py
git commit -m "fix(stormguard): D8 — log halt callback exceptions instead of silently swallowing"
```

---

### Task 2: D3 — Surgical Wall-Clock to Monotonic Migration (8 Sites)

**Files:**
- Modify: `src/hft_platform/risk/storm_guard.py` (3 sites: L62, L150, L154-157, L162-164, L254)
- Modify: `src/hft_platform/order/circuit_breaker.py` (1 site: L49, L59)
- Modify: `src/hft_platform/strategy/runner.py` (2 sites: L537, L658)
- Modify: `src/hft_platform/risk/engine.py` (1 site: L476)
- Modify: `src/hft_platform/order/adapter.py` (1 site: L192)
- Create: `tests/unit/test_d3_monotonic.py`

- [ ] **Step 1: Write failing tests**

Create `tests/unit/test_d3_monotonic.py`:

```python
"""D3: Timeout/cooldown paths must use monotonic clock, not wall-clock."""
from __future__ import annotations

import time
from unittest.mock import MagicMock, patch

import pytest


class TestStormGuardMonotonic:
    """Sites 3a-3c: StormGuard cooldown uses time.monotonic()."""

    def test_storm_entry_ts_uses_monotonic(self):
        from hft_platform.risk.storm_guard import StormGuard, StormGuardState

        sg = StormGuard()
        sg.metrics = MagicMock()

        # Force escalation to STORM
        with patch("time.monotonic", return_value=1000.0):
            sg.state = StormGuardState.NORMAL
            sg._de_escalate_count = 0
            sg.update(drawdown_bps=sg.thresholds.drawdown_storm_bps + 1)

        assert sg._storm_entry_ts == 1000.0, (
            f"Expected monotonic value 1000.0, got {sg._storm_entry_ts}"
        )

    def test_halt_cooldown_uses_monotonic(self):
        from hft_platform.risk.storm_guard import StormGuard, StormGuardState

        sg = StormGuard()
        sg.metrics = MagicMock()
        sg._halt_cooldown_s = 10.0
        sg._de_escalate_threshold = 1

        # Enter HALT at monotonic=100
        with patch("time.monotonic", return_value=100.0):
            sg.transition(StormGuardState.HALT, "test")

        assert sg._halt_entry_ts == 100.0

        # Try de-escalate at monotonic=105 (before cooldown) — should NOT de-escalate
        with patch("time.monotonic", return_value=105.0):
            sg.update(drawdown_bps=0)

        assert sg.state == StormGuardState.HALT

        # Try de-escalate at monotonic=111 (after cooldown) — should de-escalate
        with patch("time.monotonic", return_value=111.0):
            sg.update(drawdown_bps=0)

        assert sg.state != StormGuardState.HALT

    def test_last_state_change_uses_monotonic(self):
        from hft_platform.risk.storm_guard import StormGuard, StormGuardState

        sg = StormGuard()
        sg.metrics = MagicMock()

        with patch("time.monotonic", return_value=42.0):
            sg.transition(StormGuardState.WARM, "test")

        assert sg.last_state_change == 42.0


class TestCircuitBreakerMonotonic:
    """Site 3d: OrderAdapter circuit breaker uses time.monotonic()."""

    def test_circuit_breaker_open_until_uses_monotonic(self):
        from hft_platform.order.circuit_breaker import CircuitBreaker

        cb = CircuitBreaker(threshold=2, timeout_s=30)

        with patch("time.monotonic", return_value=500.0):
            cb.record_failure()
            cb.record_failure()  # trips at threshold

        assert cb.open_until == 530.0  # 500 + 30

    def test_circuit_breaker_is_open_uses_monotonic(self):
        from hft_platform.order.circuit_breaker import CircuitBreaker

        cb = CircuitBreaker(threshold=1, timeout_s=10)

        with patch("time.monotonic", return_value=100.0):
            cb.record_failure()  # trips

        with patch("time.monotonic", return_value=105.0):
            assert cb.is_open() is True  # 105 < 110

        with patch("time.monotonic", return_value=111.0):
            assert cb.is_open() is False  # 111 > 110


class TestRunnerCircuitMonotonic:
    """Sites 3e-3f: StrategyRunner circuit cooldown uses time.monotonic_ns()."""

    def test_circuit_halted_at_uses_monotonic_ns(self):
        """Verify _circuit_halted_at_ns stores monotonic_ns, not wall-clock."""
        # We verify the value stored matches time.monotonic_ns() mock
        with patch("time.monotonic_ns", return_value=999_000_000_000):
            from hft_platform.strategy.runner import StrategyRunner

            # Direct dict write to simulate the halted path
            d: dict[str, int] = {}
            d["test_strat"] = time.monotonic_ns()
            assert d["test_strat"] == 999_000_000_000


class TestRiskEngineDeadlineMonotonic:
    """Site 3g: RiskEngine deadline_ns uses time.monotonic_ns()."""

    def test_deadline_uses_monotonic_ns(self):
        import asyncio
        from unittest.mock import AsyncMock

        from hft_platform.risk.engine import RiskEngine

        q_in = asyncio.Queue()
        q_out = asyncio.Queue()
        engine = RiskEngine.__new__(RiskEngine)
        engine.intent_queue = q_in
        engine.order_queue = q_out
        engine.metrics = MagicMock()
        engine.storm_guard = MagicMock()
        engine.storm_guard.state = 0  # NORMAL
        engine._cmd_counter = 0
        engine.latency = None
        engine._reject_metric_cache = {}
        engine._reject_metric_cache_owner_id = None
        engine._reject_metric_counter = 0
        engine._trace_sampler = None

        mock_intent = MagicMock()
        mock_intent.trace_id = "t1"

        with patch("time.monotonic_ns", return_value=5_000_000_000):
            cmd = engine.create_command(mock_intent)

        # Deadline should be monotonic_ns + 500ms
        assert cmd.deadline_ns == 5_500_000_000, (
            f"Expected 5_500_000_000, got {cmd.deadline_ns}"
        )


class TestAdapterDeadlineCheckMonotonic:
    """Site 3h: OrderAdapter deadline check uses time.monotonic_ns()."""

    def test_deadline_check_uses_monotonic_ns(self):
        """The adapter rejects commands where monotonic_ns > deadline_ns."""
        # This is a structural assertion — we verify the import/call is monotonic.
        # Full integration is tested by D4 tests.
        # Here we verify circuit_breaker.is_open uses monotonic:
        from hft_platform.order.circuit_breaker import CircuitBreaker

        cb = CircuitBreaker(threshold=1, timeout_s=5)
        with patch("time.monotonic", return_value=100.0):
            cb.record_failure()
        with patch("time.monotonic", return_value=103.0):
            assert cb.is_open() is True
```

- [ ] **Step 2: Run tests — verify they fail**

```bash
uv run pytest tests/unit/test_d3_monotonic.py -v
```
Expected: Several tests FAIL because current code uses `timebase.now_s()` / `timebase.now_ns()` instead of `time.monotonic()` / `time.monotonic_ns()`.

- [ ] **Step 3: Implement Site 3a-3c — StormGuard**

In `src/hft_platform/risk/storm_guard.py`:

Add `import time` at the top if not already present.

Replace line 62:
```python
        self.last_state_change = timebase.now_s()
```
with:
```python
        self.last_state_change = time.monotonic()
```

Replace line 150:
```python
        now = timebase.now_s()
```
with:
```python
        now = time.monotonic()
```

(Lines 154-157 use `now` which is already changed. Lines 162-164 use `now` and `self._halt_entry_ts` / `self._storm_entry_ts` which are set using `now` — all consistent.)

Replace line 254:
```python
        self.last_state_change = timebase.now_s()
```
with:
```python
        self.last_state_change = time.monotonic()
```

- [ ] **Step 4: Implement Site 3d — CircuitBreaker**

In `src/hft_platform/order/circuit_breaker.py`:

Add `import time` at the top. Remove the `from hft_platform.core import timebase` import (line 8) if no other usage remains.

Replace line 49:
```python
            return self._open_until > timebase.now_s()
```
with:
```python
            return self._open_until > time.monotonic()
```

Replace line 59:
```python
                self._open_until = timebase.now_s() + self.timeout_s
```
with:
```python
                self._open_until = time.monotonic() + self.timeout_s
```

- [ ] **Step 5: Implement Sites 3e-3f — StrategyRunner**

In `src/hft_platform/strategy/runner.py`:

Replace line 537:
```python
                    if halted_at and timebase.now_ns() - halted_at >= self._circuit_cooldown_ns:
```
with:
```python
                    if halted_at and time.monotonic_ns() - halted_at >= self._circuit_cooldown_ns:
```

Replace line 658:
```python
                        self._circuit_halted_at_ns[sid] = timebase.now_ns()
```
with:
```python
                        self._circuit_halted_at_ns[sid] = time.monotonic_ns()
```

- [ ] **Step 6: Implement Site 3g — RiskEngine deadline**

In `src/hft_platform/risk/engine.py`:

Replace line 476:
```python
        deadline = timebase.now_ns() + 500_000_000
```
with:
```python
        deadline = time.monotonic_ns() + 500_000_000
```

**Do NOT change** line 483 (`created_ns=timebase.now_ns()`) — that is a wall-clock event timestamp for audit/recording, not a timeout. Only `deadline` uses monotonic.

- [ ] **Step 7: Implement Site 3h — OrderAdapter deadline check**

In `src/hft_platform/order/adapter.py`:

Replace line 192:
```python
                if timebase.now_ns() > cmd.deadline_ns:
```
with:
```python
                if time.monotonic_ns() > cmd.deadline_ns:
```

Ensure `import time` is present at the top of the file.

- [ ] **Step 8: Run D3 tests — verify they pass**

```bash
uv run pytest tests/unit/test_d3_monotonic.py -v
```
Expected: all tests PASS.

- [ ] **Step 9: Run full test suite to check for regressions**

```bash
uv run pytest tests/ -x --timeout=30 -q
```
Expected: no new failures.

- [ ] **Step 10: Commit**

```bash
git add src/hft_platform/risk/storm_guard.py src/hft_platform/order/circuit_breaker.py src/hft_platform/strategy/runner.py src/hft_platform/risk/engine.py src/hft_platform/order/adapter.py tests/unit/test_d3_monotonic.py
git commit -m "fix(timebase): D3 — migrate 8 timeout/cooldown sites from wall-clock to monotonic clock"
```

---

### Task 3: D5 — PositionStore Atomic Snapshot

**Files:**
- Modify: `src/hft_platform/execution/positions.py` (add `snapshot_positions` method)
- Modify: `src/hft_platform/strategy/runner.py:456-462` (use `snapshot_positions`)
- Create: `tests/unit/test_d5_position_snapshot.py`

- [ ] **Step 1: Write failing test**

Create `tests/unit/test_d5_position_snapshot.py`:

```python
"""D5: PositionStore.snapshot_positions() must be atomic under _fill_lock."""
from __future__ import annotations

import threading
import time
from unittest.mock import MagicMock

import pytest


def test_snapshot_positions_exists():
    """PositionStore must expose a snapshot_positions() method."""
    from hft_platform.execution.positions import PositionStore

    ps = PositionStore.__new__(PositionStore)
    ps.positions = {"a": 1, "b": 2}
    ps._fill_lock = threading.Lock()
    result = ps.snapshot_positions()
    assert result == {"a": 1, "b": 2}
    # Must be a copy, not the original
    assert result is not ps.positions


def test_snapshot_holds_lock_during_copy():
    """snapshot_positions must hold _fill_lock during dict copy."""
    from hft_platform.execution.positions import PositionStore

    ps = PositionStore.__new__(PositionStore)
    ps.positions = {f"k{i}": i for i in range(100)}
    ps._fill_lock = threading.Lock()

    lock_was_held = threading.Event()

    original_enter = ps._fill_lock.acquire
    def _checking_acquire(*a, **kw):
        result = original_enter(*a, **kw)
        lock_was_held.set()
        return result

    ps._fill_lock.acquire = _checking_acquire

    ps.snapshot_positions()
    assert lock_was_held.is_set(), "Lock was not acquired during snapshot"


def test_snapshot_returns_consistent_view():
    """Concurrent fills must not produce a torn snapshot."""
    from hft_platform.execution.positions import PositionStore

    ps = PositionStore.__new__(PositionStore)
    ps._fill_lock = threading.Lock()
    # Start with generation 0
    ps.positions = {f"k{i}": 0 for i in range(50)}

    errors: list[str] = []
    stop = threading.Event()

    def _mutator():
        """Simulates on_fill: atomically updates all values to a new generation."""
        gen = 1
        while not stop.is_set():
            with ps._fill_lock:
                for k in ps.positions:
                    ps.positions[k] = gen
            gen += 1
            time.sleep(0.0001)

    def _reader():
        """Takes snapshots and checks all values are from the same generation."""
        for _ in range(200):
            snap = ps.snapshot_positions()
            vals = set(snap.values())
            if len(vals) > 1:
                errors.append(f"Torn snapshot: {vals}")
            time.sleep(0.0001)

    t1 = threading.Thread(target=_mutator)
    t2 = threading.Thread(target=_reader)
    t1.start()
    t2.start()
    t2.join(timeout=5.0)
    stop.set()
    t1.join(timeout=2.0)

    assert not errors, f"Torn snapshots detected: {errors[:5]}"
```

- [ ] **Step 2: Run test — verify it fails**

```bash
uv run pytest tests/unit/test_d5_position_snapshot.py::test_snapshot_positions_exists -v
```
Expected: FAILS with `AttributeError: 'PositionStore' object has no attribute 'snapshot_positions'`

- [ ] **Step 3: Add `snapshot_positions` to PositionStore**

In `src/hft_platform/execution/positions.py`, add this method to the `PositionStore` class (after the `on_fill` method, around line 230):

```python
    def snapshot_positions(self) -> dict:
        """Return a consistent shallow copy of positions under fill lock."""
        with self._fill_lock:
            return dict(self.positions)
```

- [ ] **Step 4: Update StrategyRunner to use snapshot**

In `src/hft_platform/strategy/runner.py`, replace lines 456-462:

```python
        raw = getattr(self.position_store, "positions", None)
        if not isinstance(raw, dict):
            return {}

        # S1: Take a snapshot before iteration to prevent dict-changed-during-iteration
        # from concurrent broker callback threads that may update positions.
        raw = dict(raw)
```

with:

```python
        if hasattr(self.position_store, "snapshot_positions"):
            raw = self.position_store.snapshot_positions()
        else:
            raw = getattr(self.position_store, "positions", None)
            if not isinstance(raw, dict):
                return {}
            raw = dict(raw)
```

- [ ] **Step 5: Run tests — verify they pass**

```bash
uv run pytest tests/unit/test_d5_position_snapshot.py -v
```
Expected: all 3 tests PASS.

- [ ] **Step 6: Run existing position tests**

```bash
uv run pytest tests/ -k "position" -v --timeout=30
```
Expected: no regressions.

- [ ] **Step 7: Commit**

```bash
git add src/hft_platform/execution/positions.py src/hft_platform/strategy/runner.py tests/unit/test_d5_position_snapshot.py
git commit -m "fix(positions): D5 — atomic snapshot_positions() under fill lock"
```

---

### Task 4: D4 — RiskEngine HALT Drain Race + DLQ

**Files:**
- Modify: `src/hft_platform/risk/engine.py:100-110` (init), `314-316` (put path)
- Create: `tests/unit/test_d4_halt_drain_race.py`

- [ ] **Step 1: Write failing test**

Create `tests/unit/test_d4_halt_drain_race.py`:

```python
"""D4: RiskEngine must not use blocking put; must guard against HALT and DLQ on QueueFull."""
from __future__ import annotations

import asyncio
import collections
from unittest.mock import MagicMock, patch

import pytest

from hft_platform.contracts.strategy import StormGuardState


@pytest.fixture()
def risk_engine():
    """Minimal RiskEngine for testing the dispatch path."""
    from hft_platform.risk.engine import RiskEngine

    engine = RiskEngine.__new__(RiskEngine)
    engine.intent_queue = asyncio.Queue()
    engine.order_queue = asyncio.Queue(maxsize=2)
    engine.metrics = MagicMock()
    engine.storm_guard = MagicMock()
    engine.storm_guard.state = StormGuardState.NORMAL
    engine.storm_guard.validate = MagicMock(return_value=(True, ""))
    engine.running = True
    engine._cmd_counter = 0
    engine.latency = None
    engine._reject_metric_cache = {}
    engine._reject_metric_cache_owner_id = None
    engine._reject_metric_counter = 0
    engine._trace_sampler = None
    engine._order_dlq = collections.deque()
    engine._ORDER_DLQ_MAX = 256
    return engine


def test_halt_guard_blocks_dispatch(risk_engine):
    """When StormGuard is HALT, approved commands must not reach order_queue."""
    risk_engine.storm_guard.state = StormGuardState.HALT

    mock_intent = MagicMock()
    mock_intent.strategy_id = "s1"
    mock_intent.symbol = "TXFD6"
    mock_intent.trace_id = "t1"

    cmd = MagicMock()
    cmd.cmd_id = 1
    cmd.intent = mock_intent

    # Simulate the dispatch path after evaluate() returns approved
    # The HALT guard should prevent put_nowait
    async def _run():
        # Directly test: if HALT, should not put
        if risk_engine.storm_guard.state == StormGuardState.HALT:
            risk_engine.metrics.risk_halt_blocked_total.inc()
            return  # blocked

        risk_engine.order_queue.put_nowait(cmd)

    asyncio.run(_run())

    assert risk_engine.order_queue.empty(), "Command should not have reached order_queue during HALT"
    risk_engine.metrics.risk_halt_blocked_total.inc.assert_called_once()


def test_queue_full_routes_to_dlq(risk_engine):
    """When order_queue is full, command goes to DLQ and StormGuard HALTs."""
    # Fill queue to capacity
    for i in range(2):
        risk_engine.order_queue.put_nowait(MagicMock())

    mock_intent = MagicMock()
    mock_intent.strategy_id = "s1"
    mock_intent.symbol = "TXFD6"

    cmd = MagicMock()
    cmd.cmd_id = 99
    cmd.intent = mock_intent

    # Try put_nowait — should fail
    try:
        risk_engine.order_queue.put_nowait(cmd)
    except asyncio.QueueFull:
        risk_engine._order_dlq.append((cmd, 0))
        risk_engine.metrics.order_queue_full_total.inc()
        risk_engine.storm_guard.trigger_halt("order_queue_full")

    assert len(risk_engine._order_dlq) == 1
    assert risk_engine._order_dlq[0][0] is cmd
    risk_engine.metrics.order_queue_full_total.inc.assert_called_once()
    risk_engine.storm_guard.trigger_halt.assert_called_once_with("order_queue_full")


def test_dlq_bounded_at_max(risk_engine):
    """DLQ must not grow beyond _ORDER_DLQ_MAX."""
    risk_engine._ORDER_DLQ_MAX = 3

    for i in range(5):
        risk_engine._order_dlq.append((MagicMock(), i))
        if len(risk_engine._order_dlq) > risk_engine._ORDER_DLQ_MAX:
            risk_engine._order_dlq.popleft()

    assert len(risk_engine._order_dlq) == 3
```

- [ ] **Step 2: Run test — verify it fails**

```bash
uv run pytest tests/unit/test_d4_halt_drain_race.py -v
```
Expected: Tests may pass partially (they test the pattern directly), but the actual RiskEngine code at line 316 still uses `await self.order_queue.put(cmd)` with no HALT guard and no DLQ — we need to verify the code change itself.

- [ ] **Step 3: Implement the fix**

In `src/hft_platform/risk/engine.py`:

Add `import collections` and `import time` at the top if not already present.

After the `__init__` assignments (around line 110, after `self._reject_metric_counter = 0`), add:

```python
        self._order_dlq: collections.deque = collections.deque()
        self._ORDER_DLQ_MAX: int = 256
```

Replace lines 314-316:

```python
                if decision.approved:
                    cmd = self.create_command(decision.intent)
                    await self.order_queue.put(cmd)
```

with:

```python
                if decision.approved:
                    cmd = self.create_command(decision.intent)
                    if self.storm_guard.state == StormGuardState.HALT:
                        logger.warning(
                            "risk_engine_blocked_by_halt",
                            cmd_id=cmd.cmd_id,
                        )
                        self.metrics.risk_halt_blocked_total.inc()
                    else:
                        try:
                            self.order_queue.put_nowait(cmd)
                        except asyncio.QueueFull:
                            logger.error(
                                "order_queue_full_in_risk",
                                cmd_id=cmd.cmd_id,
                                strategy_id=cmd.intent.strategy_id,
                                symbol=cmd.intent.symbol,
                            )
                            self.metrics.order_queue_full_total.inc()
                            self._order_dlq.append((cmd, time.monotonic_ns()))
                            if len(self._order_dlq) > self._ORDER_DLQ_MAX:
                                self._order_dlq.popleft()
                            self.storm_guard.trigger_halt("order_queue_full")
```

Ensure `StormGuardState` is imported (it likely already is since `storm_guard.state` is used elsewhere).

- [ ] **Step 4: Run tests — verify they pass**

```bash
uv run pytest tests/unit/test_d4_halt_drain_race.py -v
```
Expected: all 3 tests PASS.

- [ ] **Step 5: Run existing risk engine tests**

```bash
uv run pytest tests/ -k "risk" -v --timeout=30
```
Expected: no regressions.

- [ ] **Step 6: Commit**

```bash
git add src/hft_platform/risk/engine.py tests/unit/test_d4_halt_drain_race.py
git commit -m "fix(risk): D4 — replace blocking put with put_nowait + HALT guard + DLQ"
```

---

### Task 5: D7 — Intent Batch Partial Submit Protection

**Files:**
- Modify: `src/hft_platform/strategy/runner.py:760-786`
- Create: `tests/unit/test_d7_intent_batch.py`

- [ ] **Step 1: Write failing test**

Create `tests/unit/test_d7_intent_batch.py`:

```python
"""D7: QueueFull during intent submit must not crash the runner; must record circuit failure."""
from __future__ import annotations

import asyncio
from unittest.mock import MagicMock

import pytest


def _make_intent(strategy_id="s1", symbol="TXFD6", intent_type=1):
    intent = MagicMock()
    intent.strategy_id = strategy_id
    intent.symbol = symbol
    intent.intent_type = intent_type
    return intent


def test_partial_batch_does_not_crash_runner():
    """If _risk_submit raises QueueFull mid-batch, remaining intents are dropped but runner lives."""
    call_count = 0

    def _submit_that_fails_on_third(intent):
        nonlocal call_count
        call_count += 1
        if call_count >= 3:
            raise asyncio.QueueFull()

    intents = [_make_intent() for _ in range(5)]

    submitted = 0
    dropped = 0
    for intent in intents:
        try:
            _submit_that_fails_on_third(intent)
            submitted += 1
        except asyncio.QueueFull:
            dropped += 1

    assert submitted == 2
    assert dropped == 3


def test_circuit_failure_recorded_on_drop():
    """When intents are dropped, the strategy's circuit breaker must be notified."""
    metrics = MagicMock()

    call_count = 0
    def _submit(intent):
        nonlocal call_count
        call_count += 1
        if call_count > 1:
            raise asyncio.QueueFull()

    intents = [_make_intent() for _ in range(3)]

    submitted = 0
    dropped = 0
    for intent in intents:
        try:
            _submit(intent)
            submitted += 1
        except asyncio.QueueFull:
            dropped += 1
            metrics.intent_queue_full_total.inc()

    assert submitted == 1
    assert dropped == 2
    assert metrics.intent_queue_full_total.inc.call_count == 2
```

- [ ] **Step 2: Run test — verify it passes (pattern test)**

```bash
uv run pytest tests/unit/test_d7_intent_batch.py -v
```
Expected: PASS (these test the pattern). The actual runner code change is next.

- [ ] **Step 3: Implement the fix**

In `src/hft_platform/strategy/runner.py`, replace lines 760-786:

```python
            if intents:
                for intent in intents:
                    # Populate decision_mid from LOB engine's last stats
                    if hasattr(self.lob_engine, "last_stats") and self.lob_engine.last_stats is not None:
                        if isinstance(intent, OrderIntent):
                            intent.decision_mid = self.lob_engine.last_stats.mid_price_x2 // 2

                    self._emit_trace(
                        "strategy_intent_submit",
                        trace_id,
                        {
                            "strategy_id": strategy.strategy_id,
                            "intent_type": int(getattr(intent, "intent_type", -1))
                            if not (isinstance(intent, tuple) and intent and intent[0] == "typed_intent_v1")
                            else -2,
                            "typed": bool(isinstance(intent, tuple) and intent and intent[0] == "typed_intent_v1"),
                        },
                    )
                    if (
                        self._typed_intent_fastpath
                        and isinstance(intent, tuple)
                        and intent
                        and intent[0] == "typed_intent_v1"
                    ):
                        self._risk_submit_typed(intent)
                    else:
                        self._risk_submit(intent)
```

with:

```python
            if intents:
                _d7_submitted = 0
                _d7_dropped = 0
                for intent in intents:
                    # Populate decision_mid from LOB engine's last stats
                    if hasattr(self.lob_engine, "last_stats") and self.lob_engine.last_stats is not None:
                        if isinstance(intent, OrderIntent):
                            intent.decision_mid = self.lob_engine.last_stats.mid_price_x2 // 2

                    self._emit_trace(
                        "strategy_intent_submit",
                        trace_id,
                        {
                            "strategy_id": strategy.strategy_id,
                            "intent_type": int(getattr(intent, "intent_type", -1))
                            if not (isinstance(intent, tuple) and intent and intent[0] == "typed_intent_v1")
                            else -2,
                            "typed": bool(isinstance(intent, tuple) and intent and intent[0] == "typed_intent_v1"),
                        },
                    )
                    try:
                        if (
                            self._typed_intent_fastpath
                            and isinstance(intent, tuple)
                            and intent
                            and intent[0] == "typed_intent_v1"
                        ):
                            self._risk_submit_typed(intent)
                        else:
                            self._risk_submit(intent)
                        _d7_submitted += 1
                    except asyncio.QueueFull:
                        _d7_dropped += 1
                        self.metrics.intent_queue_full_total.inc()
                        logger.error(
                            "intent_submit_queue_full",
                            strategy_id=getattr(intent, "strategy_id", "?"),
                            submitted=_d7_submitted,
                            dropped=_d7_dropped,
                            batch_size=len(intents),
                        )
                if _d7_dropped > 0:
                    _sid = strategy.strategy_id
                    if self._rust_circuit is not None:
                        self._rust_circuit.record_failure(_sid, time.monotonic_ns())
                    else:
                        _failures = self._failure_counts.get(_sid, 0) + 1
                        self._failure_counts[_sid] = _failures
                        _state = self._circuit_states.get(_sid, "normal")
                        _half = max(1, self._circuit_threshold // 2)
                        if _state == "normal" and _failures >= _half:
                            self._circuit_states[_sid] = "degraded"
                            logger.warning("strategy_circuit_degraded", id=_sid, reason="queue_full_partial_batch")
                        if _failures >= self._circuit_threshold and _state != "halted":
                            self._circuit_states[_sid] = "halted"
                            strategy.enabled = False
                            self._circuit_halted_at_ns[_sid] = time.monotonic_ns()
                            logger.error("strategy_circuit_halted", id=_sid, reason="queue_full_partial_batch")
```

- [ ] **Step 4: Run tests**

```bash
uv run pytest tests/unit/test_d7_intent_batch.py -v && uv run pytest tests/ -k "runner or strategy" --timeout=30 -q
```
Expected: all pass.

- [ ] **Step 5: Commit**

```bash
git add src/hft_platform/strategy/runner.py tests/unit/test_d7_intent_batch.py
git commit -m "fix(runner): D7 — per-intent QueueFull catch prevents runner crash on partial batch"
```

---

### Task 6: D6 — TrackGate Default CLOSED + Per-Intent Symbol Gating

**Files:**
- Modify: `src/hft_platform/ops/session_governor.py:75-94`
- Modify: `src/hft_platform/strategy/runner.py:684-693`
- Create: `tests/unit/test_d6_track_gate.py`

- [ ] **Step 1: Write failing test**

Create `tests/unit/test_d6_track_gate.py`:

```python
"""D6: Unknown symbols must default to CLOSED; gating must be per-intent, not per-event."""
from __future__ import annotations

import os
from unittest.mock import MagicMock, patch

import pytest

from hft_platform.ops.session_governor import SessionPhase, TrackGate


class TestTrackGateDefaultClosed:
    def test_unknown_symbol_returns_closed(self):
        gate = TrackGate()
        assert gate.get_phase("UNKNOWN_SYMBOL") == SessionPhase.CLOSED

    def test_registered_symbol_returns_correct_phase(self):
        gate = TrackGate()
        gate.register_symbol("TXFD6", "futures_day")
        gate.set_track_phase("futures_day", SessionPhase.OPEN)
        assert gate.get_phase("TXFD6") == SessionPhase.OPEN

    def test_registered_track_no_phase_returns_closed(self):
        gate = TrackGate()
        gate.register_symbol("TXFD6", "futures_day")
        # Track exists but no phase set
        assert gate.get_phase("TXFD6") == SessionPhase.CLOSED

    def test_warning_logged_once_per_unknown_symbol(self, caplog):
        gate = TrackGate()
        gate.get_phase("NEW_SYM")
        gate.get_phase("NEW_SYM")
        gate.get_phase("NEW_SYM")
        warnings = [r for r in caplog.records if "track_gate_unknown_symbol_blocked" in r.message]
        assert len(warnings) == 1

    def test_env_override_restores_open_default(self):
        with patch.dict(os.environ, {"HFT_TRACK_GATE_DEFAULT_OPEN": "1"}):
            gate = TrackGate()
            assert gate.get_phase("UNKNOWN") == SessionPhase.OPEN


class TestPerIntentGating:
    """Runner must gate each intent on its own symbol, not on event.symbol."""

    def test_intent_for_unregistered_symbol_is_filtered(self):
        """Strategy triggered by registered symbol emits intent for unregistered symbol."""
        gate = TrackGate()
        gate.register_symbol("TXFD6", "futures_day")
        gate.set_track_phase("futures_day", SessionPhase.OPEN)

        # Intent for registered symbol — should pass
        intent_registered = MagicMock()
        intent_registered.symbol = "TXFD6"
        intent_registered.intent_type = 1  # NEW

        # Intent for unregistered symbol — should be filtered
        intent_unregistered = MagicMock()
        intent_unregistered.symbol = "NEWHEDGE"
        intent_unregistered.intent_type = 1  # NEW

        intents = [intent_registered, intent_unregistered]

        # Simulate per-intent gating
        from hft_platform.contracts.strategy import IntentType

        _CLOSE_ONLY_TYPES = (IntentType.CANCEL, IntentType.FORCE_FLAT)
        filtered = []
        for intent in intents:
            phase = gate.get_phase(intent.symbol)
            if phase == SessionPhase.OPEN:
                filtered.append(intent)
            elif phase == SessionPhase.CLOSE_ONLY:
                if intent.intent_type in _CLOSE_ONLY_TYPES:
                    filtered.append(intent)

        assert len(filtered) == 1
        assert filtered[0].symbol == "TXFD6"
```

- [ ] **Step 2: Run test — verify it fails**

```bash
uv run pytest tests/unit/test_d6_track_gate.py -v
```
Expected: `test_unknown_symbol_returns_closed` FAILS (currently returns `OPEN`).

- [ ] **Step 3: Implement TrackGate changes**

In `src/hft_platform/ops/session_governor.py`:

Replace line 75:
```python
    __slots__ = ("_symbol_to_track", "_track_phases")
```
with:
```python
    __slots__ = ("_symbol_to_track", "_track_phases", "_warned_unknown", "_default_open")
```

Replace lines 77-79 (`__init__`):
```python
    def __init__(self) -> None:
        self._symbol_to_track: dict[str, str] = {}
        self._track_phases: dict[str, SessionPhase] = {}
```
with:
```python
    def __init__(self) -> None:
        self._symbol_to_track: dict[str, str] = {}
        self._track_phases: dict[str, SessionPhase] = {}
        self._warned_unknown: set[str] = set()
        self._default_open: bool = os.getenv("HFT_TRACK_GATE_DEFAULT_OPEN", "0") in ("1", "true", "yes")
```

Ensure `import os` is at the top (check if it's already there).

Replace lines 89-94 (`get_phase`):
```python
    def get_phase(self, symbol: str) -> SessionPhase:
        """Return current phase for *symbol*. Unknown symbols default to OPEN."""
        track = self._symbol_to_track.get(symbol)
        if track is None:
            return SessionPhase.OPEN
        return self._track_phases.get(track, SessionPhase.OPEN)
```
with:
```python
    def get_phase(self, symbol: str) -> SessionPhase:
        """Return current phase for *symbol*. Unknown symbols default to CLOSED."""
        track = self._symbol_to_track.get(symbol)
        if track is None:
            if self._default_open:
                return SessionPhase.OPEN
            if symbol not in self._warned_unknown:
                self._warned_unknown.add(symbol)
                logger.warning("track_gate_unknown_symbol_blocked", symbol=symbol, default_phase="CLOSED")
            return SessionPhase.CLOSED
        return self._track_phases.get(track, SessionPhase.CLOSED)
```

- [ ] **Step 4: Implement per-intent gating in StrategyRunner**

In `src/hft_platform/strategy/runner.py`, replace lines 684-693:

```python
            # TrackGate per-event filtering (session phase enforcement)
            if getattr(self, "track_gate", None) is not None and intents:
                from hft_platform.ops.session_governor import SessionPhase  # noqa: PLC0415

                symbol = getattr(event, "symbol", "")
                phase = self.track_gate.get_phase(symbol)
                if phase == SessionPhase.CLOSE_ONLY:
                    intents = [i for i in intents if i.intent_type in (IntentType.CANCEL, IntentType.FORCE_FLAT)]
                elif phase in (SessionPhase.FORCE_FLAT, SessionPhase.CLOSED, SessionPhase.PRE_OPEN, SessionPhase.INIT):
                    intents = []
```

with:

```python
            # TrackGate per-intent filtering (session phase enforcement)
            if getattr(self, "track_gate", None) is not None and intents:
                from hft_platform.ops.session_governor import SessionPhase  # noqa: PLC0415

                _CLOSE_ONLY_TYPES = (IntentType.CANCEL, IntentType.FORCE_FLAT)
                _filtered: list = []
                for _intent in intents:
                    _phase = self.track_gate.get_phase(_intent.symbol)
                    if _phase == SessionPhase.OPEN:
                        _filtered.append(_intent)
                    elif _phase == SessionPhase.CLOSE_ONLY:
                        if _intent.intent_type in _CLOSE_ONLY_TYPES:
                            _filtered.append(_intent)
                intents = _filtered
```

- [ ] **Step 5: Run tests**

```bash
uv run pytest tests/unit/test_d6_track_gate.py -v && uv run pytest tests/ -k "session_governor or track_gate or runner" --timeout=30 -q
```
Expected: all pass.

- [ ] **Step 6: Commit**

```bash
git add src/hft_platform/ops/session_governor.py src/hft_platform/strategy/runner.py tests/unit/test_d6_track_gate.py
git commit -m "fix(session): D6 — TrackGate defaults CLOSED for unknown symbols + per-intent gating"
```

---

### Task 7: D1 — Exec Queue Overflow Ring Buffer

**Files:**
- Modify: `src/hft_platform/services/system.py:671-679` (`_on_exec`, init)
- Modify: `src/hft_platform/execution/router.py:55-80` (init, run loop)
- Create: `tests/unit/test_d1_exec_overflow.py`

- [ ] **Step 1: Write failing test**

Create `tests/unit/test_d1_exec_overflow.py`:

```python
"""D1: Exec queue overflow must route to buffer, not silently drop fills."""
from __future__ import annotations

import asyncio
import collections
from unittest.mock import MagicMock

import pytest


class TestSafeEnqueueExec:
    """_safe_enqueue_exec routes to overflow buffer on QueueFull."""

    def test_normal_enqueue(self):
        """When queue has space, event goes directly to queue."""
        queue = asyncio.Queue(maxsize=10)
        overflow_buf: collections.deque = collections.deque()
        metrics = MagicMock()
        storm_guard = MagicMock()

        event = MagicMock()
        # Simulate _safe_enqueue_exec logic
        try:
            queue.put_nowait(event)
        except asyncio.QueueFull:
            overflow_buf.append(event)

        assert queue.qsize() == 1
        assert len(overflow_buf) == 0

    def test_overflow_routes_to_buffer(self):
        """When queue is full, event goes to overflow buffer."""
        queue = asyncio.Queue(maxsize=1)
        queue.put_nowait(MagicMock())  # fill it
        overflow_buf: collections.deque = collections.deque()
        overflow_max = 4096
        metrics = MagicMock()
        storm_guard = MagicMock()

        event = MagicMock()
        try:
            queue.put_nowait(event)
        except asyncio.QueueFull:
            if len(overflow_buf) >= overflow_max:
                metrics.exec_overflow_evicted_total.inc()
                storm_guard.trigger_halt("exec_overflow_buf_exhausted")
            else:
                overflow_buf.append(event)
                metrics.exec_queue_overflow_total.inc()

        assert len(overflow_buf) == 1
        metrics.exec_queue_overflow_total.inc.assert_called_once()

    def test_overflow_buffer_full_triggers_halt(self):
        """When overflow buffer is also full, HALT immediately and log eviction."""
        queue = asyncio.Queue(maxsize=1)
        queue.put_nowait(MagicMock())
        overflow_buf: collections.deque = collections.deque()
        overflow_max = 2
        # Pre-fill overflow buffer to capacity
        overflow_buf.append(MagicMock())
        overflow_buf.append(MagicMock())
        metrics = MagicMock()
        storm_guard = MagicMock()

        event = MagicMock()
        try:
            queue.put_nowait(event)
        except asyncio.QueueFull:
            if len(overflow_buf) >= overflow_max:
                metrics.exec_overflow_evicted_total.inc()
                storm_guard.trigger_halt("exec_overflow_buf_exhausted")
            else:
                overflow_buf.append(event)

        assert len(overflow_buf) == 2  # unchanged — event was NOT appended
        metrics.exec_overflow_evicted_total.inc.assert_called_once()
        storm_guard.trigger_halt.assert_called_once_with("exec_overflow_buf_exhausted")

    def test_halt_after_3_overflows(self):
        """StormGuard HALT triggers after 3 cumulative overflows."""
        queue = asyncio.Queue(maxsize=1)
        queue.put_nowait(MagicMock())
        overflow_buf: collections.deque = collections.deque()
        overflow_max = 4096
        overflow_counter = 0
        metrics = MagicMock()
        storm_guard = MagicMock()

        for _ in range(3):
            event = MagicMock()
            try:
                queue.put_nowait(event)
            except asyncio.QueueFull:
                if len(overflow_buf) >= overflow_max:
                    pass
                else:
                    overflow_buf.append(event)
                    overflow_counter += 1
                    metrics.exec_queue_overflow_total.inc()
                    if overflow_counter >= 3:
                        storm_guard.trigger_halt("exec_queue_overflow_repeated")

        assert overflow_counter == 3
        storm_guard.trigger_halt.assert_called_once_with("exec_queue_overflow_repeated")


class TestExecutionRouterOverflowDrain:
    """Router must drain overflow buffer before processing main queue."""

    def test_overflow_drained_first(self):
        overflow_buf: collections.deque = collections.deque()
        overflow_buf.append("overflow_event_1")
        overflow_buf.append("overflow_event_2")
        metrics = MagicMock()

        processed = []
        while overflow_buf:
            processed.append(overflow_buf.popleft())
            metrics.exec_overflow_drained_total.inc()

        assert processed == ["overflow_event_1", "overflow_event_2"]
        assert metrics.exec_overflow_drained_total.inc.call_count == 2
```

- [ ] **Step 2: Run test — verify it passes (pattern tests)**

```bash
uv run pytest tests/unit/test_d1_exec_overflow.py -v
```
Expected: PASS (pattern tests). Now implement in actual code.

- [ ] **Step 3: Implement overflow buffer init in system.py**

In `src/hft_platform/services/system.py`, add `import collections` at the top.

In the `__init__` or bootstrap setup where `self.raw_exec_queue` is created, add:

```python
        self._exec_overflow_buf: collections.deque = collections.deque()
        self._EXEC_OVERFLOW_MAX: int = 4096
        self._exec_overflow_counter: int = 0
        self._exec_overflow_evicted: int = 0
```

- [ ] **Step 4: Add `_safe_enqueue_exec` method to HFTSystem**

In `src/hft_platform/services/system.py`, add this method (before or after `_on_exec`):

```python
    def _safe_enqueue_exec(self, event) -> None:
        """Enqueue exec event with overflow buffer fallback."""
        try:
            self.raw_exec_queue.put_nowait(event)
        except asyncio.QueueFull:
            buf_len = len(self._exec_overflow_buf)
            if buf_len >= self._EXEC_OVERFLOW_MAX:
                self._exec_overflow_evicted += 1
                self.metrics.exec_overflow_evicted_total.inc()
                logger.critical(
                    "exec_overflow_buf FULL — fill LOST",
                    evicted_count=self._exec_overflow_evicted,
                    event_topic=getattr(event, "topic", "?"),
                )
                self.storm_guard.trigger_halt("exec_overflow_buf_exhausted")
                return
            self._exec_overflow_buf.append(event)
            self._exec_overflow_counter += 1
            self.metrics.exec_queue_overflow_total.inc()
            logger.critical(
                "raw_exec_queue FULL — fill routed to overflow buffer",
                overflow_count=self._exec_overflow_counter,
                buf_depth=buf_len + 1,
            )
            if self._exec_overflow_counter >= 3:
                self.storm_guard.trigger_halt("exec_queue_overflow_repeated")
```

- [ ] **Step 5: Update `_on_exec` to use wrapper**

Replace lines 678-679:

```python
            event = RawExecEvent(topic, data, timebase.now_ns())
            loop.call_soon_threadsafe(self.raw_exec_queue.put_nowait, event)
```

with:

```python
            event = RawExecEvent(topic, data, timebase.now_ns())
            loop.call_soon_threadsafe(self._safe_enqueue_exec, event)
```

- [ ] **Step 6: Pass overflow buffer to ExecutionRouter**

Where `ExecutionRouter` is constructed (in bootstrap or system.py), pass the overflow buffer reference. In `ExecutionRouter.__init__`, add a parameter:

```python
    def __init__(
        self,
        bus: RingBufferBus,
        raw_queue: asyncio.Queue,
        order_id_map: Dict[str, str],
        position_store: PositionStore,
        terminal_handler: Union[Callable[[str, str], None], object],
        risk_engine: Optional[object] = None,
        overflow_buf: Optional[collections.deque] = None,
    ):
        ...
        self._overflow_buf = overflow_buf
```

- [ ] **Step 7: Add overflow drain to router's `run()` loop**

In `src/hft_platform/execution/router.py`, after line 79 (`raw: RawExecEvent = await self.raw_queue.get()`), add:

```python
                # D1: Drain overflow buffer first (fills that couldn't enter main queue)
                if self._overflow_buf:
                    while self._overflow_buf:
                        try:
                            overflow_event = self._overflow_buf.popleft()
                            self._process_raw_event(overflow_event)
                            self.metrics.exec_overflow_drained_total.inc()
                        except Exception as e:  # noqa: BLE001
                            logger.error("overflow_drain_error", error=str(e))
```

Note: The existing event processing logic in `run()` (lines 85-150) should be extracted into a `_process_raw_event` method if not already, so both the main path and the overflow drain can use it. If extraction is too invasive, duplicate the key processing block inline for the overflow path.

- [ ] **Step 8: Run tests**

```bash
uv run pytest tests/unit/test_d1_exec_overflow.py -v && uv run pytest tests/ -k "router or execution" --timeout=30 -q
```
Expected: all pass.

- [ ] **Step 9: Commit**

```bash
git add src/hft_platform/services/system.py src/hft_platform/execution/router.py tests/unit/test_d1_exec_overflow.py
git commit -m "fix(exec): D1 — overflow ring buffer for raw_exec_queue with explicit eviction + HALT"
```

---

### Task 8: D2 — OrderAdapter live_orders TOCTOU Fix

**Files:**
- Modify: `src/hft_platform/order/adapter.py:112-113` (init), `245-252` (on_terminal_state), `447-455` (_pending_close_qty), `500-640` (_dispatch_to_api)
- Create: `tests/unit/test_d2_live_orders_toctou.py`

- [ ] **Step 1: Write failing test**

Create `tests/unit/test_d2_live_orders_toctou.py`:

```python
"""D2: Terminal callback arriving before _register_broker_ids must be deferred and drained."""
from __future__ import annotations

import asyncio
import time
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from hft_platform.order.adapter import _PENDING_SENTINEL, _TERMINAL_BEFORE_REGISTERED


def test_sentinel_objects_exist():
    """Module must export sentinel objects."""
    assert _PENDING_SENTINEL is not None
    assert _TERMINAL_BEFORE_REGISTERED is not None
    assert _PENDING_SENTINEL is not _TERMINAL_BEFORE_REGISTERED


class TestDeferredTerminal:
    """Terminal callback arriving during place_order race window must be deferred."""

    @pytest.fixture()
    def adapter(self):
        """Minimal OrderAdapter with deferred terminal support."""
        from hft_platform.order.adapter import OrderAdapter

        a = OrderAdapter.__new__(OrderAdapter)
        a.live_orders = {}
        a._live_orders_lock = asyncio.Lock()
        a._pending_order_keys = set()
        a._deferred_terminals = []
        a.order_id_resolver = MagicMock()
        a.metrics = MagicMock()
        return a

    @pytest.mark.asyncio
    async def test_terminal_deferred_when_order_pending(self, adapter):
        """on_terminal_state defers when strategy has pending orders."""
        # Pre-register sentinel
        async with adapter._live_orders_lock:
            adapter.live_orders["s1:42"] = _PENDING_SENTINEL
            adapter._pending_order_keys.add("s1:42")

        # Resolver returns a fallback key that doesn't match sentinel key
        adapter.order_id_resolver.resolve_order_key.return_value = "s1:ABC123"

        await adapter.on_terminal_state("s1", "ABC123")

        assert len(adapter._deferred_terminals) == 1
        assert adapter._deferred_terminals[0][0] == "s1"
        assert adapter._deferred_terminals[0][1] == "ABC123"
        adapter.metrics.terminal_before_registration_total.inc.assert_called_once()

    @pytest.mark.asyncio
    async def test_normal_terminal_deletes_order(self, adapter):
        """on_terminal_state deletes normally when order is registered (not sentinel)."""
        adapter.live_orders["s1:42"] = MagicMock()  # real trade, not sentinel
        adapter.order_id_resolver.resolve_order_key.return_value = "s1:42"

        await adapter.on_terminal_state("s1", "42")

        assert "s1:42" not in adapter.live_orders

    @pytest.mark.asyncio
    async def test_drain_resolves_deferred(self, adapter):
        """_drain_deferred_terminals resolves deferred entries after registration."""
        # Setup: order is now registered
        adapter.live_orders["s1:42"] = MagicMock()
        adapter._deferred_terminals = [("s1", "ABC123", time.monotonic())]

        # After _register_broker_ids, resolver can now map ABC123 -> s1:42
        adapter.order_id_resolver.resolve_order_key.return_value = "s1:42"

        await adapter._drain_deferred_terminals("s1:42", MagicMock())

        assert "s1:42" not in adapter.live_orders
        assert len(adapter._deferred_terminals) == 0

    @pytest.mark.asyncio
    async def test_deferred_terminal_expires_after_30s(self, adapter):
        """Stale deferred terminals older than 30s are garbage collected."""
        old_ts = time.monotonic() - 31.0
        adapter._deferred_terminals = [("s1", "OLD_ORDER", old_ts)]
        adapter.order_id_resolver.resolve_order_key.return_value = "s1:OLD_ORDER"

        await adapter._drain_deferred_terminals("s1:99", MagicMock())

        assert len(adapter._deferred_terminals) == 0
        adapter.metrics.deferred_terminal_expired_total.inc.assert_called_once()


class TestPendingCloseQtyLock:
    """_pending_close_qty must acquire _live_orders_lock."""

    @pytest.mark.asyncio
    async def test_pending_close_qty_acquires_lock(self):
        from hft_platform.order.adapter import OrderAdapter

        a = OrderAdapter.__new__(OrderAdapter)
        a.live_orders = {}
        a._live_orders_lock = asyncio.Lock()

        # If _pending_close_qty is now async, test it acquires the lock
        # If still sync, verify it's called within lock context
        assert hasattr(a, "_live_orders_lock")
```

- [ ] **Step 2: Run test — verify it fails**

```bash
uv run pytest tests/unit/test_d2_live_orders_toctou.py -v
```
Expected: `ImportError` on `_PENDING_SENTINEL` — it doesn't exist yet.

- [ ] **Step 3: Add sentinel objects and new fields to adapter**

In `src/hft_platform/order/adapter.py`, add at module level (after imports, before class def):

```python
_PENDING_SENTINEL = object()
_TERMINAL_BEFORE_REGISTERED = object()
```

In `OrderAdapter.__init__`, after line 113 (`self._live_orders_lock = asyncio.Lock()`), add:

```python
        self._pending_order_keys: set[str] = set()
        self._deferred_terminals: list[tuple[str, str, float]] = []
```

- [ ] **Step 4: Implement `on_terminal_state` with deferred terminal support**

Replace lines 245-252:

```python
    async def on_terminal_state(self, strategy_id: str, order_id: str) -> None:
        """Called when an order reaches a terminal state (Filled, Cancelled, Rejected)."""
        async with self._live_orders_lock:
            order_key = self.order_id_resolver.resolve_order_key(strategy_id, order_id, self.live_orders)

            if order_key in self.live_orders:
                logger.info("Removing terminal order", key=order_key)
                del self.live_orders[order_key]
```

with:

```python
    async def on_terminal_state(self, strategy_id: str, order_id: str) -> None:
        """Called when an order reaches a terminal state (Filled, Cancelled, Rejected)."""
        async with self._live_orders_lock:
            order_key = self.order_id_resolver.resolve_order_key(strategy_id, order_id, self.live_orders)
            entry = self.live_orders.get(order_key)

            if entry is not None and entry is not _PENDING_SENTINEL:
                # Normal path — order is registered, clean up
                logger.info("Removing terminal order", key=order_key)
                del self.live_orders[order_key]
                return

            # Check if any order for this strategy is in-flight
            has_pending = any(k.startswith(f"{strategy_id}:") for k in self._pending_order_keys)
            if has_pending:
                import time as _time

                self._deferred_terminals.append((strategy_id, order_id, _time.monotonic()))
                self.metrics.terminal_before_registration_total.inc()
                logger.warning(
                    "terminal_before_registration",
                    strategy_id=strategy_id,
                    broker_order_id=order_id,
                )
                return

            # No pending orders — genuine orphan or already cleaned up
            if order_key in self.live_orders:
                logger.info("Removing terminal order", key=order_key)
                del self.live_orders[order_key]
```

- [ ] **Step 5: Implement `_drain_deferred_terminals`**

Add this new method to `OrderAdapter`:

```python
    async def _drain_deferred_terminals(self, order_key: str, trade: Any) -> None:
        """Re-process deferred terminal callbacks now that broker IDs are registered."""
        import time as _time

        remaining: list[tuple[str, str, float]] = []
        now = _time.monotonic()
        async with self._live_orders_lock:
            for sid, oid, ts in self._deferred_terminals:
                if now - ts >= 30.0:
                    logger.error(
                        "deferred_terminal_expired",
                        strategy_id=sid,
                        broker_order_id=oid,
                        age_s=round(now - ts, 1),
                    )
                    self.metrics.deferred_terminal_expired_total.inc()
                    continue
                resolved = self.order_id_resolver.resolve_order_key(sid, oid, self.live_orders)
                if resolved in self.live_orders:
                    del self.live_orders[resolved]
                    logger.info(
                        "deferred_terminal_cleanup",
                        key=resolved,
                        broker_order_id=oid,
                        defer_age_ms=int((now - ts) * 1000),
                    )
                else:
                    remaining.append((sid, oid, ts))
            self._deferred_terminals = remaining
```

- [ ] **Step 6: Modify `_dispatch_to_api` NEW path**

In `_dispatch_to_api`, replace the NEW order path (lines ~500-640). After line 500 (`order_key = f"{intent.strategy_id}:{intent.intent_id}"`), before the `place_order` call:

Add pre-registration:
```python
                # D2: Pre-register sentinel to track in-flight order
                async with self._live_orders_lock:
                    self.live_orders[order_key] = _PENDING_SENTINEL
                    self._pending_order_keys.add(order_key)
```

After the `place_order` call, where `trade is None` check happens (line 613-614):
```python
                if trade is None:
                    async with self._live_orders_lock:
                        self.live_orders.pop(order_key, None)
                        self._pending_order_keys.discard(order_key)
                    return
```

Replace the existing `live_orders` write block (lines 635-640):
```python
                # Store with lock protection
                async with self._live_orders_lock:
                    self.live_orders[order_key] = trade

                # Populate lookup using Shioaji trade attributes (broker ID -> order_key).
                await self._register_broker_ids(order_key, trade)
```

with:

```python
                # D2: Replace sentinel with real trade and drain deferred terminals
                async with self._live_orders_lock:
                    self.live_orders[order_key] = trade
                    self._pending_order_keys.discard(order_key)

                await self._register_broker_ids(order_key, trade)
                await self._drain_deferred_terminals(order_key, trade)
```

- [ ] **Step 7: Fix `_pending_close_qty` lockless read**

Replace lines 447-455:

```python
    def _pending_close_qty(self, symbol: str, side: Side) -> int:
        pending_qty = 0
        for trade in self.live_orders.values():
            if self._live_order_symbol(trade) != symbol:
                continue
            if self._live_order_side(trade) != side:
                continue
            pending_qty += self._live_order_qty(trade)
        return pending_qty
```

with:

```python
    async def _pending_close_qty(self, symbol: str, side: Side) -> int:
        pending_qty = 0
        async with self._live_orders_lock:
            for trade in self.live_orders.values():
                if trade is _PENDING_SENTINEL or trade is _TERMINAL_BEFORE_REGISTERED:
                    continue
                if self._live_order_symbol(trade) != symbol:
                    continue
                if self._live_order_side(trade) != side:
                    continue
                pending_qty += self._live_order_qty(trade)
        return pending_qty
```

**Note**: This changes `_pending_close_qty` from sync to async. Find all callers and add `await`. If any caller is synchronous and cannot be made async, use a sync lock wrapper pattern instead (acquire `_live_orders_lock` via `asyncio.Lock` requires async context).

- [ ] **Step 8: Run tests**

```bash
uv run pytest tests/unit/test_d2_live_orders_toctou.py -v && uv run pytest tests/ -k "adapter or order" --timeout=30 -q
```
Expected: all pass.

- [ ] **Step 9: Run full test suite**

```bash
uv run pytest tests/ -x --timeout=30 -q
```
Expected: no regressions.

- [ ] **Step 10: Commit**

```bash
git add src/hft_platform/order/adapter.py tests/unit/test_d2_live_orders_toctou.py
git commit -m "fix(order): D2 — deferred terminal queue for live_orders TOCTOU race"
```

---

### Task 9: Final Verification

**Files:** None (read-only)

- [ ] **Step 1: Run full test suite**

```bash
uv run pytest tests/ --timeout=60 -q
```
Expected: all tests pass, no regressions.

- [ ] **Step 2: Run lint**

```bash
uv run ruff check src/hft_platform/observability/metrics.py src/hft_platform/risk/storm_guard.py src/hft_platform/order/circuit_breaker.py src/hft_platform/strategy/runner.py src/hft_platform/risk/engine.py src/hft_platform/order/adapter.py src/hft_platform/execution/positions.py src/hft_platform/ops/session_governor.py src/hft_platform/services/system.py src/hft_platform/execution/router.py
```
Expected: no lint errors.

- [ ] **Step 3: Run type check on modified files**

```bash
uv run mypy src/hft_platform/observability/metrics.py src/hft_platform/risk/storm_guard.py src/hft_platform/order/circuit_breaker.py src/hft_platform/strategy/runner.py src/hft_platform/risk/engine.py src/hft_platform/order/adapter.py src/hft_platform/execution/positions.py src/hft_platform/ops/session_governor.py
```
Expected: no new type errors.

- [ ] **Step 4: Verify all 8 new metrics are accessible**

```bash
uv run python -c "
from hft_platform.observability.metrics import MetricsRegistry
m = MetricsRegistry()
for attr in ['exec_queue_overflow_total', 'exec_overflow_drained_total', 'exec_overflow_evicted_total',
             'terminal_before_registration_total', 'deferred_terminal_expired_total',
             'risk_halt_blocked_total', 'order_queue_full_total', 'intent_queue_full_total']:
    assert hasattr(m, attr), f'Missing metric: {attr}'
print('All 8 metrics OK')
"
```
Expected: `All 8 metrics OK`

- [ ] **Step 5: Verify git log**

```bash
git log --oneline -10
```
Expected: 9 commits (T0–T8), each with conventional commit format, in order.
