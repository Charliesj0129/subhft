"""The supervisor submits the exits, and they get through a halted risk engine.

The exit is only worth having if its orders pass the gates that froze the book:
StormGuard HALT, the daily-loss validator and the expiry validator.
"""

from __future__ import annotations

import asyncio
import datetime as dt
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import pytest

from hft_platform.contracts.strategy import IntentType, OrderIntent, Side
from hft_platform.execution.positions import Position
from hft_platform.risk.engine import RiskEngine
from hft_platform.risk.loss_exit import LossExitController
from hft_platform.risk.storm_guard import StormGuard, StormGuardState
from hft_platform.risk.validators import DailyLossLimitValidator, ExpiringContractValidator
from hft_platform.services.system import HFTSystem

PROD_LIMITS = Path(__file__).resolve().parents[2] / "config" / "env" / "prod" / "strategy_limits.yaml"
TPE = dt.timezone(dt.timedelta(hours=8))
SCALE = 10_000
BID, ASK = 46_600 * SCALE, 46_602 * SCALE


def _tpe(y: int, mo: int, d: int, h: int, mi: int) -> int:
    return int(dt.datetime(y, mo, d, h, mi, tzinfo=TPE).timestamp()) * 1_000_000_000


NOW = _tpe(2026, 10, 16, 10, 51)


class _Queue:
    def __init__(self, fail: bool = False) -> None:
        self.items: list[OrderIntent] = []
        self._fail = fail

    def submit_nowait(self, item: OrderIntent) -> None:
        if self._fail:
            raise asyncio.QueueFull
        self.items.append(item)


def _system(*, halted: bool, enabled: bool = True, book_age_s: float = 0.5, queue: _Queue | None = None):
    cfg = {"intraday_pnl": {"hard_limit_ntd": 5000, "price_scale": 10000}, "loss_exit": {"enabled": enabled}}
    daily = DailyLossLimitValidator(cfg, None)
    daily.halt_triggered = halted
    expiry = ExpiringContractValidator(cfg, None)
    sys = HFTSystem.__new__(HFTSystem)
    sys._loss_exit = LossExitController()
    sys.risk_engine = SimpleNamespace(config=cfg, validators=[daily, expiry])
    l1 = (NOW - int(book_age_s * 1e9), BID, ASK, BID + ASK, ASK - BID, 3, 3)
    sys.md_service = SimpleNamespace(lob=SimpleNamespace(get_l1_scaled=lambda _s: l1))
    pos = Position(
        account_id="acc", strategy_id="R47_MAKER_TMF", symbol="TMFJ6", net_qty=1, avg_price_scaled=47_426 * SCALE
    )
    sys.position_store = SimpleNamespace(
        snapshot_open_positions=lambda: {"acc:R47_MAKER_TMF:TMFJ6": pos},
        metadata=SimpleNamespace(contract_multiplier=lambda _s: 10, tick_size_scaled=lambda _s: SCALE),
    )
    sys.risk_queue = queue or _Queue()
    return sys


def _run(sys: HFTSystem) -> None:
    with mock.patch("hft_platform.core.timebase.now_ns", return_value=NOW):
        sys._run_loss_exit()


def test_a_latched_stop_with_an_open_lot_submits_one_force_flat():
    sys = _system(halted=True)
    _run(sys)

    [intent] = sys.risk_queue.items
    assert (intent.intent_type, intent.side, intent.symbol) == (IntentType.FORCE_FLAT, Side.SELL, "TMFJ6")
    assert intent.price == BID - 5 * SCALE


def test_the_config_switch_stops_it():
    sys = _system(halted=True, enabled=False)
    _run(sys)
    assert sys.risk_queue.items == []


def test_a_stale_book_is_not_a_price():
    sys = _system(halted=True, book_age_s=31)
    _run(sys)
    assert sys.risk_queue.items == []


def test_a_full_queue_is_reported_and_does_not_raise():
    sys = _system(halted=True, queue=_Queue(fail=True))
    _run(sys)  # must not raise into the supervisor loop
    assert sys.risk_queue.items == []


@pytest.fixture
def halted_engine():
    with mock.patch("hft_platform.core.timebase.now_ns", return_value=NOW):
        sg = StormGuard()
        engine = RiskEngine(
            str(PROD_LIMITS),
            asyncio.Queue(),
            asyncio.Queue(),
            storm_guard=sg,
            position_provider=lambda symbol, _sid=None: 1 if symbol in ("TMFJ6", "TMFK6") else 0,
        )
        sg.trigger_halt("DAILY_LOSS_LIMIT_EXCEEDED")
    assert sg.state == StormGuardState.HALT
    return engine


def test_the_exit_passes_a_halted_risk_engine_on_the_prod_limits(halted_engine):
    sys = _system(halted=True)
    _run(sys)
    [intent] = sys.risk_queue.items

    with mock.patch("hft_platform.core.timebase.now_ns", return_value=NOW):
        decision = halted_engine.evaluate(intent)

    assert decision.approved, decision.reason_code


def test_on_the_delivery_date_the_prod_limits_refuse_an_opener():
    """Flat book, NORMAL StormGuard: the only reason left to refuse is the expiry cutoff."""
    at_1305 = _tpe(2026, 10, 21, 13, 5)
    with mock.patch("hft_platform.core.timebase.now_ns", return_value=at_1305):
        engine = RiskEngine(
            str(PROD_LIMITS),
            asyncio.Queue(),
            asyncio.Queue(),
            storm_guard=StormGuard(),
            position_provider=lambda _symbol, _sid=None: 0,
        )

        def opener(symbol: str) -> OrderIntent:
            return OrderIntent(
                intent_id=1,
                strategy_id="R47_MAKER_TMF",
                symbol=symbol,
                intent_type=IntentType.NEW,
                side=Side.BUY,
                price=46_600 * SCALE,
                qty=1,
            )

        refused = engine.evaluate(opener("TMFJ6"))
        next_month = engine.evaluate(opener("TMFK6"))

    assert not refused.approved
    assert str(refused.reason_code).startswith("EXPIRY_CUTOFF")
    assert next_month.approved, next_month.reason_code
