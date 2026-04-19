"""Gate 2a: events carry an optional ``ContractRef``.

Backward-compatibility contract:
- Existing keyword-only callers keep working (contract defaults to None).
- Existing positional constructors still work as long as they pass only the
  historically-positional fields.
- Reading ``event.contract`` on a legacy-constructed event returns ``None``.
"""

from __future__ import annotations

from datetime import date

import numpy as np

from hft_platform.contracts.ref import FamilyCode, FutureRef, OptionRef, Right, StockRef
from hft_platform.events import BidAskEvent, MetaData, TickEvent


def _meta() -> MetaData:
    return MetaData(seq=1, source_ts=0, local_ts=0, topic="t")


class TestBackwardCompat:
    def test_tick_event_without_contract_defaults_none(self) -> None:
        t = TickEvent(meta=_meta(), symbol="TMFE6", price=100_000, volume=1)
        assert t.contract is None

    def test_bidask_event_without_contract_defaults_none(self) -> None:
        b = BidAskEvent(
            meta=_meta(),
            symbol="TMFE6",
            bids=np.array([[10_000, 1]], dtype=np.int64),
            asks=np.array([[10_100, 1]], dtype=np.int64),
        )
        assert b.contract is None


class TestWithContract:
    def test_tick_event_with_future_ref(self) -> None:
        ref = FutureRef(root="TMF", expiry=date(2026, 5, 21))
        t = TickEvent(
            meta=_meta(),
            symbol="TMFE6",
            price=100_000,
            volume=1,
            contract=ref,
        )
        assert isinstance(t.contract, FutureRef)
        assert t.contract.root == "TMF"

    def test_tick_event_with_option_ref(self) -> None:
        ref = OptionRef(
            root="TXO",
            expiry=date(2026, 5, 21),
            strike=23_000,
            right=Right.CALL,
        )
        t = TickEvent(
            meta=_meta(),
            symbol=ref.display(),
            price=100_000,
            volume=1,
            contract=ref,
        )
        assert t.contract.display() == "TXO202605C23000"

    def test_bidask_event_with_stock_ref(self) -> None:
        ref = StockRef(code="2330")
        b = BidAskEvent(
            meta=_meta(),
            symbol="2330",
            bids=np.array([[5_800_000, 1]], dtype=np.int64),
            asks=np.array([[5_801_000, 1]], dtype=np.int64),
            contract=ref,
        )
        assert b.contract.code == "2330"


class TestContractAlignsWithSymbol:
    """Sanity: when both are set they describe the same instrument."""

    def test_display_matches_symbol_for_future(self) -> None:
        ref = FutureRef(root="TMF", expiry=date(2026, 5, 21), family=FamilyCode.R1)
        t = TickEvent(
            meta=_meta(),
            symbol=ref.display(),
            price=100_000,
            volume=1,
            contract=ref,
        )
        assert t.symbol == ref.display() == "TMFE6"

    def test_frozen_event_still_frozen(self) -> None:
        """Adding ``contract`` must not break frozen semantics."""
        t = TickEvent(meta=_meta(), symbol="TMFE6", price=100_000, volume=1)
        import dataclasses

        try:
            t.contract = FutureRef("TMF", date(2026, 5, 21))  # type: ignore[misc]
        except (dataclasses.FrozenInstanceError, AttributeError):
            return
        raise AssertionError("TickEvent should still be frozen")
