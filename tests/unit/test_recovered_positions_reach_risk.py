"""A position carried across a restart is visible to valuation, the exit and FORCE_FLAT.

Startup recovery parks each carried lot in ``PositionStore._recovery_positions``
until its first fill. Mark-to-market, the platform loss exit and FORCE_FLAT
sizing all read ``positions`` alone, so a recovered lot had no PnL, no exit
trigger and could not be flattened ("already flat") -- for good, when the
strategy had rolled to the next month and would never trade it again
(THESHOW 2026-10-21: TMFJ6 carried into settlement while R47 quoted TMFK6).
"""

from __future__ import annotations

import datetime as dt
from types import SimpleNamespace
from unittest import mock
from unittest.mock import MagicMock

import pytest
from prometheus_client import REGISTRY, generate_latest

from hft_platform.contracts.strategy import IntentType, Side
from hft_platform.execution.mtm import MarkToMarketCalculator
from hft_platform.execution.positions import PositionStore
from hft_platform.observability.metrics import MetricsRegistry
from hft_platform.order.adapter import OrderAdapter
from hft_platform.risk.loss_exit import UNATTRIBUTED_STRATEGY_ID, LossExitController, OpenPosition
from hft_platform.risk.validators import DailyLossLimitValidator, ExpiringContractValidator
from hft_platform.services.system import HFTSystem

SCALE = 10_000
TPE = dt.timezone(dt.timedelta(hours=8))
BID, ASK = 46_600 * SCALE, 46_602 * SCALE


def _tpe(y: int, mo: int, d: int, h: int, mi: int) -> int:
    return int(dt.datetime(y, mo, d, h, mi, tzinfo=TPE).timestamp()) * 1_000_000_000


@pytest.fixture()
def store(monkeypatch: pytest.MonkeyPatch) -> PositionStore:
    meta = MagicMock()
    meta.price_scale.return_value = SCALE
    meta.contract_multiplier.return_value = 10
    meta.tick_size_scaled.return_value = SCALE
    monkeypatch.setattr("hft_platform.execution.positions.SymbolMetadata", lambda *a, **kw: meta)
    provider = MagicMock()
    provider.scale_for.return_value = SCALE
    monkeypatch.setattr("hft_platform.execution.positions.SymbolMetadataPriceScaleProvider", lambda *a, **kw: provider)
    monkeypatch.setattr("hft_platform.execution.positions._RustPositionTracker", None)
    s = PositionStore()
    s._rust_tracker = None
    return s


def _recover(store: PositionStore, *, symbol: str = "TMFJ6", qty: int = 1, entry: int = 47_426, strategy: str = "R47"):
    store.load_recovery(
        account_id="acc", symbol=symbol, net_qty=qty, avg_price_scaled=entry * SCALE, strategy_id=strategy
    )


def test_snapshot_open_positions_includes_an_untraded_recovered_lot(store):
    _recover(store)

    [(key, pos)] = store.snapshot_open_positions().items()

    assert key == "recovery:acc:R47:TMFJ6"
    assert (pos.symbol, pos.net_qty, pos.avg_price_scaled, pos.strategy_id) == ("TMFJ6", 1, 47_426 * SCALE, "R47")
    assert store.snapshot_positions() == {}  # the old view, unchanged


def test_mtm_values_a_recovered_lot(store):
    _recover(store)  # long 1 @ 47,426, mid 46,601: -825 pt x 10 NTD
    snap = MarkToMarketCalculator(store, lambda _s: (BID + ASK) // 2, lambda _s: 10).snapshot()

    assert snap.total_scaled == (46_601 - 47_426) * 10 * SCALE
    assert snap.complete


def test_mtm_counts_an_unknown_cost_basis_as_unpriced(store):
    store.load_recovery(account_id="acc", symbol="TMFJ6", net_qty=1, avg_price_scaled=-1, strategy_id="R47")
    snap = MarkToMarketCalculator(store, lambda _s: (BID + ASK) // 2, lambda _s: 10).snapshot()

    assert snap.total_scaled == 0
    assert not snap.complete
    assert snap.unpriced_symbols == ("TMFJ6",)


def test_force_flat_sizing_includes_a_recovered_lot(store):
    _recover(store, qty=-2)
    adapter = SimpleNamespace(position_store=store)

    assert OrderAdapter._platform_net_position_for_symbol(adapter, "TMFJ6") == -2


def _system(store: PositionStore, now: int) -> HFTSystem:
    cfg = {"intraday_pnl": {"hard_limit_ntd": 5000, "price_scale": SCALE}, "loss_exit": {"enabled": True}}
    sys = HFTSystem.__new__(HFTSystem)
    sys._loss_exit = LossExitController()
    sys.risk_engine = SimpleNamespace(
        config=cfg, validators=[DailyLossLimitValidator(cfg, None), ExpiringContractValidator(cfg, None)]
    )
    l1 = (now - 500_000_000, BID, ASK, BID + ASK, ASK - BID, 3, 3)
    sys.md_service = SimpleNamespace(lob=SimpleNamespace(get_l1_scaled=lambda _s: l1))
    sys.position_store = store
    sys.risk_queue = SimpleNamespace(items=[])
    sys.risk_queue.submit_nowait = sys.risk_queue.items.append
    return sys


def test_the_expiry_exit_closes_a_recovered_lot_in_a_month_the_strategy_left(store):
    """R47 quotes TMFK6; the TMFJ6 lot exists only as a recovery entry."""
    _recover(store, entry=46_600)
    now = _tpe(2026, 10, 21, 13, 5)
    sys = _system(store, now)

    with mock.patch("hft_platform.core.timebase.now_ns", return_value=now):
        sys._run_loss_exit()

    [intent] = sys.risk_queue.items
    assert (intent.intent_type, intent.symbol, intent.side) == (IntentType.FORCE_FLAT, "TMFJ6", Side.SELL)
    assert intent.strategy_id == "R47"
    assert intent.reason == "loss_exit:expiry"


def test_an_unknown_basis_skips_only_the_position_loss_trigger():
    ctrl = LossExitController()
    common = dict(
        position_loss_limit_scaled=5_000 * SCALE,
        expiry_enabled=True,
        l1=lambda _s: (BID, ASK),
        multiplier=lambda _s: 10,
        tick_size=lambda _s: SCALE,
    )
    short_unknown = [OpenPosition("", "TMFK6", -1, -1)]  # -1 would read as a 46,602 pt loss

    assert ctrl.plan(now_ns=_tpe(2026, 10, 16, 10, 0), positions=short_unknown, daily_halt=False, **common) == []
    [halted] = ctrl.plan(now_ns=_tpe(2026, 10, 16, 10, 1), positions=short_unknown, daily_halt=True, **common)
    assert halted.reason == "loss_exit:daily_loss_halt"
    assert halted.strategy_id == UNATTRIBUTED_STRATEGY_ID


def test_loss_exit_counters_are_exported_after_the_registry_is_rebuilt():
    import hft_platform.risk.loss_exit  # noqa: F401  -- imported before the registry, as in main.py

    MetricsRegistry._instance = None
    try:
        MetricsRegistry.get()
        text = generate_latest(REGISTRY).decode()
    finally:
        MetricsRegistry._instance = None
    assert "# HELP loss_exit_orders_total" in text
    assert "# HELP loss_exit_unpriced_total" in text
