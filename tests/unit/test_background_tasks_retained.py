"""Fire-and-forget tasks are held until they finish.

``asyncio`` keeps only a weak reference to a task. A task parked on a future that nothing else references forms an
unreachable cycle (task <-> future <-> coroutine), so the garbage collector destroys it and closes its coroutine at
the ``await``: a FORCE_FLAT dispatch, a HALT or daily-loss notification then silently never completes.

The hazard is reproduced with a coroutine that waits on exactly such a future and records when it is torn down.
Each call site that used to drop its task is driven with that coroutine as the work; after ``gc.collect()`` the work
must still be pending. ``test_no_task_result_is_discarded_in_src`` is the structural guard for the sites that are
not reachable from a unit test (the FORCE_FLAT closure in bootstrap, the contract-refresh alert).
"""

from __future__ import annotations

import ast
import asyncio
import gc
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from hft_platform.core import background_tasks
from hft_platform.core.background_tasks import retained_count, spawn_retained
from hft_platform.observability.metrics import MetricsRegistry


class _Parked:
    """Work that waits on a future only its own task can reach, and records if it is torn down."""

    def __init__(self) -> None:
        self.started = 0
        self.torn_down = 0

    async def run(self, *_args, **_kwargs) -> None:
        self.started += 1
        try:
            await asyncio.get_running_loop().create_future()
        finally:
            self.torn_down += 1


async def _settle() -> None:
    for _ in range(3):
        await asyncio.sleep(0)


def _collect() -> None:
    for _ in range(3):
        gc.collect()


async def _drain_retained() -> None:
    for task in list(background_tasks._RETAINED):
        task.cancel()
    await _settle()


def _failed(name: str) -> float:
    return MetricsRegistry.get().background_task_failed_total.labels(name=name)._value.get()


async def _assert_held_after_gc(parked: _Parked, *, started: int = 1) -> None:
    await _settle()
    assert parked.started == started
    _collect()
    assert parked.torn_down == 0, "the task was garbage collected while it was still running"
    assert retained_count() >= started
    await _drain_retained()


# ── the helper ───────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_a_bare_task_parked_on_an_unreferenced_future_is_collected():
    """Control: the hazard exists in this interpreter, so the tests below prove something."""
    parked = _Parked()
    asyncio.get_running_loop().create_task(parked.run())  # result dropped, as the call sites used to do
    await _settle()
    assert parked.started == 1

    _collect()

    assert parked.torn_down == 1


@pytest.mark.asyncio
async def test_a_retained_task_survives_garbage_collection_while_it_waits():
    parked = _Parked()
    spawn_retained(parked.run(), name="unit_parked")

    await _assert_held_after_gc(parked)


@pytest.mark.asyncio
async def test_the_reference_is_dropped_when_the_task_finishes():
    before = retained_count()

    async def quick() -> None:
        return None

    task = spawn_retained(quick(), name="unit_quick")
    assert retained_count() == before + 1

    await task
    await _settle()

    assert retained_count() == before


@pytest.mark.asyncio
async def test_a_failed_task_is_counted_and_released():
    async def boom() -> None:
        raise RuntimeError("notify failed")

    before = _failed("unit_boom")
    task = spawn_retained(boom(), name="unit_boom", track="futures_day")

    await asyncio.gather(task, return_exceptions=True)
    await _settle()

    assert _failed("unit_boom") == before + 1
    assert task not in background_tasks._RETAINED


@pytest.mark.asyncio
async def test_a_cancelled_task_is_not_counted_as_a_failure():
    parked = _Parked()
    before = _failed("unit_cancelled")
    task = spawn_retained(parked.run(), name="unit_cancelled")
    await _settle()

    task.cancel()
    await asyncio.gather(task, return_exceptions=True)
    await _settle()

    assert _failed("unit_cancelled") == before
    assert task not in background_tasks._RETAINED
    assert parked.torn_down == 1


def test_without_a_running_loop_it_raises_like_create_task():
    async def work() -> None:
        return None

    coro = work()
    try:
        with pytest.raises(RuntimeError):
            spawn_retained(coro, name="unit_no_loop")
    finally:
        coro.close()


def test_a_task_is_scheduled_on_the_loop_it_is_given():
    loop = asyncio.new_event_loop()
    try:
        done: list[int] = []

        async def work() -> None:
            done.append(1)

        task = spawn_retained(work(), name="unit_given_loop", loop=loop)
        loop.run_until_complete(task)

        assert done == [1]
        assert retained_count() == 0 or task not in background_tasks._RETAINED
    finally:
        loop.close()


# ── the call sites ───────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_the_halt_notification_task_survives_garbage_collection():
    from hft_platform.risk.storm_guard import StormGuard
    from hft_platform.services.bootstrap import _make_halt_notification_callback

    parked = _Parked()
    guard = StormGuard()
    guard.metrics = MagicMock()
    guard.bind_loop(asyncio.get_running_loop())
    callback = _make_halt_notification_callback(guard, SimpleNamespace(notify_halt=parked.run))

    callback()  # on the loop thread: the "direct" path

    await _assert_held_after_gc(parked)


@pytest.mark.asyncio
async def test_the_flatten_failure_notification_survives_garbage_collection(tmp_path):
    from hft_platform.ops.session_governor import SessionGovernor
    from tests.unit.test_session_governor import _write_config

    parked = _Parked()
    governor = SessionGovernor(
        config_path=_write_config(tmp_path),
        notification_dispatcher=SimpleNamespace(notify_flatten_result=parked.run),
    )

    async def failing_flatten() -> None:
        raise RuntimeError("broker down")

    task = asyncio.get_running_loop().create_task(failing_flatten())
    task._flatten_retry_count = 0  # type: ignore[attr-defined]
    task._flatten_track_name = "futures_day"  # type: ignore[attr-defined]
    await asyncio.gather(task, return_exceptions=True)

    governor._on_flatten_task_done(task)

    await _assert_held_after_gc(parked)


@pytest.mark.asyncio
async def test_the_daily_report_task_survives_garbage_collection():
    from hft_platform.ops.session_governor import SessionPhase
    from hft_platform.services.daily_report import DailyReportService

    parked = _Parked()

    class _Service(DailyReportService):
        async def on_session_closed(self, track: str) -> None:
            await parked.run()

    service = _Service(
        ch_client=None,
        notification_dispatcher=None,
        evidence_writer=None,
        position_store=None,
        storm_guard=None,
    )

    service.on_phase_transition("futures_day", SessionPhase.FORCE_FLAT, SessionPhase.CLOSED)

    await _assert_held_after_gc(parked)


@pytest.mark.asyncio
async def test_the_daily_loss_notifications_survive_garbage_collection():
    from hft_platform.risk.validators import DailyLossLimitValidator
    from tests.unit.test_risk_engine_enforcement_gaps import _BASE_CONFIG, _engine

    parked = _Parked()
    dispatcher = SimpleNamespace(notify_daily_loss=parked.run, notify_halt=parked.run)
    engine, _ = _engine(_BASE_CONFIG, notification_dispatcher=dispatcher)
    validator = next(v for v in engine.validators if isinstance(v, DailyLossLimitValidator))
    validator.halt_triggered = True

    engine._check_daily_loss_halt()

    await _assert_held_after_gc(parked, started=2)


@pytest.mark.asyncio
async def test_the_canary_notification_survives_garbage_collection():
    from hft_platform.alpha.canary_scheduler import CanaryAutoScheduler

    parked = _Parked()
    monitor = MagicMock()
    monitor.load_active_canaries.return_value = [{"alpha_id": "r47"}]
    monitor.evaluate.return_value = SimpleNamespace(state="rolled_back", reason="sharpe drop")
    scheduler = CanaryAutoScheduler(
        monitor,
        interval_s=60.0,
        dry_run=False,
        notification_dispatcher=SimpleNamespace(notify_canary_action=parked.run),
    )
    scheduler._build_metrics = lambda _canary: {}  # type: ignore[method-assign]

    await scheduler.evaluate_all()

    await _assert_held_after_gc(parked)


@pytest.mark.asyncio
async def test_the_subscription_truncation_alert_survives_garbage_collection(monkeypatch):
    from hft_platform.feed_adapter.shioaji.subscription_manager import SubscriptionManager
    from tests.unit.feed_adapter.shioaji.test_subscribe_basket_truncate import (
        _ClientStub,
        _make_symbols,
        _patched_subscribe_symbol,
    )

    parked = _Parked()
    client = _ClientStub(
        symbols=_make_symbols(200),
        max_subscriptions=120,
        notification_dispatcher=SimpleNamespace(notify_subscription_truncated=parked.run),
    )
    client.conn_id = "0"
    manager = SubscriptionManager(client)  # type: ignore[arg-type]
    monkeypatch.setattr(SubscriptionManager, "_subscribe_symbol", _patched_subscribe_symbol)

    manager.subscribe_basket(cb=lambda *a, **kw: None)

    await _assert_held_after_gc(parked)


# ── the sites a unit test cannot reach ───────────────────────────────────


def _discarded_task_calls(source_root: Path) -> list[str]:
    hits: list[str] = []
    for path in sorted(source_root.rglob("*.py")):
        tree = ast.parse(path.read_text())
        for node in ast.walk(tree):
            if not (isinstance(node, ast.Expr) and isinstance(node.value, ast.Call)):
                continue
            func = node.value.func
            name = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", "")
            if name in ("create_task", "ensure_future"):
                hits.append(f"{path.relative_to(source_root)}:{node.lineno} {name}")
    return hits


def test_no_task_result_is_discarded_in_src():
    """A statement that schedules a task and drops it is the hazard itself: use ``spawn_retained``."""
    assert _discarded_task_calls(Path(background_tasks.__file__).parent.parent) == []


def test_the_guard_sees_a_discarded_task(tmp_path):
    (tmp_path / "bad.py").write_text(
        "import asyncio\n\nasync def f(loop, coro):\n    loop.create_task(coro)\n    task = loop.create_task(coro)\n"
    )

    assert _discarded_task_calls(tmp_path) == ["bad.py:4 create_task"]
