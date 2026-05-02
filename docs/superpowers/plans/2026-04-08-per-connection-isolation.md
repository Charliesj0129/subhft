# Per-Connection Isolation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Isolate per-facade failures in QuoteConnectionPool so a single connection drop does not trigger platform-wide STORM or reconnect storms.

**Architecture:** Add `FacadeState` FSM and `FacadeSlot` per-connection tracking inside `QuoteConnectionPool`. Health checks run from the existing system monitor loop. StormGuard only reads feed gap from CONNECTED facades. Reconnect targets only unhealthy facades. Warmup reset is scoped to affected symbols.

**Tech Stack:** Python 3.12, structlog, Prometheus client, asyncio

**Spec:** `docs/superpowers/specs/2026-04-08-per-connection-isolation-design.md`

---

### Task 1: FacadeState + FacadeSlot data structures

**Files:**
- Create: `src/hft_platform/feed_adapter/shioaji/facade_slot.py`
- Test: `tests/unit/test_facade_slot.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/unit/test_facade_slot.py
"""Tests for FacadeSlot state machine."""
import time
from unittest.mock import MagicMock

from hft_platform.feed_adapter.shioaji.facade_slot import FacadeSlot, FacadeState


class TestFacadeState:
    def test_enum_ordering(self):
        assert FacadeState.CONNECTED < FacadeState.DEGRADED
        assert FacadeState.DEGRADED < FacadeState.RECOVERING
        assert FacadeState.RECOVERING < FacadeState.DISCONNECTED

    def test_is_healthy(self):
        assert FacadeState.CONNECTED.is_healthy()
        assert not FacadeState.DEGRADED.is_healthy()
        assert not FacadeState.RECOVERING.is_healthy()
        assert not FacadeState.DISCONNECTED.is_healthy()


class TestFacadeSlot:
    def test_init_defaults(self):
        facade = MagicMock()
        slot = FacadeSlot(conn_id=0, facade=facade, symbols={"TXFD6", "MXFD6"})
        assert slot.conn_id == 0
        assert slot.state == FacadeState.CONNECTED
        assert slot.symbols == {"TXFD6", "MXFD6"}
        assert slot.reconnect_failures == 0

    def test_feed_gap(self):
        facade = MagicMock()
        slot = FacadeSlot(conn_id=0, facade=facade, symbols=set())
        slot.last_data_mono = time.monotonic() - 5.0
        gap = slot.feed_gap_s()
        assert 4.9 < gap < 5.5

    def test_backoff_seconds(self):
        facade = MagicMock()
        slot = FacadeSlot(conn_id=0, facade=facade, symbols=set())
        slot.reconnect_failures = 0
        assert slot.backoff_s() == 5
        slot.reconnect_failures = 1
        assert slot.backoff_s() == 10
        slot.reconnect_failures = 5
        assert slot.backoff_s() == 120  # capped at 120
        slot.reconnect_failures = 10
        assert slot.backoff_s() == 120  # still capped
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/unit/test_facade_slot.py -v`
Expected: FAIL with `ModuleNotFoundError`

- [ ] **Step 3: Write minimal implementation**

```python
# src/hft_platform/feed_adapter/shioaji/facade_slot.py
"""Per-facade state tracking for QuoteConnectionPool."""
from __future__ import annotations

import time
from enum import IntEnum
from typing import Any


class FacadeState(IntEnum):
    CONNECTED = 0
    DEGRADED = 1
    RECOVERING = 2
    DISCONNECTED = 3

    def is_healthy(self) -> bool:
        return self == FacadeState.CONNECTED


class FacadeSlot:
    """Tracks per-facade connection state, health, and reconnect backoff."""

    __slots__ = (
        "conn_id",
        "facade",
        "state",
        "symbols",
        "last_data_mono",
        "last_reconnect_mono",
        "reconnect_failures",
        "degraded_since_mono",
    )

    def __init__(
        self,
        conn_id: int,
        facade: Any,
        symbols: set[str],
    ) -> None:
        self.conn_id = conn_id
        self.facade = facade
        self.state = FacadeState.CONNECTED
        self.symbols = symbols
        self.last_data_mono: float = time.monotonic()
        self.last_reconnect_mono: float = 0.0
        self.reconnect_failures: int = 0
        self.degraded_since_mono: float = 0.0

    def feed_gap_s(self) -> float:
        return time.monotonic() - self.last_data_mono

    def backoff_s(self) -> float:
        return min(120.0, 5.0 * (2 ** self.reconnect_failures))
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/unit/test_facade_slot.py -v`
Expected: PASS (6 tests)

- [ ] **Step 5: Commit**

```bash
git add src/hft_platform/feed_adapter/shioaji/facade_slot.py tests/unit/test_facade_slot.py
git commit -m "feat(pool): add FacadeState FSM and FacadeSlot data structure"
```

---

### Task 2: Health check + get_healthy_feed_gap_s

**Files:**
- Create: `src/hft_platform/feed_adapter/shioaji/pool_health.py`
- Test: `tests/unit/test_pool_health.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/unit/test_pool_health.py
"""Tests for pool health check logic and healthy feed gap calculation."""
import time
from unittest.mock import MagicMock

from hft_platform.feed_adapter.shioaji.facade_slot import FacadeSlot, FacadeState
from hft_platform.feed_adapter.shioaji.pool_health import (
    check_facade_health,
    get_healthy_feed_gap_s,
)


def _make_slot(conn_id: int, state: FacadeState = FacadeState.CONNECTED, gap: float = 0.0) -> FacadeSlot:
    slot = FacadeSlot(conn_id=conn_id, facade=MagicMock(), symbols={f"SYM{conn_id}"})
    slot.state = state
    slot.last_data_mono = time.monotonic() - gap
    return slot


class TestGetHealthyFeedGapS:
    def test_all_connected_returns_max_gap(self):
        slots = [_make_slot(0, gap=1.0), _make_slot(1, gap=2.0)]
        gap = get_healthy_feed_gap_s(slots)
        assert 1.8 < gap < 2.5

    def test_degraded_excluded(self):
        slots = [
            _make_slot(0, gap=0.5),
            _make_slot(1, state=FacadeState.DEGRADED, gap=10.0),
        ]
        gap = get_healthy_feed_gap_s(slots)
        assert gap < 1.0  # only slot 0 counted

    def test_all_degraded_returns_inf(self):
        slots = [
            _make_slot(0, state=FacadeState.DEGRADED, gap=5.0),
            _make_slot(1, state=FacadeState.DISCONNECTED, gap=10.0),
        ]
        gap = get_healthy_feed_gap_s(slots)
        assert gap == float("inf")


class TestCheckFacadeHealth:
    def test_connected_to_degraded(self):
        slot = _make_slot(0, gap=5.0)  # > 3s threshold
        assert slot.state == FacadeState.CONNECTED
        schedule_calls: list[int] = []
        check_facade_health([slot], degraded_threshold_s=3.0, reconnect_trigger_s=10.0, schedule_fn=lambda cid: schedule_calls.append(cid))
        assert slot.state == FacadeState.DEGRADED

    def test_degraded_recovery(self):
        slot = _make_slot(0, state=FacadeState.DEGRADED, gap=0.5)
        check_facade_health([slot], degraded_threshold_s=3.0, reconnect_trigger_s=10.0, schedule_fn=lambda cid: None)
        assert slot.state == FacadeState.CONNECTED

    def test_degraded_to_reconnect(self):
        slot = _make_slot(0, state=FacadeState.DEGRADED, gap=15.0)
        slot.degraded_since_mono = time.monotonic() - 12.0  # > 10s trigger
        schedule_calls: list[int] = []
        check_facade_health([slot], degraded_threshold_s=3.0, reconnect_trigger_s=10.0, schedule_fn=lambda cid: schedule_calls.append(cid))
        assert schedule_calls == [0]

    def test_disconnected_backoff_retry(self):
        slot = _make_slot(0, state=FacadeState.DISCONNECTED, gap=20.0)
        slot.reconnect_failures = 0
        slot.last_reconnect_mono = time.monotonic() - 10.0  # > 5s backoff
        schedule_calls: list[int] = []
        check_facade_health([slot], degraded_threshold_s=3.0, reconnect_trigger_s=10.0, schedule_fn=lambda cid: schedule_calls.append(cid))
        assert schedule_calls == [0]

    def test_disconnected_backoff_too_soon(self):
        slot = _make_slot(0, state=FacadeState.DISCONNECTED, gap=20.0)
        slot.reconnect_failures = 0
        slot.last_reconnect_mono = time.monotonic() - 1.0  # < 5s backoff
        schedule_calls: list[int] = []
        check_facade_health([slot], degraded_threshold_s=3.0, reconnect_trigger_s=10.0, schedule_fn=lambda cid: schedule_calls.append(cid))
        assert schedule_calls == []
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/unit/test_pool_health.py -v`
Expected: FAIL with `ModuleNotFoundError`

- [ ] **Step 3: Write minimal implementation**

```python
# src/hft_platform/feed_adapter/shioaji/pool_health.py
"""Health check logic for QuoteConnectionPool per-facade isolation."""
from __future__ import annotations

import time
from typing import Callable

from structlog import get_logger

from hft_platform.feed_adapter.shioaji.facade_slot import FacadeSlot, FacadeState

logger = get_logger("feed_adapter.pool_health")


def get_healthy_feed_gap_s(slots: list[FacadeSlot]) -> float:
    """Max feed gap across CONNECTED facades only.

    Returns float('inf') when no facade is CONNECTED (triggers HALT).
    """
    now = time.monotonic()
    max_gap = 0.0
    has_connected = False
    for slot in slots:
        if slot.state == FacadeState.CONNECTED:
            has_connected = True
            gap = now - slot.last_data_mono
            if gap > max_gap:
                max_gap = gap
    if not has_connected:
        return float("inf")
    return max_gap


def check_facade_health(
    slots: list[FacadeSlot],
    *,
    degraded_threshold_s: float = 3.0,
    reconnect_trigger_s: float = 10.0,
    schedule_fn: Callable[[int], None],
) -> None:
    """Update per-slot state based on feed gap. Called from monitor loop."""
    now = time.monotonic()
    for slot in slots:
        gap = now - slot.last_data_mono

        if slot.state == FacadeState.CONNECTED:
            if gap > degraded_threshold_s:
                slot.state = FacadeState.DEGRADED
                slot.degraded_since_mono = now
                logger.warning("facade_degraded", conn_id=slot.conn_id, gap_s=round(gap, 2))

        elif slot.state == FacadeState.DEGRADED:
            if gap <= degraded_threshold_s:
                slot.state = FacadeState.CONNECTED
                logger.info("facade_recovered", conn_id=slot.conn_id)
            elif now - slot.degraded_since_mono > reconnect_trigger_s:
                schedule_fn(slot.conn_id)

        elif slot.state == FacadeState.DISCONNECTED:
            if now - slot.last_reconnect_mono > slot.backoff_s():
                schedule_fn(slot.conn_id)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/unit/test_pool_health.py -v`
Expected: PASS (7 tests)

- [ ] **Step 5: Commit**

```bash
git add src/hft_platform/feed_adapter/shioaji/pool_health.py tests/unit/test_pool_health.py
git commit -m "feat(pool): add per-facade health check and healthy feed gap calculation"
```

---

### Task 3: LOBEngine.reset_books_for_symbols + FeatureEngine.reset_symbols

**Files:**
- Modify: `src/hft_platform/feed_adapter/lob_engine.py`
- Modify: `src/hft_platform/feature/engine.py`
- Test: `tests/unit/test_targeted_reset.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/unit/test_targeted_reset.py
"""Tests for targeted symbol-scoped reset on LOBEngine and FeatureEngine."""
from unittest.mock import MagicMock, patch


class TestLOBEngineResetForSymbols:
    def test_resets_only_specified_symbols(self):
        from hft_platform.feed_adapter.lob_engine import LOBEngine

        lob = LOBEngine.__new__(LOBEngine)
        lob.books = {"A": MagicMock(), "B": MagicMock(), "C": MagicMock()}
        lob._last_symbol = "A"
        lob._last_book = lob.books["A"]
        lob._metrics_known_symbols = set()

        lob.reset_books_for_symbols({"A", "C"})

        assert "A" not in lob.books
        assert "B" in lob.books
        assert "C" not in lob.books
        assert lob._last_symbol is None
        assert lob._last_book is None

    def test_preserves_cache_when_not_affected(self):
        from hft_platform.feed_adapter.lob_engine import LOBEngine

        lob = LOBEngine.__new__(LOBEngine)
        lob.books = {"A": MagicMock(), "B": MagicMock()}
        lob._last_symbol = "B"
        lob._last_book = lob.books["B"]
        lob._metrics_known_symbols = set()

        lob.reset_books_for_symbols({"A"})

        assert lob._last_symbol == "B"
        assert lob._last_book is not None


class TestFeatureEngineResetSymbols:
    def test_resets_only_specified_symbols(self):
        from hft_platform.feature.engine import FeatureEngine

        fe = MagicMock(spec=FeatureEngine)
        fe.reset_symbols = FeatureEngine.reset_symbols.__get__(fe, FeatureEngine)

        fe.reset_symbols({"SYM1", "SYM2"})

        assert fe.reset_symbol.call_count == 2
        called_syms = {call.args[0] for call in fe.reset_symbol.call_args_list}
        assert called_syms == {"SYM1", "SYM2"}
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/unit/test_targeted_reset.py -v`
Expected: FAIL with `AttributeError: type object 'LOBEngine' has no attribute 'reset_books_for_symbols'`

- [ ] **Step 3: Add `reset_books_for_symbols` to LOBEngine**

Add after the existing `reset_books()` method in `src/hft_platform/feed_adapter/lob_engine.py`:

```python
    def reset_books_for_symbols(self, symbols: set[str]) -> None:
        """Reset only the specified symbols' book state."""
        for sym in symbols:
            self.books.pop(sym, None)
        if self._last_symbol in symbols:
            self._last_symbol = None
            self._last_book = None
```

- [ ] **Step 4: Add `reset_symbols` to FeatureEngine**

Add after the existing `reset_all()` method in `src/hft_platform/feature/engine.py`:

```python
    def reset_symbols(self, symbols: set[str]) -> None:
        """Reset state for a subset of symbols (per-facade reconnect)."""
        for sym in symbols:
            self.reset_symbol(sym)
```

- [ ] **Step 5: Run test to verify it passes**

Run: `uv run pytest tests/unit/test_targeted_reset.py -v`
Expected: PASS (3 tests)

- [ ] **Step 6: Commit**

```bash
git add src/hft_platform/feed_adapter/lob_engine.py src/hft_platform/feature/engine.py tests/unit/test_targeted_reset.py
git commit -m "feat(pool): add targeted reset_books_for_symbols and reset_symbols methods"
```

---

### Task 4: Integrate FacadeSlot into QuoteConnectionPool

**Files:**
- Modify: `src/hft_platform/feed_adapter/shioaji/quote_connection_pool.py`
- Test: `tests/unit/test_pool_integration.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/unit/test_pool_integration.py
"""Integration tests for QuoteConnectionPool per-facade isolation."""
import asyncio
import time
from unittest.mock import MagicMock, patch

import pytest

from hft_platform.feed_adapter.shioaji.facade_slot import FacadeState


class TestPoolFacadeSlots:
    def test_slots_created_on_create_facades(self):
        with patch("hft_platform.feed_adapter.shioaji.quote_connection_pool.ShioajiClientFacade") as MockFacade:
            from hft_platform.feed_adapter.shioaji.quote_connection_pool import QuoteConnectionPool
            import tempfile, os, yaml

            symbols_path = os.path.join(tempfile.mkdtemp(), "symbols.yaml")
            with open(symbols_path, "w") as f:
                yaml.safe_dump({"symbols": [
                    {"code": "A", "group": 0}, {"code": "B", "group": 1},
                ]}, f)

            pool = QuoteConnectionPool(symbols_path, {}, num_conns=2)
            pool.create_facades()

            assert len(pool._slots) == 2
            assert pool._slots[0].conn_id == 0
            assert pool._slots[0].symbols == {"A"}
            assert pool._slots[1].symbols == {"B"}
            assert all(s.state == FacadeState.CONNECTED for s in pool._slots)

    def test_get_healthy_feed_gap_excludes_degraded(self):
        with patch("hft_platform.feed_adapter.shioaji.quote_connection_pool.ShioajiClientFacade"):
            from hft_platform.feed_adapter.shioaji.quote_connection_pool import QuoteConnectionPool
            import tempfile, os, yaml

            symbols_path = os.path.join(tempfile.mkdtemp(), "symbols.yaml")
            with open(symbols_path, "w") as f:
                yaml.safe_dump({"symbols": [
                    {"code": "A", "group": 0}, {"code": "B", "group": 1},
                ]}, f)

            pool = QuoteConnectionPool(symbols_path, {}, num_conns=2)
            pool.create_facades()

            # Slot 0 fresh, slot 1 degraded with large gap
            pool._slots[0].last_data_mono = time.monotonic()
            pool._slots[1].state = FacadeState.DEGRADED
            pool._slots[1].last_data_mono = time.monotonic() - 30.0

            gap = pool.get_healthy_feed_gap_s()
            assert gap < 1.0  # only slot 0 counted

    def test_reconnect_skips_healthy(self):
        with patch("hft_platform.feed_adapter.shioaji.quote_connection_pool.ShioajiClientFacade") as MockFacade:
            from hft_platform.feed_adapter.shioaji.quote_connection_pool import QuoteConnectionPool
            import tempfile, os, yaml

            symbols_path = os.path.join(tempfile.mkdtemp(), "symbols.yaml")
            with open(symbols_path, "w") as f:
                yaml.safe_dump({"symbols": [
                    {"code": "A", "group": 0}, {"code": "B", "group": 1},
                ]}, f)

            pool = QuoteConnectionPool(symbols_path, {}, num_conns=2)
            pool.create_facades()

            # Slot 0 connected, slot 1 degraded
            pool._slots[1].state = FacadeState.DEGRADED

            pool.reconnect(reason="test")

            # Only slot 1's facade should have reconnect called
            pool._slots[0].facade.reconnect.assert_not_called()
            pool._slots[1].facade.reconnect.assert_called_once()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/unit/test_pool_integration.py -v`
Expected: FAIL with `AttributeError: 'QuoteConnectionPool' object has no attribute '_slots'`

- [ ] **Step 3: Integrate FacadeSlot into QuoteConnectionPool**

Modify `src/hft_platform/feed_adapter/shioaji/quote_connection_pool.py`:

1. Add imports at top:
```python
from hft_platform.feed_adapter.shioaji.facade_slot import FacadeSlot, FacadeState
from hft_platform.feed_adapter.shioaji.pool_health import check_facade_health, get_healthy_feed_gap_s
```

2. Add to `__slots__`:
```python
"_slots",
"_lob",
"_feature_engine",
"_degraded_threshold_s",
"_reconnect_trigger_s",
"_per_facade_timeout_s",
```

3. In `__init__`, add after existing env var reads:
```python
self._slots: list[FacadeSlot] = []
self._lob: Any = None
self._feature_engine: Any = None
self._degraded_threshold_s = float(os.getenv("HFT_FACADE_DEGRADED_THRESHOLD_S", "3"))
self._reconnect_trigger_s = float(os.getenv("HFT_FACADE_RECONNECT_TRIGGER_S", "10"))
self._per_facade_timeout_s = float(os.getenv("HFT_PER_FACADE_TIMEOUT_S", "15"))
```

4. In `create_facades()`, after creating each facade, build the FacadeSlot:
```python
def create_facades(self) -> None:
    self._clients = []
    self._slots = []
    for group_id in range(self._num_conns):
        per_conn_cfg = dict(self._config)
        per_conn_cfg["session_lock_suffix"] = f"_conn{group_id}"
        facade = ShioajiClientFacade(
            config_path=self._shard_paths[group_id],
            shioaji_config=per_conn_cfg,
        )
        self._clients.append(facade)
        # Build symbol set from shard config
        with open(self._shard_paths[group_id], "r") as f:
            shard_data = yaml.safe_load(f) or {}
        symbols = {s.get("code", "") for s in shard_data.get("symbols", []) if s.get("code")}
        self._slots.append(FacadeSlot(conn_id=group_id, facade=facade, symbols=symbols))
        logger.info("Created facade for group", conn_id=group_id, symbols=len(symbols))
```

5. Add new public methods:
```python
def set_reset_targets(self, lob: Any, feature_engine: Any) -> None:
    """Inject LOB and FeatureEngine for targeted warmup reset."""
    self._lob = lob
    self._feature_engine = feature_engine

def get_healthy_feed_gap_s(self) -> float:
    """Max feed gap across CONNECTED facades only."""
    return get_healthy_feed_gap_s(self._slots)

def check_facade_health(self) -> None:
    """Update per-slot state. Called from system monitor loop."""
    check_facade_health(
        self._slots,
        degraded_threshold_s=self._degraded_threshold_s,
        reconnect_trigger_s=self._reconnect_trigger_s,
        schedule_fn=self._schedule_reconnect,
    )

def _schedule_reconnect(self, conn_id: int) -> None:
    """Mark slot as RECOVERING and schedule async reconnect."""
    slot = self._slots[conn_id]
    if slot.state == FacadeState.RECOVERING:
        return  # already in progress
    slot.state = FacadeState.RECOVERING
    slot.last_reconnect_mono = time.monotonic()
    logger.warning("facade_reconnect_scheduled", conn_id=conn_id)

def _notify_warmup_reset(self, conn_id: int) -> None:
    """Reset LOB/Feature state for a single facade's symbols."""
    symbols = self._slots[conn_id].symbols
    if self._lob is not None and hasattr(self._lob, "reset_books_for_symbols"):
        self._lob.reset_books_for_symbols(symbols)
    if self._feature_engine is not None and hasattr(self._feature_engine, "reset_symbols"):
        self._feature_engine.reset_symbols(symbols)
    logger.info("facade_warmup_reset", conn_id=conn_id, symbols=len(symbols))
```

6. Rewrite `reconnect()` to skip healthy facades:
```python
def reconnect(self, reason: str = "", force: bool = False) -> bool:
    """Reconnect only unhealthy facades. Healthy facades are untouched."""
    targets = [s for s in self._slots if force or s.state != FacadeState.CONNECTED]
    if not targets:
        return True
    any_ok = False
    for slot in targets:
        slot.state = FacadeState.RECOVERING
        slot.last_reconnect_mono = time.monotonic()
        log = logger.bind(conn_id=slot.conn_id)
        try:
            ok = slot.facade.reconnect(reason=reason, force=force)
            if ok:
                slot.state = FacadeState.CONNECTED
                slot.reconnect_failures = 0
                slot.last_data_mono = time.monotonic()
                self._notify_warmup_reset(slot.conn_id)
                any_ok = True
                log.info("facade_reconnected")
            else:
                slot.reconnect_failures += 1
                slot.state = FacadeState.DISCONNECTED
                log.warning("facade_reconnect_failed")
        except Exception as exc:
            slot.reconnect_failures += 1
            slot.state = FacadeState.DISCONNECTED
            log.error("facade_reconnect_exception", error=str(exc))
    return any_ok
```

7. Wrap callbacks in `subscribe_all` to update `last_data_mono`:
```python
def subscribe_all(self, cb: Callable[..., Any]) -> None:
    for i, facade in enumerate(self._clients):
        log = logger.bind(conn_id=i)
        if not facade.logged_in:
            log.warning("Skipping subscribe for unconnected facade")
            continue
        slot = self._slots[i]
        def _make_wrapper(s: FacadeSlot, original_cb: Callable) -> Callable:
            def wrapper(*args: Any, **kwargs: Any) -> Any:
                s.last_data_mono = time.monotonic()
                return original_cb(*args, **kwargs)
            return wrapper
        try:
            facade.subscribe_basket(_make_wrapper(slot, cb))
            log.info("Subscribed", count=facade.subscribed_count)
        except Exception as exc:
            log.error("Subscribe failed", error=str(exc))

    if os.getenv("HFT_OPTIONS_AUTO_REFRESH", "1").lower() not in {"0", "false", "no", "off"}:
        self.start_options_refresh_thread(cb=cb)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/unit/test_pool_integration.py -v`
Expected: PASS (3 tests)

- [ ] **Step 5: Commit**

```bash
git add src/hft_platform/feed_adapter/shioaji/quote_connection_pool.py tests/unit/test_pool_integration.py
git commit -m "feat(pool): integrate FacadeSlot into QuoteConnectionPool with per-facade reconnect"
```

---

### Task 5: Wire into system.py + bootstrap.py + _md_reconnect.py

**Files:**
- Modify: `src/hft_platform/services/system.py:44-53` (StormGuard feed gap)
- Modify: `src/hft_platform/services/system.py:~640` (monitor loop)
- Modify: `src/hft_platform/services/bootstrap.py:~598-605` (inject lob+fe)
- Modify: `src/hft_platform/services/_md_reconnect.py:160-168` (remove global reset)

- [ ] **Step 1: Modify `_get_max_feed_gap_s` in system.py**

Change `src/hft_platform/services/system.py` lines 44-53:

```python
@staticmethod
def _get_max_feed_gap_s(md_service: Any) -> float:
    """Return max feed gap from market data service, or 0.0 if unavailable."""
    # Prefer per-facade healthy gap from QuoteConnectionPool
    client = getattr(md_service, "client", None)
    if client is not None and hasattr(client, "get_healthy_feed_gap_s"):
        gap = client.get_healthy_feed_gap_s()
        within_fn = getattr(md_service, "within_reconnect_window", None)
        if within_fn is not None and not within_fn():
            return 0.0
        return float(gap)
    fn = getattr(md_service, "get_max_feed_gap_s", None)
    if fn is None:
        return 0.0
    gap = fn()
    within_fn = getattr(md_service, "within_reconnect_window", None)
    if within_fn is not None and not within_fn():
        return 0.0
    return float(gap)
```

- [ ] **Step 2: Add `check_facade_health()` call to monitor loop**

In `src/hft_platform/services/system.py`, in the monitor loop (around line 640, after `update_system_metrics`), add:

```python
            # Per-facade health check (QuoteConnectionPool isolation)
            client = getattr(self.md_service, "client", None)
            if client is not None and hasattr(client, "check_facade_health"):
                client.check_facade_health()
```

- [ ] **Step 3: Inject lob + fe into Pool in bootstrap.py**

In `src/hft_platform/services/bootstrap.py`, after the pool creation block (around line 598-605), find where `ServiceRegistry` is built and the LOB/FeatureEngine are available. Add after the registry is wired:

```python
# Inject LOB + FeatureEngine into Pool for targeted warmup reset
if hasattr(quote_client, "set_reset_targets"):
    quote_client.set_reset_targets(
        lob=registry.lob_engine,
        feature_engine=registry.feature_engine,
    )
```

Read the `build()` method to find the exact location where `registry.lob_engine` and `registry.feature_engine` are available.

- [ ] **Step 4: Remove global LOB/Feature reset from _md_reconnect.py**

In `src/hft_platform/services/_md_reconnect.py`, lines 160-168, remove the unconditional LOB/Feature reset block. The Pool's `reconnect()` now handles targeted reset via `_notify_warmup_reset`. Change:

```python
        # Always clear stale LOB/feature state after any reconnect attempt —
        # even partial success leaves some facades with fresh data flowing into
        # stale BookState objects.
        lob = getattr(self, "lob", None)
        if lob is not None and hasattr(lob, "reset_books"):
            lob.reset_books()
        fe = getattr(self, "feature_engine", None)
        if fe is not None and hasattr(fe, "reset_all"):
            fe.reset_all()
```

To:

```python
        # Per-facade LOB/Feature reset is handled by QuoteConnectionPool._notify_warmup_reset
        # when pool-based reconnect is active. Only fall back to global reset for single-client mode.
        client = getattr(self, "client", None)
        if not hasattr(client, "get_healthy_feed_gap_s"):
            lob = getattr(self, "lob", None)
            if lob is not None and hasattr(lob, "reset_books"):
                lob.reset_books()
            fe = getattr(self, "feature_engine", None)
            if fe is not None and hasattr(fe, "reset_all"):
                fe.reset_all()
```

- [ ] **Step 5: Run existing tests to verify no regression**

Run: `uv run pytest tests/unit/ -x -q --timeout=30`
Expected: No new failures

- [ ] **Step 6: Commit**

```bash
git add src/hft_platform/services/system.py src/hft_platform/services/bootstrap.py src/hft_platform/services/_md_reconnect.py
git commit -m "feat(pool): wire per-facade health check into StormGuard and bootstrap"
```

---

### Task 6: Per-facade metrics

**Files:**
- Modify: `src/hft_platform/feed_adapter/shioaji/quote_connection_pool.py` (update_metrics)

- [ ] **Step 1: Update `update_metrics` in QuoteConnectionPool**

Find the existing `update_metrics` method and extend it to emit per-slot state and feed gap:

```python
def update_metrics(self) -> None:
    _ensure_metrics()
    now = time.monotonic()
    for slot in self._slots:
        cid = str(slot.conn_id)
        if _METRIC_SUBSCRIBED is not None:
            _METRIC_SUBSCRIBED.labels(conn_id=cid).set(
                getattr(slot.facade, "subscribed_count", 0)
            )
        if _METRIC_LOGGED_IN is not None:
            _METRIC_LOGGED_IN.labels(conn_id=cid).set(1 if slot.facade.logged_in else 0)
        if _METRIC_LAST_DATA_AGE is not None:
            _METRIC_LAST_DATA_AGE.labels(conn_id=cid).set(now - slot.last_data_mono)
        if _METRIC_CONN_STATE is not None:
            _METRIC_CONN_STATE.labels(conn_id=cid).set(int(slot.state))
```

Add `_METRIC_CONN_STATE` to the module-level metrics setup:

```python
_METRIC_CONN_STATE = None

def _ensure_metrics() -> None:
    global _METRIC_SUBSCRIBED, _METRIC_LOGGED_IN, _METRIC_LAST_DATA_AGE, _METRIC_CONN_STATE
    if Gauge is None or _METRIC_SUBSCRIBED is not None:
        return
    # ... existing metrics ...
    _METRIC_CONN_STATE = Gauge(
        "hft_quote_conn_state",
        "Connection state per quote connection (0=connected, 1=degraded, 2=recovering, 3=disconnected)",
        ["conn_id"],
    )
```

- [ ] **Step 2: Run full test suite**

Run: `uv run pytest tests/unit/ -x -q --timeout=30`
Expected: PASS

- [ ] **Step 3: Commit**

```bash
git add src/hft_platform/feed_adapter/shioaji/quote_connection_pool.py
git commit -m "feat(pool): add per-facade connection state metric"
```

---

### Task 7: End-to-end integration test

**Files:**
- Test: `tests/integration/test_pool_isolation_e2e.py`

- [ ] **Step 1: Write E2E test simulating single-facade failure**

```python
# tests/integration/test_pool_isolation_e2e.py
"""E2E test: single facade failure should not trigger platform-wide STORM."""
import time
from unittest.mock import MagicMock, patch

from hft_platform.feed_adapter.shioaji.facade_slot import FacadeSlot, FacadeState
from hft_platform.feed_adapter.shioaji.pool_health import check_facade_health, get_healthy_feed_gap_s


class TestSingleFacadeFailureIsolation:
    """Simulates the production scenario: 1 of 4 connections drops."""

    def test_single_drop_does_not_affect_healthy_gap(self):
        """Core assertion: healthy facades' gap stays low when one facade dies."""
        slots = []
        for i in range(4):
            slot = FacadeSlot(conn_id=i, facade=MagicMock(), symbols={f"S{i}"})
            slot.last_data_mono = time.monotonic()
            slots.append(slot)

        # Simulate: conn_2 stops receiving data 8s ago
        slots[2].last_data_mono = time.monotonic() - 8.0

        # First health check: conn_2 should go DEGRADED
        check_facade_health(
            slots,
            degraded_threshold_s=3.0,
            reconnect_trigger_s=10.0,
            schedule_fn=lambda _: None,
        )
        assert slots[0].state == FacadeState.CONNECTED
        assert slots[1].state == FacadeState.CONNECTED
        assert slots[2].state == FacadeState.DEGRADED
        assert slots[3].state == FacadeState.CONNECTED

        # StormGuard feed gap should be ~0s (only healthy facades)
        gap = get_healthy_feed_gap_s(slots)
        assert gap < 1.0, f"Healthy gap should be <1s, got {gap}"

    def test_all_drop_returns_inf(self):
        """Safety net: all facades down returns inf for HALT."""
        slots = []
        for i in range(4):
            slot = FacadeSlot(conn_id=i, facade=MagicMock(), symbols={f"S{i}"})
            slot.last_data_mono = time.monotonic() - 10.0
            slots.append(slot)

        check_facade_health(
            slots,
            degraded_threshold_s=3.0,
            reconnect_trigger_s=10.0,
            schedule_fn=lambda _: None,
        )

        gap = get_healthy_feed_gap_s(slots)
        assert gap == float("inf")

    def test_reconnect_only_targets_failed_facade(self):
        """Only the failed facade should be reconnected."""
        slots = []
        for i in range(4):
            facade = MagicMock()
            facade.reconnect.return_value = True
            facade.logged_in = True
            slot = FacadeSlot(conn_id=i, facade=facade, symbols={f"S{i}"})
            slot.last_data_mono = time.monotonic()
            slots.append(slot)

        # Mark conn_1 as degraded
        slots[1].state = FacadeState.DEGRADED

        # Simulate pool.reconnect() behavior: only reconnect non-CONNECTED
        for slot in slots:
            if slot.state != FacadeState.CONNECTED:
                slot.facade.reconnect(reason="test")
                slot.state = FacadeState.CONNECTED

        # Verify: only conn_1 was reconnected
        slots[0].facade.reconnect.assert_not_called()
        slots[1].facade.reconnect.assert_called_once()
        slots[2].facade.reconnect.assert_not_called()
        slots[3].facade.reconnect.assert_not_called()
```

- [ ] **Step 2: Run test**

Run: `uv run pytest tests/integration/test_pool_isolation_e2e.py -v`
Expected: PASS (3 tests)

- [ ] **Step 3: Commit**

```bash
git add tests/integration/test_pool_isolation_e2e.py
git commit -m "test(pool): add E2E test for single-facade failure isolation"
```
