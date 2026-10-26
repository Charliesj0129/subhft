"""Follow-ups from the Codex adversarial review of risk batches B0-B6 (2026-10-24).

1. A recovered lot's historical realized PnL reached the daily-loss stop again
   as today's PnL on its first fill: the router's pre-fill snapshot read
   ``positions`` only, while the fill seeded the lot -- realized PnL included --
   from ``_recovery_positions``.
2. After a restart restored the day's opening mark, the since-entry unrealized
   PnL was still 0 until the first mark-to-market tick, so an opening order in
   that window read the whole opening mark as today's loss and latched the stop.
4. The checkpoint identity was built in ``bootstrap.build()``, before the
   broker login, so every account shared the broker's identity.
"""

from __future__ import annotations

import asyncio
import hashlib
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from hft_platform.contracts.execution import FillEvent
from hft_platform.contracts.strategy import IntentType, OrderIntent, Side
from hft_platform.execution.normalizer import RawExecEvent
from hft_platform.execution.positions import PositionStore
from hft_platform.execution.router import ExecutionRouter
from hft_platform.risk.validators import DailyLossLimitValidator
from hft_platform.services.system import HFTSystem

SCALE = 10_000


def _ntd(n: int) -> int:
    return n * SCALE


# ---------------------------------------------------------------------------
# 1. A recovered lot's history is not today's PnL
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _isolate_dedup(monkeypatch, tmp_path):
    monkeypatch.setenv("HFT_FILL_DEDUP_PERSIST_PATH", str(tmp_path / "dedup.jsonl"))


@pytest.fixture(params=["python", "rust"])
def store(request, monkeypatch) -> PositionStore:
    meta = MagicMock()
    meta.price_scale.return_value = SCALE
    meta.contract_multiplier.return_value = 10
    monkeypatch.setattr("hft_platform.execution.positions.SymbolMetadata", lambda *a, **kw: meta)
    provider = MagicMock()
    provider.scale_for.return_value = SCALE
    monkeypatch.setattr("hft_platform.execution.positions.SymbolMetadataPriceScaleProvider", lambda *a, **kw: provider)
    s = PositionStore()
    if request.param == "python":
        s._rust_tracker = None
    elif s._rust_tracker is None:
        pytest.skip("rust_core not built")
    return s


def _close_at_entry(entry: int) -> FillEvent:
    return FillEvent(
        fill_id="d1",
        account_id="acc",
        order_id="o1",
        strategy_id="R47",
        symbol="TMFJ6",
        side=Side.SELL,
        qty=1,
        price=entry,
        fee=0,
        tax=0,
        ingest_ts_ns=1,
        match_ts_ns=1,
    )


async def _route_one(router: ExecutionRouter, store: PositionStore, key: str) -> None:
    router.raw_queue.put_nowait(RawExecEvent("deal", {"payload": {}}, 0))
    task = asyncio.create_task(router.run())
    for _ in range(400):  # bounded: 400 x 5 ms
        if key in store.positions:
            break
        await asyncio.sleep(0.005)
    router.running = False
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass


def test_a_recovered_lots_history_is_not_booked_as_todays_pnl_on_its_first_fill(store):
    entry = _ntd(47_426)
    store.load_recovery(
        account_id="acc",
        symbol="TMFJ6",
        net_qty=1,
        avg_price_scaled=entry,
        realized_pnl_scaled=_ntd(20_000),  # realized before the restart
        strategy_id="R47",
    )
    risk_engine = MagicMock()
    router = ExecutionRouter(
        bus=MagicMock(),
        raw_queue=asyncio.Queue(),
        order_id_map={},
        position_store=store,
        terminal_handler=MagicMock(),
        risk_engine=risk_engine,
    )
    router.normalizer.normalize_fill = MagicMock(return_value=_close_at_entry(entry))

    asyncio.run(_route_one(router, store, "acc:R47:TMFJ6"))

    assert store.positions["acc:R47:TMFJ6"].net_qty == 0
    # Closed at its entry price with no fee: today's PnL is zero.
    booked = [c.args[1] for c in risk_engine.notify_fill_pnl.call_args_list]
    assert sum(booked) == 0, booked


def test_the_realized_baseline_includes_the_recovered_lot_the_fill_will_take(store):
    store.load_recovery(
        account_id="acc", symbol="TMFJ6", net_qty=1, avg_price_scaled=_ntd(47_426), realized_pnl_scaled=_ntd(7)
    )
    assert store.realized_before_fill(_close_at_entry(_ntd(47_426))) == _ntd(7)
    assert "acc:TMFJ6" in store._recovery_positions  # a peek, not a take


# ---------------------------------------------------------------------------
# 2. No mark yet after a restore is not a loss
# ---------------------------------------------------------------------------


def _validator() -> DailyLossLimitValidator:
    cfg = {
        "intraday_pnl": {
            "scope": "global",
            "soft_limit_ntd": 3000,
            "hard_limit_ntd": 5000,
            "price_scale": SCALE,
        },
    }
    return DailyLossLimitValidator(cfg, None)


def _open() -> OrderIntent:
    return OrderIntent(
        intent_id=1,
        strategy_id="R47",
        symbol="TMFK6",
        intent_type=IntentType.NEW,
        side=Side.BUY,
        price=_ntd(22_000),
        qty=1,
    )


def _restored_with_day_open(mark: int) -> DailyLossLimitValidator:
    before = _validator()
    before.update_unrealized(mark, complete=True)  # the day's opening mark
    after = _validator()
    assert after.restore_state(before.export_state())
    return after


def test_an_opening_order_before_the_first_mark_after_a_restore_does_not_latch_the_stop():
    validator = _restored_with_day_open(_ntd(20_000))

    ok, reason = validator.check(_open())

    assert validator.halt_triggered is False
    assert ok is False and reason == "DAILY_LOSS_AWAITING_MARK"


def test_the_first_mark_after_a_restore_reopens_trading_on_the_real_number():
    validator = _restored_with_day_open(_ntd(20_000))

    validator.update_unrealized(_ntd(20_000), complete=True)  # unchanged since the open

    assert validator.check(_open()) == (True, "OK")
    assert validator.halt_triggered is False


def test_a_real_loss_after_a_restore_still_latches_the_stop():
    validator = _restored_with_day_open(_ntd(20_000))

    validator.update_unrealized(_ntd(14_000), complete=True)  # 6,000 NTD down on the day

    assert validator.halt_triggered is True


# ---------------------------------------------------------------------------
# 4. The checkpoint names the account that logged in
# ---------------------------------------------------------------------------


def _digest(account: str) -> str:
    return hashlib.sha256(account.encode("utf-8")).hexdigest()[:16]


def _system_for_identity(account_id: str) -> HFTSystem:
    system = HFTSystem.__new__(HFTSystem)
    system.order_client = SimpleNamespace(get_default_account_id=lambda: account_id)
    system.registry = SimpleNamespace(broker_id="shioaji")
    system.checkpoint_writer = SimpleNamespace(identity={"broker": "shioaji", "account": _digest("shioaji")})
    system.startup_verifier = SimpleNamespace(identity={"broker": "shioaji", "account": _digest("shioaji")})
    return system


def test_after_login_the_checkpoint_identity_names_the_logged_in_account(monkeypatch):
    monkeypatch.setenv("HFT_ORDER_MODE", "sim")
    system = _system_for_identity("F1234567")

    assert system._bind_checkpoint_identity() is True

    expected = {"broker": "shioaji", "account": _digest("F1234567"), "venue": "paper"}
    assert system.checkpoint_writer.identity == expected
    assert system.startup_verifier.identity == expected


def test_no_account_after_login_refuses_to_vouch_for_any_checkpoint(monkeypatch):
    from hft_platform.execution.checkpoint import checkpoint_identity_mismatch

    monkeypatch.setenv("HFT_ORDER_MODE", "sim")
    system = _system_for_identity("")

    assert system._bind_checkpoint_identity() is False

    shared = {"identity": {"broker": "shioaji", "account": _digest("shioaji"), "venue": "paper"}}
    assert checkpoint_identity_mismatch(shared, system.startup_verifier.identity) == ["account"]
