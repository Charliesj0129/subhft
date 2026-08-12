"""``hft.order_intents`` would record blank rows the day it is switched on.

The table has zero rows for all time. That much is not a defect:
``HFT_INTENT_RECORDER_ENABLED`` defaults to ``"0"``
(``strategy/runner.py:371``) and has never been set, so the producer hook at
``runner.py:1535`` has never fired.

The defect is what happens when it is. ``HFT_TYPED_INTENT_CHANNEL`` defaults to
``"1"`` (``runner.py:258``), so the strategy→risk fast path carries intents as
``("typed_intent_v1", intent_id, strategy_id, symbol, ...)`` tuples, and
``runner.py:1541`` puts that tuple straight into the recorder envelope.
``_extract_intent_values`` then reads every field with bare ``getattr``, which
on a tuple returns the default for all eighteen of them: intent_id 0,
strategy_id "", symbol "", side "", price 0, qty 0.

``contracts/strategy.py:132-135`` warns about exactly this:

    Attribute access (`intent.side`, etc.) does not work on tuples, so
    consumers that need these fields MUST use these helpers rather than bare
    ``getattr`` — Bug 9/13 showed silent ``side=None`` propagation freezing
    R47's pending counters permanently.

Same failure family as the 2026-08-10 outage: a structurally-different type
meets ``getattr``, and the result is zeros that look like data.
"""

from __future__ import annotations

import pytest

from hft_platform.contracts.strategy import TIF, IntentType, OrderIntent, Side
from hft_platform.recorder.worker import INTENT_COLUMNS, _extract_intent_values

_COL = {name: i for i, name in enumerate(INTENT_COLUMNS)}


def _typed_frame() -> tuple:
    """The frame ``StrategyRunner._make_intent`` builds when the fast path is
    on (``runner.py:891-910``)."""
    return (
        "typed_intent_v1",
        4242,  # 1  intent_id
        "R47_MAKER_TMF",  # 2  strategy_id
        "TMFH6",  # 3  symbol
        int(IntentType.NEW),  # 4  intent_type
        int(Side.SELL),  # 5  side
        3733_0000,  # 6  price (scaled x10000)
        2,  # 7  qty
        int(TIF.LIMIT),  # 8  tif
        "",  # 9  target_order_id
        1_700_000_000_000_000_000,  # 10 timestamp_ns
        1_699_999_999_000_000_000,  # 11 source_ts_ns
        "maker_quote",  # 12 reason
        "trace-abc",  # 13 trace_id
        "idem-xyz",  # 14 idempotency_key
        5_000_000_000,  # 15 ttl_ns
        3730_0000,  # 16 decision_price
        "LMT",  # 17 price_type
    )


def _envelope(intent: object) -> dict:
    return {"intent": intent, "ingest_ts": 1_700_000_000_123_456_789}


# --------------------------------------------------------------------------- #
# The typed tuple — the shape production actually emits                        #
# --------------------------------------------------------------------------- #


@pytest.mark.unit
def test_a_typed_intent_tuple_records_its_identity_not_blanks() -> None:
    """The regression: every identity column came back empty, so the audit
    table would have filled with anonymous rows."""
    values = _extract_intent_values(_envelope(_typed_frame()))

    assert values is not None
    assert values[_COL["intent_id"]] == 4242
    assert values[_COL["strategy_id"]] == "R47_MAKER_TMF"
    assert values[_COL["symbol"]] == "TMFH6"


@pytest.mark.unit
def test_a_typed_intent_tuple_records_its_side_and_type_as_names() -> None:
    """Enum fields arrive as ints on the fast path but the ClickHouse columns
    are String. ``getattr`` produced "" for all three."""
    values = _extract_intent_values(_envelope(_typed_frame()))

    assert values[_COL["side"]] == "SELL"
    assert values[_COL["intent_type"]] == "NEW"
    assert values[_COL["tif"]] == "LIMIT"


@pytest.mark.unit
def test_a_typed_intent_tuple_records_price_and_qty() -> None:
    """A zero price on a money-facing audit row is worse than no row."""
    values = _extract_intent_values(_envelope(_typed_frame()))

    assert values[_COL["price_scaled"]] == 3733_0000
    assert values[_COL["qty"]] == 2
    assert values[_COL["decision_price"]] == 3730_0000


@pytest.mark.unit
def test_a_typed_intent_tuple_records_its_correlation_fields() -> None:
    """trace_id and idempotency_key are what make the table joinable against
    hft.orders and the decision trace."""
    values = _extract_intent_values(_envelope(_typed_frame()))

    assert values[_COL["trace_id"]] == "trace-abc"
    assert values[_COL["idempotency_key"]] == "idem-xyz"
    assert values[_COL["reason"]] == "maker_quote"
    assert values[_COL["ttl_ns"]] == 5_000_000_000
    assert values[_COL["timestamp_ns"]] == 1_700_000_000_000_000_000
    assert values[_COL["source_ts_ns"]] == 1_699_999_999_000_000_000
    assert values[_COL["price_type"]] == "LMT"
    assert values[_COL["ingest_ts"]] == 1_700_000_000_123_456_789


@pytest.mark.unit
def test_the_extractor_reads_the_same_frame_layout_as_the_gateway() -> None:
    """The tuple layout is positional and duplicated in two places. The gateway
    already decodes it (``typed_frame_to_view``); if the extractor's indices
    ever drift from the gateway's, this fails rather than silently writing
    misaligned columns."""
    from hft_platform.gateway.channel import typed_frame_to_view

    frame = _typed_frame()
    view = typed_frame_to_view(frame)
    values = _extract_intent_values(_envelope(frame))

    assert values[_COL["intent_id"]] == view.intent_id
    assert values[_COL["strategy_id"]] == view.strategy_id
    assert values[_COL["symbol"]] == view.symbol
    assert values[_COL["price_scaled"]] == view.price
    assert values[_COL["qty"]] == view.qty
    assert values[_COL["side"]] == Side(view.side).name
    assert values[_COL["intent_type"]] == IntentType(view.intent_type).name
    assert values[_COL["tif"]] == TIF(view.tif).name
    assert values[_COL["reason"]] == view.reason
    assert values[_COL["trace_id"]] == view.trace_id
    assert values[_COL["idempotency_key"]] == view.idempotency_key
    assert values[_COL["ttl_ns"]] == view.ttl_ns
    assert values[_COL["decision_price"]] == view.decision_price
    assert values[_COL["price_type"]] == view.price_type


@pytest.mark.unit
def test_a_legacy_17_field_frame_does_not_raise() -> None:
    """``typed_intent_price_type`` documents 17-tuple frames as legacy-valid.
    A short frame must degrade to the documented default, not drop the row."""
    values = _extract_intent_values(_envelope(_typed_frame()[:17]))

    assert values is not None
    assert values[_COL["price_type"]] == "LMT"
    assert values[_COL["decision_price"]] == 3730_0000


@pytest.mark.unit
def test_a_malformed_tuple_is_dropped_rather_than_written_short() -> None:
    """A row with the wrong number of values corrupts the whole columnar batch,
    not just itself. Returning None drops one row instead."""
    assert _extract_intent_values(_envelope(("typed_intent_v1", 1, "s"))) is None


@pytest.mark.unit
def test_a_plain_tuple_is_not_treated_as_a_typed_frame() -> None:
    assert _extract_intent_values(_envelope((1, 2, 3))) is None


# --------------------------------------------------------------------------- #
# The OrderIntent path must keep working                                       #
# --------------------------------------------------------------------------- #


@pytest.mark.unit
def test_an_order_intent_object_still_records_every_field() -> None:
    """The fast path is switchable off (``HFT_TYPED_INTENT_CHANNEL=0``), and
    the object path is what the existing extractor was written for."""
    intent = OrderIntent(
        intent_id=7,
        strategy_id="R47_MAKER_TMF",
        symbol="TMFH6",
        intent_type=IntentType.NEW,
        side=Side.BUY,
        price=3728_0000,
        qty=1,
        tif=TIF.LIMIT,
        trace_id="trace-obj",
    )

    values = _extract_intent_values(_envelope(intent))

    assert values[_COL["intent_id"]] == 7
    assert values[_COL["symbol"]] == "TMFH6"
    assert values[_COL["side"]] == "BUY"
    assert values[_COL["intent_type"]] == "NEW"
    assert values[_COL["price_scaled"]] == 3728_0000
    assert values[_COL["trace_id"]] == "trace-obj"


@pytest.mark.unit
def test_the_extractor_emits_exactly_one_value_per_column() -> None:
    """Positional packing: a length mismatch misaligns every column in the
    batch. Pinned for both input shapes."""
    for payload in (_typed_frame(), OrderIntent(
            intent_id=1,
            strategy_id="s",
            symbol="X",
            intent_type=IntentType.NEW,
            side=Side.BUY,
            price=1_0000,
            qty=1,
        )):
        values = _extract_intent_values(_envelope(payload))
        assert values is not None
        assert len(values) == len(INTENT_COLUMNS)


@pytest.mark.unit
def test_none_and_empty_payloads_are_dropped_quietly() -> None:
    assert _extract_intent_values(None) is None
    assert _extract_intent_values({"intent": None, "ingest_ts": 1}) is None
