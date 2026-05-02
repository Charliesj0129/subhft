"""Unit 1: Test ExecutionRouter PnL bridge."""

import asyncio
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from hft_platform.execution.router import ExecutionRouter


@pytest.fixture(autouse=True)
def _isolate_dedup(monkeypatch, tmp_path):
    """Prevent cross-test dedup state pollution from persisted fill_dedup_window."""
    monkeypatch.setenv("HFT_FILL_DEDUP_PERSIST_PATH", str(tmp_path / "dedup.jsonl"))


def _make_router(*, risk_engine=None):
    bus = MagicMock()
    bus.publish_many_nowait = MagicMock()
    raw_queue = asyncio.Queue()
    position_store = MagicMock(spec=["positions", "on_fill"])
    position_store.positions = {}
    position_store.on_fill = MagicMock()
    terminal_handler = MagicMock()
    return ExecutionRouter(
        bus=bus,
        raw_queue=raw_queue,
        order_id_map={},
        position_store=position_store,
        terminal_handler=terminal_handler,
        risk_engine=risk_engine,
    )


def test_notify_fill_pnl_calls_record_pnl(tmp_path):
    cfg = tmp_path / "risk.yaml"
    cfg.write_text("risk:\n  max_order_size: 10\n")
    from hft_platform.risk.engine import RiskEngine

    engine = RiskEngine(str(cfg), asyncio.Queue(), asyncio.Queue())
    from hft_platform.risk.validators import DailyLossLimitValidator

    dll = next((v for v in engine.validators if isinstance(v, DailyLossLimitValidator)), None)
    assert dll is not None
    engine.notify_fill_pnl("strat_a", -50_000)
    assert dll._accumulated_loss.get("strat_a") == -50_000


@pytest.mark.asyncio
async def test_router_calls_notify_fill_pnl_on_fill():
    risk_engine = MagicMock()
    router = _make_router(risk_engine=risk_engine)
    fill_norm = SimpleNamespace(
        account_id="a", strategy_id="s", symbol="X", order_id="o", fill_id="f", price=100, qty=1, side=1, fee=0, tax=0
    )
    router.normalizer.normalize_fill = MagicMock(return_value=fill_norm)
    delta = SimpleNamespace(realized_pnl=5000)
    router.position_store.on_fill = MagicMock(return_value=delta)
    router.position_store.positions = {}
    from hft_platform.execution.normalizer import RawExecEvent

    router.raw_queue.put_nowait(RawExecEvent("deal", {"payload": {}}, 0))
    task = asyncio.create_task(router.run())
    await asyncio.sleep(0.05)
    router.running = False
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass
    risk_engine.notify_fill_pnl.assert_called_once_with("s", 5000)
