"""A position in a settled contract is not restored as if it were still open.

THESHOW, 2026-09-16 onwards, ``HFT_ORDER_MODE=sim``::

    checkpoint  TMFI6 +1   (R47 held it into the 13:30 final session)
    broker      TMFI6  0   (paper routing: the live account never saw it)
    09-16 13:30 TMFI6 settles, is delisted, and never quotes again

    restart  ->  _recover_dual: sim, broker 0, ckpt != 0  ->  "not comparable"
             ->  restored +1 unchanged, every restart, forever
             ->  mark-to-market incomplete (no price will ever arrive)
             ->  a daily stop can never release

Paper routing explains a broker 0 while the contract trades. Once the exchange
has settled it, broker 0 is simply true.
"""

from __future__ import annotations

from datetime import date
from typing import Any
from unittest.mock import patch

import pytest

from hft_platform.execution import startup_recon
from hft_platform.execution.positions import PositionStore
from hft_platform.execution.startup_recon import StartupPositionVerifier, startup_recon_not_comparable


class _EmptyBroker:
    def get_positions(self) -> list[Any]:
        return []


def _ckpt(**symbol_qty: int) -> dict[str, dict[str, Any]]:
    return {
        f"ACC1:R47_MAKER_TMF:{sym}": {
            "symbol": sym,
            "net_qty": qty,
            "avg_price_scaled": 331_000_000,
            "realized_pnl_scaled": 12_000_000,
        }
        for sym, qty in symbol_qty.items()
    }


async def _recover(verifier: StartupPositionVerifier, checkpoint: dict[str, dict[str, Any]]):
    from hft_platform.execution.checkpoint import PositionCheckpointWriter

    with patch.object(
        PositionCheckpointWriter,
        "load_checkpoint",
        return_value={"trading_date": "20260919", "positions": checkpoint, "total_realized_pnl_scaled": 12_000_000},
    ):
        verifier.checkpoint_path = "/fake"
        return await verifier.recover(trading_date="20260919", account_id="ACC1")


def _pin_cutoff(monkeypatch: pytest.MonkeyPatch, cutoff: date) -> None:
    monkeypatch.setattr(startup_recon, "taifex_delivery_cutoff", lambda _now_ns: cutoff)


class TestSettledContractUnderSim:
    @pytest.mark.asyncio
    async def test_the_production_shape_is_not_restored_after_delivery(self, monkeypatch) -> None:
        """TMFI6 +1, broker 0, sim, restart on 09-20."""
        _pin_cutoff(monkeypatch, date(2026, 9, 20))
        store = PositionStore()
        verifier = StartupPositionVerifier(_EmptyBroker(), store, checkpoint_path="/fake", order_mode="sim")

        result = await _recover(verifier, _ckpt(TMFI6=1))

        assert result.halted is False
        assert result.positions_loaded == 0
        assert store._recovery_positions == {}
        assert [m["action"] for m in result.mismatches] == ["dropped_settled"]
        assert result.auto_corrected == 1

    @pytest.mark.asyncio
    async def test_the_drop_is_logged_with_symbol_and_quantity(self, monkeypatch, module_log_sink) -> None:
        _pin_cutoff(monkeypatch, date(2026, 9, 20))
        events = module_log_sink(startup_recon)
        verifier = StartupPositionVerifier(_EmptyBroker(), PositionStore(), checkpoint_path="/fake", order_mode="sim")

        await _recover(verifier, _ckpt(TMFI6=1))

        settled = [e for e in events if e["event"] == "expired_contract_position_settled"]
        assert len(settled) == 1
        assert settled[0]["log_level"] == "warning"
        assert (settled[0]["symbol"], settled[0]["qty"], settled[0]["delivery_date"]) == ("TMFI6", 1, "2026-09-16")

    @pytest.mark.asyncio
    async def test_a_settled_position_is_not_counted_as_unconfirmable(self, monkeypatch) -> None:
        _pin_cutoff(monkeypatch, date(2026, 9, 20))
        verifier = StartupPositionVerifier(_EmptyBroker(), PositionStore(), checkpoint_path="/fake", order_mode="sim")

        await _recover(verifier, _ckpt(TMFI6=1))

        assert startup_recon_not_comparable._value.get() == 0

    @pytest.mark.asyncio
    async def test_its_realized_pnl_stays_in_the_portfolio_total(self, monkeypatch) -> None:
        """Dropping the position must not drop the money it already booked."""
        _pin_cutoff(monkeypatch, date(2026, 9, 20))
        store = PositionStore()
        verifier = StartupPositionVerifier(_EmptyBroker(), store, checkpoint_path="/fake", order_mode="sim")

        await _recover(verifier, _ckpt(TMFI6=1))

        assert store._total_realized_pnl_scaled == 12_000_000

    @pytest.mark.asyncio
    async def test_the_next_month_beside_it_is_still_preserved(self, monkeypatch) -> None:
        """Only the settled contract goes; the #481 partition holds for the live one."""
        _pin_cutoff(monkeypatch, date(2026, 9, 20))
        store = PositionStore()
        verifier = StartupPositionVerifier(_EmptyBroker(), store, checkpoint_path="/fake", order_mode="sim")

        result = await _recover(verifier, _ckpt(TMFI6=1, TMFJ6=-2))

        actions = {m["symbol"]: m["action"] for m in result.mismatches}
        assert actions == {"TMFI6": "dropped_settled", "TMFJ6": "preserved_not_comparable"}
        assert [d["net_qty"] for d in store._recovery_positions.values()] == [-2]

    @pytest.mark.parametrize(
        ("cutoff", "action"),
        [
            (date(2026, 9, 16), "preserved_not_comparable"),  # delivery day, before 13:30
            (date(2026, 9, 17), "dropped_settled"),  # delivery day after 13:30, and every day after
        ],
        ids=["final-session-still-open", "final-session-closed"],
    )
    @pytest.mark.asyncio
    async def test_the_boundary_is_the_final_session_close(self, monkeypatch, cutoff: date, action: str) -> None:
        _pin_cutoff(monkeypatch, cutoff)
        verifier = StartupPositionVerifier(_EmptyBroker(), PositionStore(), checkpoint_path="/fake", order_mode="sim")

        result = await _recover(verifier, _ckpt(TMFI6=1))

        assert [m["action"] for m in result.mismatches] == [action]

    @pytest.mark.parametrize("symbol", ["2330", "TMFR1", "TXO202609C23000", "not a code"])
    @pytest.mark.asyncio
    async def test_anything_that_is_not_a_monthly_future_code_is_never_dropped(self, monkeypatch, symbol: str) -> None:
        _pin_cutoff(monkeypatch, date(2027, 1, 1))
        verifier = StartupPositionVerifier(_EmptyBroker(), PositionStore(), checkpoint_path="/fake", order_mode="sim")

        result = await _recover(verifier, _ckpt(**{symbol: 1}))

        assert [m["action"] for m in result.mismatches] == ["preserved_not_comparable"]


class TestLiveModeIsUnchanged:
    @pytest.mark.asyncio
    async def test_live_mode_still_compares_a_settled_contract_as_before(self, monkeypatch) -> None:
        """Under live the broker book is authoritative already; this change is sim-only.

        A 3-lot gap on a future is over the threshold, so live still halts and a
        human books the settlement, which the platform cannot.
        """
        _pin_cutoff(monkeypatch, date(2026, 9, 20))
        verifier = StartupPositionVerifier(_EmptyBroker(), PositionStore(), checkpoint_path="/fake", order_mode="live")

        result = await _recover(verifier, _ckpt(TMFI6=3))

        assert result.halted is True
        assert [m["action"] for m in result.mismatches] == ["halt"]
