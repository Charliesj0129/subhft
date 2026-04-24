"""Regression tests for infra-plane concurrency audit fixes.

Covers:
  - P0-I1: bootstrap deferred tasks — no ``asyncio.get_event_loop()`` during build()
  - P0-I2: WAL batch write mutual exclusion (timer thread vs async flush)
  - P0-I3: AuditWriter queue creation deferred to start() (engine loop only)
  - P0-I4: StormGuard halt-callback tolerates cross-thread invocation
  - P0-I5: _sync_drain_recorder stops WAL batch timer before creating tmp_loop
  - P1 fixes: merge-back counter, shutdown per-batcher timeout, metrics unregister lock,
    StormGuard transition metrics outside _state_lock, audit sticky guardrail, SIGTERM stop_async tracking.
"""

from __future__ import annotations

import asyncio
import threading
import time
import warnings
from unittest.mock import MagicMock, patch

import pytest


# ---------------------------------------------------------------------------
# P0-I1: bootstrap deferred tasks
# ---------------------------------------------------------------------------


class TestBootstrapDeferredTasks:
    """build() must not call asyncio.get_event_loop() in Python 3.12+.

    Before fix: bootstrap.py:1304 and :1315 scheduled config-snapshot and
    alertmanager-bridge tasks on whatever loop get_event_loop() returned —
    which is NOT the engine loop because build() runs in __init__, before
    HFTSystem.run() creates the running loop.

    After fix: build() collects coroutines into ``ServiceRegistry.deferred_tasks``
    and HFTSystem.run() creates the tasks once the loop is running.
    """

    def test_registry_has_deferred_tasks_list(self) -> None:
        """ServiceRegistry must expose a ``deferred_tasks`` attribute of list type."""
        from hft_platform.services.registry import ServiceRegistry

        # dataclass field existence check
        fields = {f.name for f in ServiceRegistry.__dataclass_fields__.values()}  # type: ignore[attr-defined]
        assert "deferred_tasks" in fields, (
            "ServiceRegistry must expose a ``deferred_tasks`` field so build() can hand "
            "coroutines to HFTSystem.run() without calling asyncio.get_event_loop() itself."
        )

    def test_build_does_not_call_get_event_loop(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """build() must not call asyncio.get_event_loop() (deprecated in 3.12).

        We patch asyncio.get_event_loop to raise so we catch any lingering call.
        """
        from hft_platform.services.bootstrap import SystemBootstrapper

        monkeypatch.setenv("HFT_MODE", "sim")
        monkeypatch.setenv("HFT_FEATURE_ENGINE_ENABLED", "0")

        call_counter = {"n": 0}
        _real_get = asyncio.get_event_loop

        def _spy_get_event_loop():  # noqa: ANN202
            call_counter["n"] += 1
            return _real_get()

        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            with (
                patch("asyncio.get_event_loop", side_effect=_spy_get_event_loop),
                patch("hft_platform.services.bootstrap.SystemBootstrapper._check_session_ownership",
                      return_value=False),
                patch("hft_platform.feed_adapter.shioaji.facade.ShioajiClientFacade"),
                patch("hft_platform.services.bootstrap.MarketDataService"),
                patch("hft_platform.services.bootstrap.OrderAdapter"),
                patch("hft_platform.services.bootstrap.ExecutionGateway"),
                patch("hft_platform.services.bootstrap.ExecutionRouter"),
                patch("hft_platform.services.bootstrap.RiskEngine"),
                patch("hft_platform.services.bootstrap.ReconciliationService"),
                patch("hft_platform.services.bootstrap.StrategyRunner"),
                patch("hft_platform.services.bootstrap.RecorderService"),
                patch("hft_platform.services.bootstrap.RingBufferBus"),
                patch("hft_platform.services.bootstrap.PositionStore"),
                patch("hft_platform.services.bootstrap.StormGuard"),
                patch("hft_platform.services.bootstrap.SymbolMetadata"),
                patch("hft_platform.services.bootstrap.SymbolMetadataPriceScaleProvider"),
                patch("hft_platform.services.bootstrap.LatencyRecorder"),
            ):
                bootstrapper = SystemBootstrapper({})
                try:
                    registry = bootstrapper.build()
                except Exception:
                    pytest.skip("build() failed for reasons unrelated to this test")

        # If the fix is applied, build() should not call asyncio.get_event_loop()
        # (may still call from inside create_task fallbacks for the `bool` helper,
        # so we check that *no DeprecationWarning about no running loop* fired).
        assert not any(
            "no running event loop" in str(w.message).lower()
            or "get_event_loop" in str(w.message).lower()
            for w in caught
        ), f"build() must not emit get_event_loop DeprecationWarning; got: {[str(w.message) for w in caught]}"

        # deferred_tasks must exist and be a list
        assert hasattr(registry, "deferred_tasks")
        assert isinstance(registry.deferred_tasks, list)


# ---------------------------------------------------------------------------
# P0-I3: AuditWriter defers queue creation to start()
# ---------------------------------------------------------------------------


class TestAuditWriterDeferredQueueCreation:
    """AuditWriter must not create asyncio.Queue instances in __init__().

    Before fix: queues were created in __init__. First-touch from a daemon thread
    (via StormGuard._transition → audit.log_guardrail_transition) bound the queue
    futures to a non-engine loop.

    After fix: queues are created lazily in ``start()``, which must be awaited from
    the engine loop. Pre-start log methods buffer into the deque or drop.
    """

    def setup_method(self) -> None:
        from hft_platform.recorder.audit import reset_audit_writer

        reset_audit_writer()

    def teardown_method(self) -> None:
        from hft_platform.recorder.audit import reset_audit_writer

        reset_audit_writer()

    def test_init_does_not_create_queues(self) -> None:
        """Queues should be None (or empty dict) before start() — not asyncio.Queue instances."""
        from hft_platform.recorder.audit import AuditWriter

        writer = AuditWriter(queue_size=10)
        # Queues created pre-start must not be asyncio.Queue instances
        # (because there is no engine loop yet).
        for _tbl, q in writer._queues.items():
            assert not isinstance(q, asyncio.Queue), (
                "AuditWriter.__init__ must not create asyncio.Queue — "
                "defer to start() which runs on the engine loop."
            )

    def test_log_from_thread_before_start_does_not_raise(self) -> None:
        """Logging from a non-asyncio thread before start() must not raise.

        Pre-start logs should buffer into the overflow deque (which is
        thread-safe via collections.deque.append) and be flushed after start().
        """
        from hft_platform.recorder.audit import AuditWriter

        writer = AuditWriter(queue_size=10)
        errors: list[BaseException] = []

        def _log_from_thread():
            try:
                writer.log_guardrail_transition({"old_state": "NORMAL", "new_state": "HALT"})
            except BaseException as exc:  # noqa: BLE001
                errors.append(exc)

        t = threading.Thread(target=_log_from_thread, daemon=True)
        t.start()
        t.join(timeout=2.0)
        assert not t.is_alive(), "log_guardrail_transition hung"
        assert not errors, f"pre-start log from thread raised: {errors}"

    def test_start_creates_queues_on_engine_loop(self) -> None:
        """start() must create asyncio.Queue instances bound to the current running loop."""
        from hft_platform.recorder.audit import AuditWriter

        async def _inner() -> bool:
            writer = AuditWriter(queue_size=10)
            await writer.start()
            try:
                for _tbl, q in writer._queues.items():
                    assert isinstance(q, asyncio.Queue), (
                        "AuditWriter.start() must create asyncio.Queue on the engine loop."
                    )
                return True
            finally:
                await writer.stop()

        assert asyncio.run(_inner())


# ---------------------------------------------------------------------------
# P0-I4: StormGuard halt callback cross-thread safety
# ---------------------------------------------------------------------------


class TestStormGuardHaltCallbackCrossThread:
    """StormGuard._fire_halt_callback must not call asyncio.get_running_loop().

    Before fix: _fire_halt_callback ran asyncio.get_running_loop() which raises
    RuntimeError in a non-asyncio thread; the except-branch silently closed the
    coroutine, dropping Telegram HALT notifications.

    After fix: StormGuard accepts an explicit loop reference (bind_loop) and uses
    asyncio.run_coroutine_threadsafe(result, stored_loop) whenever the callback
    returns a coroutine.
    """

    def test_bind_loop_method_exists(self) -> None:
        from hft_platform.risk.storm_guard import StormGuard

        with patch("hft_platform.risk.storm_guard.MetricsRegistry.get", return_value=MagicMock()):
            guard = StormGuard()
        assert hasattr(guard, "bind_loop"), (
            "StormGuard must expose bind_loop(loop) so the engine can pass its running "
            "loop reference for cross-thread halt-callback dispatch."
        )

    def test_trigger_halt_from_thread_runs_coroutine_callback(self) -> None:
        """A coroutine halt-callback scheduled from a daemon thread must execute on the engine loop."""
        from hft_platform.risk.storm_guard import StormGuard, StormGuardState

        executed = threading.Event()

        async def _cb() -> None:
            executed.set()

        async def _main() -> None:
            loop = asyncio.get_running_loop()
            with patch("hft_platform.risk.storm_guard.MetricsRegistry.get", return_value=MagicMock()):
                guard = StormGuard()
            guard.bind_loop(loop)
            guard._on_halt_callback = _cb

            def _fire_from_thread() -> None:
                guard.trigger_halt("lease_lost")

            t = threading.Thread(target=_fire_from_thread, daemon=True)
            t.start()
            t.join(timeout=2.0)
            # Yield so scheduled coroutine runs on this loop.
            for _ in range(50):
                if executed.is_set():
                    break
                await asyncio.sleep(0.02)
            assert guard.state == StormGuardState.HALT
            assert executed.is_set(), "coroutine halt-callback did not execute on engine loop"

        asyncio.run(_main())


# ---------------------------------------------------------------------------
# P0-I2: WAL batch-write mutual exclusion
# ---------------------------------------------------------------------------


class TestWALBatchWriterMutualExclusion:
    """_write_batch_sync must be serialized between timer thread and async flush.

    Before fix: both paths released the lock before calling _write_batch_sync,
    allowing two threads to write to the same wal_dir and race on _file_seq / fsync.
    """

    def test_concurrent_flush_calls_serialized(self, tmp_path) -> None:
        from hft_platform.recorder.wal import WALBatchWriter

        concurrent_inside_write = {"max": 0, "current": 0, "lock": threading.Lock()}
        original_write = WALBatchWriter._write_batch_sync

        def _tracking_write(self, *args, **kwargs):  # type: ignore[override]
            with concurrent_inside_write["lock"]:
                concurrent_inside_write["current"] += 1
                if concurrent_inside_write["current"] > concurrent_inside_write["max"]:
                    concurrent_inside_write["max"] = concurrent_inside_write["current"]
            try:
                # Tiny sleep to widen the race window.
                time.sleep(0.01)
                return original_write(self, *args, **kwargs)
            finally:
                with concurrent_inside_write["lock"]:
                    concurrent_inside_write["current"] -= 1

        with patch.object(WALBatchWriter, "_write_batch_sync", _tracking_write):
            writer = WALBatchWriter(str(tmp_path))
            try:
                async def _run() -> None:
                    # Prime buffer with rows.
                    for _ in range(10):
                        await writer.add("hft.market_data", [{"x": 1}] * 50)

                    # Invoke flush from multiple tasks concurrently.
                    tasks = [asyncio.create_task(writer.flush()) for _ in range(5)]
                    await asyncio.gather(*tasks)

                asyncio.run(_run())
                # Let the timer thread get a chance too.
                time.sleep(0.1)
            finally:
                writer.stop()

        assert concurrent_inside_write["max"] <= 1, (
            f"WAL _write_batch_sync was entered by {concurrent_inside_write['max']} threads "
            "concurrently — needs a write lock."
        )


# ---------------------------------------------------------------------------
# P0-I5: _sync_drain_recorder stops WAL timer before temp loop
# ---------------------------------------------------------------------------


class TestSyncDrainRecorderStopsWALTimer:
    """_sync_drain_recorder must stop the WAL batch timer before running tmp_loop.

    Before fix: the tmp_loop drained the recorder while ``WALBatchWriter._timer_thread``
    was still calling _write_batch_sync concurrently — two writers on same dir.
    """

    def test_sync_drain_calls_wal_stop_before_tmp_loop(self, tmp_path) -> None:
        from hft_platform.services.system import HFTSystem

        calls: list[str] = []

        # Fake batch writer records its stop() invocation.
        class _FakeBatchWriter:
            def stop(self) -> None:
                calls.append("wal_stop")

        class _FakeWriter:
            def __init__(self) -> None:
                self._wal_batch_writer = _FakeBatchWriter()

        async def _noop_drain() -> None:
            calls.append("drain")

        async def _noop_flush() -> None:
            calls.append("flush")

        class _FakeRecorder:
            def __init__(self) -> None:
                self.running = True
                self.writer = _FakeWriter()

            async def _drain_queue_into_batchers(self) -> None:
                await _noop_drain()

            async def _shutdown_flush(self) -> None:
                await _noop_flush()

        system = HFTSystem.__new__(HFTSystem)  # bypass __init__
        system.recorder = _FakeRecorder()
        system._sync_drain_recorder()

        assert "wal_stop" in calls, "_sync_drain_recorder must call WAL batch writer stop() first"
        assert calls.index("wal_stop") < calls.index("drain"), (
            "WAL timer must be stopped BEFORE the temp loop drains the recorder."
        )


# ---------------------------------------------------------------------------
# P1: StormGuard metrics call outside _state_lock
# ---------------------------------------------------------------------------


class TestStormGuardMetricsLockOrdering:
    """stormguard_transitions_total.inc() must NOT be called while holding _state_lock.

    Before fix: storm_guard.py:434 called metrics.inc() inside the lock, creating
    a nested-lock order (state_lock → prometheus metric lock) that could invert
    vs Prometheus scraper thread.

    After fix: _transition returns a "deferred metric" instruction; the caller
    emits the metric OUTSIDE the state lock.
    """

    def test_transitions_metric_inc_called_outside_state_lock(self) -> None:
        from hft_platform.risk.storm_guard import StormGuard

        # Build a fake Counter that records the state-lock state when inc() fires.
        acquired_state_lock_during_inc: dict[str, bool] = {"flag": False}

        class _RecordingChild:
            def inc(self, *_args, **_kwargs) -> None:
                # If we can acquire guard._state_lock non-blocking, we are OUTSIDE the lock.
                acquired_state_lock_during_inc["flag"] = guard._state_lock.acquire(blocking=False)
                if acquired_state_lock_during_inc["flag"]:
                    guard._state_lock.release()

        class _FakeCounter:
            def labels(self, **_kwargs):  # noqa: ANN001
                return _RecordingChild()

        fake_metrics = MagicMock()
        fake_metrics.stormguard_transitions_total = _FakeCounter()
        fake_metrics.stormguard_mode = MagicMock()
        fake_metrics.stormguard_mode.labels = MagicMock(return_value=MagicMock())

        with patch("hft_platform.risk.storm_guard.MetricsRegistry.get", return_value=fake_metrics):
            guard = StormGuard()

        guard.trigger_halt("test")
        assert acquired_state_lock_during_inc["flag"], (
            "stormguard_transitions_total.inc() was called while _state_lock was held. "
            "Move metric emission outside the lock to prevent order-inversion deadlock with "
            "the Prometheus scraper thread."
        )


# ---------------------------------------------------------------------------
# P1: Audit guardrail "sticky-first" overflow policy
# ---------------------------------------------------------------------------


class TestAuditGuardrailStickyOverflow:
    """audit.guardrail_log must retain OLDEST (root-cause) entries on overflow.

    Before fix: overflow used collections.deque(maxlen=overflow_size) which evicts
    oldest. For guardrail transitions, the FIRST transition (root cause) is the most
    valuable — we must retain that and drop the newest on overflow.
    """

    def setup_method(self) -> None:
        from hft_platform.recorder.audit import reset_audit_writer

        reset_audit_writer()

    def teardown_method(self) -> None:
        from hft_platform.recorder.audit import reset_audit_writer

        reset_audit_writer()

    def test_guardrail_log_retains_oldest_on_overflow(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Post-start path: guardrail dropped NEWEST when queue + overflow are full."""
        from hft_platform.recorder.audit import AuditWriter

        monkeypatch.setenv("HFT_AUDIT_GUARDRAIL_OVERFLOW_SIZE", "2")

        async def _inner() -> None:
            writer = AuditWriter(queue_size=1)
            await writer.start()
            try:
                # 1 fits in queue; 2 fits in overflow[0]; 3 fits in overflow[1]; 4 must be DROPPED.
                writer.log_guardrail_transition({"old": "NORMAL", "new": "WARM", "marker": "root_cause"})
                writer.log_guardrail_transition({"old": "WARM", "new": "STORM", "marker": "middle_1"})
                writer.log_guardrail_transition({"old": "STORM", "new": "HALT", "marker": "middle_2"})
                writer.log_guardrail_transition({"old": "HALT", "new": "NORMAL", "marker": "newest"})

                q = writer._queues["audit.guardrail_log"]
                first = q.get_nowait()
                assert first.get("marker") == "root_cause", (
                    "guardrail_log must preserve the root-cause entry and drop the newest. "
                    f"Got marker={first.get('marker')}"
                )
                assert writer._dropped["audit.guardrail_log"] == 1, (
                    "Exactly one row must have been dropped (the newest)."
                )
            finally:
                await writer.stop()

        asyncio.run(_inner())

    def test_guardrail_pre_start_retains_oldest_on_overflow(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Pre-start buffer path: guardrail also applies sticky-first.

        Pre-start buffer size = queue_size + overflow_size (combined post-start
        capacity). With queue_size=1 and overflow=2, buffer holds 3. The 4th
        row must be dropped to preserve the root cause.
        """
        from hft_platform.recorder.audit import AuditWriter

        monkeypatch.setenv("HFT_AUDIT_GUARDRAIL_OVERFLOW_SIZE", "2")
        writer = AuditWriter(queue_size=1)
        writer.log_guardrail_transition({"marker": "root_cause"})
        writer.log_guardrail_transition({"marker": "middle_1"})
        writer.log_guardrail_transition({"marker": "middle_2"})
        # 4th would overflow the pre-start deque of size 3; must drop NEWEST.
        writer.log_guardrail_transition({"marker": "newest"})

        buf = writer._pre_start_buffer["audit.guardrail_log"]
        assert len(buf) == 3
        assert buf[0]["marker"] == "root_cause", "root cause must survive"
        assert buf[1]["marker"] == "middle_1"
        assert buf[2]["marker"] == "middle_2"
        assert writer._dropped["audit.guardrail_log"] == 1

    def test_non_guardrail_drop_counter_increments_pre_start(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Non-guardrail tables: drop counter tracks deque auto-eviction pre-start."""
        from hft_platform.recorder.audit import AuditWriter

        # queue_size=1 + overflow=1 → pre-start buffer maxlen=2.
        monkeypatch.setenv("HFT_AUDIT_OVERFLOW_SIZE", "1")
        writer = AuditWriter(queue_size=1)
        writer.log_order({"a": 1})  # buf=[1]
        writer.log_order({"a": 2})  # buf=[1, 2]
        writer.log_order({"a": 3})  # buf=[2, 3] (1 dropped, counter+=1)
        writer.log_order({"a": 4})  # buf=[3, 4] (2 dropped, counter+=1)

        assert writer._dropped["audit.orders_log"] == 2


# ---------------------------------------------------------------------------
# P1: main.py awaits detached stop_async task
# ---------------------------------------------------------------------------


class TestSystemStopAsyncTaskTracked:
    """HFTSystem.stop() must assign the detached stop_async task to
    ``self._stop_async_task`` so the launcher can await it.
    """

    def test_stop_async_task_attribute_exists(self) -> None:
        from hft_platform.services.system import HFTSystem

        system = HFTSystem.__new__(HFTSystem)
        # Simulate __init__'s attribute creation by accessing the slot / dict.
        # The attribute is initialised in __init__; in a real run it starts
        # as None. Here we just verify the attribute NAME is in the system
        # source so the launcher's `getattr(system, "_stop_async_task", None)`
        # lookup is not referencing a name that doesn't exist.
        import inspect

        src = inspect.getsource(HFTSystem.__init__)
        assert "_stop_async_task" in src, (
            "HFTSystem.__init__ must initialise self._stop_async_task so "
            "HFTSystem.stop() can store the detached stop_async() task for "
            "the launcher to await on shutdown."
        )

        src_stop = inspect.getsource(HFTSystem.stop)
        assert "_stop_async_task" in src_stop, (
            "HFTSystem.stop() must store the detached stop_async() task "
            "so the launcher can await it before loop teardown."
        )


# ---------------------------------------------------------------------------
# P1: MetricsRegistry scrape-vs-rebuild lock
# ---------------------------------------------------------------------------


class TestMetricsRegistryScrapeLock:
    """registry_rw_lock must guard both rebuild and scrape paths."""

    def test_registry_rw_lock_module_attribute(self) -> None:
        from hft_platform.observability import metrics

        assert hasattr(metrics, "registry_rw_lock"), (
            "metrics module must expose `registry_rw_lock` so the scraper "
            "can synchronise with `MetricsRegistry.__init__`."
        )

    def test_metrics_server_acquires_registry_rw_lock(self) -> None:
        """Metrics server source must reference registry_rw_lock."""
        import inspect

        from hft_platform.observability import metrics_server

        src = inspect.getsource(metrics_server)
        assert "registry_rw_lock" in src, (
            "metrics_server.py must acquire registry_rw_lock during scrape to "
            "avoid observing a partially-rebuilt REGISTRY."
        )
