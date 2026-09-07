"""Startup recovery must not halt on a comparison that is undefined.

Production, 2026-09-04::

    HFT_ORDER_MODE=sim  ->  orders route to the broker's PAPER venue
    list_positions()    ->  reads the REAL account

    checkpoint TMFI6 -4        (paper fills the platform really took)
    broker     TMFI6  0        (the live account never saw them)
    diff 4 > futures threshold 2   ->  "critical"  ->  HALT

The two numbers describe different books, so their difference is not drift.
``ReconciliationService`` already draws exactly this partition for the
steady-state loop (``_NON_COMPARABLE_ORDER_MODES``); ``StartupPositionVerifier``
did not, and halted the engine on it.

The partition is one-directional, and these tests pin both halves::

    sim, broker 0, ckpt != 0   ->  not comparable   (paper routing explains it)
    sim, broker != 0           ->  still compared   (real external exposure)
    live, any                  ->  still compared   (nothing to explain)
"""

from __future__ import annotations

from typing import Any
from unittest.mock import patch

import pytest

from hft_platform.execution.positions import PositionStore
from hft_platform.execution.startup_recon import (
    StartupPositionVerifier,
    startup_recon_not_comparable,
)


class _FakeBrokerPosition:
    def __init__(self, code: str, quantity: int, direction: str = "") -> None:
        self.code = code
        self.quantity = quantity
        self.direction = direction


class _FakeBrokerClient:
    def __init__(self, positions: list[Any] | None = None) -> None:
        self._positions = positions or []

    def get_positions(self) -> list[Any]:
        return self._positions


def _ckpt(**symbol_qty: int) -> dict[str, dict[str, Any]]:
    """One strategy per symbol, the shape R47 writes."""
    return {
        f"ACC1:R47_MAKER_TMF:{sym}": {
            "symbol": sym,
            "net_qty": qty,
            "avg_price_scaled": 10_000_000,
        }
        for sym, qty in symbol_qty.items()
    }


async def _recover(
    verifier: StartupPositionVerifier,
    checkpoint: dict[str, dict[str, Any]],
) -> Any:
    from hft_platform.execution.checkpoint import PositionCheckpointWriter

    with patch.object(
        PositionCheckpointWriter,
        "load_checkpoint",
        return_value={"trading_date": "20260904", "positions": checkpoint},
    ):
        verifier.checkpoint_path = "/fake"
        return await verifier.recover(trading_date="20260904", account_id="ACC1")


class TestPaperRoutingIsNotDrift:
    @pytest.mark.asyncio
    async def test_a_position_the_live_account_reports_as_zero_does_not_halt_under_sim(self) -> None:
        """The exact 2026-09-04 shape: checkpoint -4, broker 0, sim."""
        verifier = StartupPositionVerifier(
            _FakeBrokerClient([]), PositionStore(), checkpoint_path="/fake", order_mode="sim"
        )

        result = await _recover(verifier, _ckpt(TMFI6=-4))

        assert result.halted is False
        assert [m["action"] for m in result.mismatches] == ["preserved_not_comparable"]

    @pytest.mark.asyncio
    async def test_the_checkpoint_is_restored_as_recorded_not_corrected_to_broker_zero(self) -> None:
        """Preserving means keeping -4, not agreeing with a book that never saw it."""
        store = PositionStore()
        verifier = StartupPositionVerifier(_FakeBrokerClient([]), store, checkpoint_path="/fake", order_mode="sim")

        result = await _recover(verifier, _ckpt(TMFI6=-4))

        assert result.positions_loaded == 1
        assert [d["net_qty"] for d in store._recovery_positions.values()] == [-4]

    @pytest.mark.asyncio
    async def test_the_per_strategy_split_survives_preservation(self) -> None:
        """Two strategies on one symbol keep their own quantities."""
        store = PositionStore()
        verifier = StartupPositionVerifier(_FakeBrokerClient([]), store, checkpoint_path="/fake", order_mode="sim")
        checkpoint = {
            "ACC1:R47_MAKER_TMF:TMFI6": {"symbol": "TMFI6", "net_qty": -3, "avg_price_scaled": 10_000_000},
            "ACC1:C60_TMFD6:TMFI6": {"symbol": "TMFI6", "net_qty": -1, "avg_price_scaled": 10_000_000},
        }

        await _recover(verifier, checkpoint)

        by_strategy = {d["strategy_id"]: d["net_qty"] for d in store._recovery_positions.values()}
        assert by_strategy == {"R47_MAKER_TMF": -3, "C60_TMFD6": -1}

    @pytest.mark.asyncio
    async def test_the_gauge_counts_the_symbols_the_broker_could_not_confirm(self) -> None:
        verifier = StartupPositionVerifier(
            _FakeBrokerClient([]), PositionStore(), checkpoint_path="/fake", order_mode="sim"
        )

        await _recover(verifier, _ckpt(TMFI6=-4, TXFI6=2))

        assert startup_recon_not_comparable._value.get() == 2

    @pytest.mark.asyncio
    async def test_a_startup_that_verified_nothing_does_not_read_as_a_clean_pass(self) -> None:
        """``startup_recon_status`` 1 means "positions match"; nothing matched."""
        from hft_platform.execution.startup_recon import startup_recon_status

        verifier = StartupPositionVerifier(
            _FakeBrokerClient([]), PositionStore(), checkpoint_path="/fake", order_mode="sim"
        )

        await _recover(verifier, _ckpt(TMFI6=-4))

        assert startup_recon_status._value.get() == 2

    @pytest.mark.asyncio
    async def test_a_flat_book_on_both_sides_is_not_reported_as_not_comparable(self) -> None:
        """``broker_qty == 0`` alone is not the trigger; a held position is."""
        verifier = StartupPositionVerifier(
            _FakeBrokerClient([]), PositionStore(), checkpoint_path="/fake", order_mode="sim"
        )

        result = await _recover(verifier, _ckpt(TMFI6=0))

        assert result.mismatches == []
        assert startup_recon_not_comparable._value.get() == 0


class TestTheSuppressionIsOneDirectional:
    @pytest.mark.asyncio
    async def test_a_position_only_the_broker_reports_still_halts_under_sim(self) -> None:
        """Paper routing explains a missing broker position, never an extra one.

        A position the live account holds and the platform does not is real
        external exposure. Suppressing this direction too would have hidden it.
        """
        verifier = StartupPositionVerifier(
            _FakeBrokerClient([_FakeBrokerPosition("TMFI6", 4)]),
            PositionStore(),
            checkpoint_path="/fake",
            order_mode="sim",
        )

        result = await _recover(verifier, {})

        assert result.halted is True
        assert [m["action"] for m in result.mismatches] == ["halt"]

    @pytest.mark.asyncio
    async def test_a_side_flip_still_halts_under_sim(self) -> None:
        """Long locally, short at the broker: both books saw something."""
        verifier = StartupPositionVerifier(
            _FakeBrokerClient([_FakeBrokerPosition("TMFI6", 2, direction="Short")]),
            PositionStore(),
            checkpoint_path="/fake",
            order_mode="sim",
        )

        result = await _recover(verifier, _ckpt(TMFI6=2))

        assert result.halted is True


class TestOnlyPaperModesSuppress:
    @pytest.mark.asyncio
    @pytest.mark.parametrize("mode", ["live", "real", "", "disabled"])
    async def test_the_same_shape_halts_under_a_comparable_order_mode(self, mode: str) -> None:
        """Under a live order path, checkpoint -4 vs broker 0 IS drift."""
        verifier = StartupPositionVerifier(
            _FakeBrokerClient([]), PositionStore(), checkpoint_path="/fake", order_mode=mode
        )

        result = await _recover(verifier, _ckpt(TMFI6=-4))

        assert result.halted is True

    @pytest.mark.asyncio
    @pytest.mark.parametrize("mode", ["sim", "simulation", "paper", "SIM", " Sim "])
    async def test_every_paper_alias_and_casing_suppresses(self, mode: str) -> None:
        """``HFT_ORDER_MODE`` is normalized at bootstrap, but not everywhere."""
        verifier = StartupPositionVerifier(
            _FakeBrokerClient([]), PositionStore(), checkpoint_path="/fake", order_mode=mode
        )

        result = await _recover(verifier, _ckpt(TMFI6=-4))

        assert result.halted is False

    @pytest.mark.asyncio
    async def test_order_mode_falls_back_to_the_environment(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Callers that do not pass the mode must not silently get 'comparable'."""
        monkeypatch.setenv("HFT_ORDER_MODE", "sim")
        verifier = StartupPositionVerifier(_FakeBrokerClient([]), PositionStore(), checkpoint_path="/fake")

        result = await _recover(verifier, _ckpt(TMFI6=-4))

        assert verifier._order_mode == "sim"
        assert result.halted is False


class TestBootstrapWiresTheMode:
    def test_bootstrap_passes_the_order_mode_to_the_verifier(self) -> None:
        """A mode-aware verifier constructed without its mode is not mode-aware.

        Source-level, in the style of ``test_startup_recovery_ordering``: the
        construction happens deep inside broker bootstrap and cannot be reached
        without a live client.
        """
        import inspect

        from hft_platform.services import bootstrap

        src = inspect.getsource(bootstrap)
        # The construction, not the import line above it.
        call_start = src.index("startup_verifier = StartupPositionVerifier(")
        call = src[call_start : call_start + 500]
        assert "order_mode=" in call, "bootstrap constructs the verifier without an order mode"
