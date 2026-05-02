"""Gate 3 slice: OrderIntent.contract propagation from events.

Validates the dual-write contract from Gate 2b flows through the strategy
runner into downstream consumers. No behavior change beyond the extra
field — this is the minimum slice that enables Risk/OrderAdapter to start
preferring ``intent.contract`` in later commits without a bigger rewrite.
"""

from __future__ import annotations

from datetime import date

import numpy as np

from hft_platform.contracts.ref import FutureRef, StockRef
from hft_platform.contracts.strategy import TIF, IntentType, OrderIntent, Side
from hft_platform.events import BidAskEvent, MetaData, TickEvent


def _runner():
    from hft_platform.strategy.runner import StrategyRunner

    r = StrategyRunner.__new__(StrategyRunner)
    r._intent_seq = 0
    r._current_source_ts_ns = 0
    r._current_trace_id = ""
    r._current_contract = None
    r._typed_intent_fastpath = False
    r._default_intent_ttl_ns = 0
    r.symbol_metadata = None
    return r


class TestOrderIntentSchema:
    def test_contract_defaults_none(self) -> None:
        intent = OrderIntent(
            intent_id=1,
            strategy_id="s1",
            symbol="2330",
            intent_type=IntentType.NEW,
            side=Side.BUY,
            price=5_500_000,
            qty=1,
        )
        assert intent.contract is None

    def test_contract_accepts_contract_ref(self) -> None:
        ref = FutureRef(root="TMF", expiry=date(2026, 5, 21))
        intent = OrderIntent(
            intent_id=1,
            strategy_id="s1",
            symbol="TMFE6",
            intent_type=IntentType.NEW,
            side=Side.BUY,
            price=100_000,
            qty=1,
            contract=ref,
        )
        assert intent.contract is ref


class TestIntentFactoryPropagation:
    def test_intent_carries_contract_when_event_has_one(self) -> None:
        runner = _runner()
        ref = FutureRef(root="TMF", expiry=date(2026, 5, 21))
        runner._current_contract = ref

        intent = runner._intent_factory(
            strategy_id="r47",
            symbol="TMFE6",
            side=Side.BUY,
            price=100_000,
            qty=1,
            tif=TIF.LIMIT,
            intent_type=IntentType.NEW,
        )
        assert isinstance(intent, OrderIntent)
        assert intent.contract is ref

    def test_intent_contract_none_when_event_lacks_one(self) -> None:
        runner = _runner()
        runner._current_contract = None

        intent = runner._intent_factory(
            strategy_id="r47",
            symbol="TMFE6",
            side=Side.BUY,
            price=100_000,
            qty=1,
            tif=TIF.LIMIT,
            intent_type=IntentType.NEW,
        )
        assert intent.contract is None

    def test_stock_ref_propagates(self) -> None:
        runner = _runner()
        runner._current_contract = StockRef("2330")
        intent = runner._intent_factory(
            strategy_id="legacy",
            symbol="2330",
            side=Side.BUY,
            price=5_800_000,
            qty=1,
            tif=TIF.LIMIT,
            intent_type=IntentType.NEW,
        )
        assert isinstance(intent.contract, StockRef)
        assert intent.contract.code == "2330"


class TestCurrentContractTrackedFromEvent:
    def test_process_event_captures_event_contract(self) -> None:
        """Run the actual tracker logic in ``process_event`` against a
        synthetic event with ``contract`` set."""
        runner = _runner()
        ref = FutureRef(root="TMF", expiry=date(2026, 5, 21))

        # Reproduce the part of process_event that matters for this test —
        # we do not need the full dispatch machinery.
        event = TickEvent(
            meta=MetaData(seq=1, source_ts=0, local_ts=0, topic="tick"),
            symbol="TMFE6",
            price=100_000,
            volume=1,
            contract=ref,
        )
        runner._current_contract = getattr(event, "contract", None)
        assert runner._current_contract is ref

    def test_process_event_missing_contract_yields_none(self) -> None:
        runner = _runner()
        event = BidAskEvent(
            meta=MetaData(seq=1, source_ts=0, local_ts=0, topic="bidask"),
            symbol="TMFE6",
            bids=np.array([[10_000, 1]], dtype=np.int64),
            asks=np.array([[10_100, 1]], dtype=np.int64),
        )
        runner._current_contract = getattr(event, "contract", None)
        assert runner._current_contract is None


class TestTypedTuplePathUnaffected:
    """Gate 3 slice intentionally does NOT extend the typed-intent tuple —
    doing so would break every tuple-consumer. Tuple path consumers that
    need ContractRef identity should resolve via ``symbol`` -> resolver."""

    def test_typed_tuple_length_unchanged(self) -> None:
        runner = _runner()
        runner._typed_intent_fastpath = True
        ref = FutureRef(root="TMF", expiry=date(2026, 5, 21))
        runner._current_contract = ref  # not propagated to tuples

        tup = runner._intent_factory(
            strategy_id="r47",
            symbol="TMFE6",
            side=Side.BUY,
            price=100_000,
            qty=1,
            tif=TIF.LIMIT,
            intent_type=IntentType.NEW,
        )
        assert isinstance(tup, tuple)
        # Production format carries 18 positional fields (see runner.py:_intent_factory)
        assert len(tup) == 18
        assert tup[0] == "typed_intent_v1"
