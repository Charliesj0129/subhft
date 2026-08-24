"""The decision_price stamp must not truncate a typed-intent frame.

StrategyRunner stamps the LOB mid into index 16 of a ``typed_intent_v1``
tuple. It used to rebuild the tuple inline as ``(*intent[:16], _mid)``, which
produced a 17-element frame and dropped index 17, ``price_type``.
``typed_intent_price_type`` requires ``len(intent) >= 18`` and otherwise
returns its legacy-frame default "LMT" — so every
``ctx.place_order(price_type="MKT")`` became a limit order whenever the LOB
had a positive mid, with no error raised anywhere on the path.

These tests exercise the helper the runner now calls, which is the only place
that knows the frame layout.
"""

from hft_platform.contracts.strategy import typed_intent_price_type, typed_intent_symbol
from hft_platform.strategy.runner import _typed_intent_with_decision_price as typed_intent_with_decision_price


def _frame(price_type: str = "MKT") -> tuple:
    """An 18-element typed_intent_v1 frame in the layout _intent_factory emits."""
    return (
        "typed_intent_v1",
        1,  # intent_id
        "S1",  # strategy_id
        "TXFE6",  # symbol
        0,  # intent_type
        0,  # side
        1_800_000,  # price
        1,  # qty
        0,  # tif
        "",  # target_order_id
        11,  # created_ts_ns
        12,  # source_ts_ns
        "",
        "trace-1",
        "",
        0,  # ttl_ns
        0,  # decision_price — index 16
        price_type,  # price_type — index 17
    )


class TestDecisionPriceStamp:
    def test_a_market_order_survives_the_stamp(self) -> None:
        assert typed_intent_price_type(_frame("MKT")) == "MKT"

        stamped = typed_intent_with_decision_price(_frame("MKT"), 1_805_000)

        assert typed_intent_price_type(stamped) == "MKT"

    def test_a_limit_order_survives_the_stamp(self) -> None:
        stamped = typed_intent_with_decision_price(_frame("LMT"), 1_805_000)
        assert typed_intent_price_type(stamped) == "LMT"

    def test_the_mid_lands_in_index_16(self) -> None:
        stamped = typed_intent_with_decision_price(_frame(), 1_805_000)
        assert stamped[16] == 1_805_000

    def test_the_frame_keeps_its_length(self) -> None:
        """A 17-tuple is exactly what makes price_type fall back to LMT."""
        stamped = typed_intent_with_decision_price(_frame(), 1_805_000)
        assert len(stamped) == len(_frame())

    def test_no_other_field_is_disturbed(self) -> None:
        original = _frame("MKT")
        stamped = typed_intent_with_decision_price(original, 1_805_000)
        assert typed_intent_symbol(stamped) == "TXFE6"
        assert stamped[:16] == original[:16]
        assert stamped[17:] == original[17:]

    def test_a_longer_future_frame_keeps_its_tail(self) -> None:
        """Splicing, not field-naming, is why this stays correct if the frame grows."""
        grown = (*_frame("MKT"), "future_field")
        stamped = typed_intent_with_decision_price(grown, 1_805_000)
        assert len(stamped) == len(grown)
        assert stamped[-1] == "future_field"
        assert typed_intent_price_type(stamped) == "MKT"

    def test_the_old_inline_rebuild_is_what_lost_the_price_type(self) -> None:
        """Pins the mechanism so a refactor cannot quietly reintroduce it."""
        truncated = (*_frame("MKT")[:16], 1_805_000)
        assert len(truncated) == 17
        assert typed_intent_price_type(truncated) == "LMT"
