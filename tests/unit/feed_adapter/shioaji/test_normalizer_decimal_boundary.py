"""Scaled-int price conversion for Shioaji's Decimal and legacy numeric payloads.

Exercise the callback normalization boundary with both dict and attribute payloads.
Degenerate prices must be dropped without raising into the broker callback.
"""

from __future__ import annotations

import math
from decimal import Decimal
from types import SimpleNamespace
from typing import Any

import pytest

from hft_platform.events import BidAskEvent, TickEvent
from hft_platform.feed_adapter.normalizer import MarketDataNormalizer

_SCALE = 10_000


@pytest.fixture
def normalizer(tmp_path) -> MarketDataNormalizer:
    """A normalizer with two explicitly-scaled symbols (stock + future), x10000."""
    cfg = tmp_path / "symbols.yaml"
    cfg.write_text(
        "symbols:\n"
        "  - code: '2330'\n"
        "    exchange: 'TSE'\n"
        "    price_scale: 10000\n"
        "  - code: 'TXFR1'\n"
        "    exchange: 'TAIFEX'\n"
        "    price_scale: 10000\n"
    )
    return MarketDataNormalizer(str(cfg))


# --------------------------------------------------------------------------- #
# Payload builders — both dict-shaped and attribute-object-shaped, because the
# normalizer reads either, and real 1.5.3 stream objects are attribute-shaped.
# --------------------------------------------------------------------------- #
def _tick_dict(code: str, close: Any, volume: int = 10) -> dict[str, Any]:
    return {
        "code": code,
        "close": close,
        "volume": volume,
        "total_volume": volume,
        "ts": 1_620_000_000_000_000,
        "simtrade": 0,
        "intraday_odd": 0,
    }


def _tick_obj(code: str, close: Any, volume: int = 10) -> SimpleNamespace:
    return SimpleNamespace(
        code=code,
        close=close,
        volume=volume,
        total_volume=volume,
        ts=1_620_000_000_000_000,
        simtrade=0,
        intraday_odd=0,
    )


def _bidask_dict(
    code: str,
    bid_price: list[Any],
    bid_volume: list[int],
    ask_price: list[Any],
    ask_volume: list[int],
) -> dict[str, Any]:
    return {
        "code": code,
        "ts": 1_620_000_000_000_000,
        "bid_price": bid_price,
        "bid_volume": bid_volume,
        "ask_price": ask_price,
        "ask_volume": ask_volume,
    }


_SHAPE = [
    pytest.param(_tick_dict, id="dict"),
    pytest.param(_tick_obj, id="obj"),
]

# Tick-grid prices (no sub-tick ambiguity): every path agrees exactly with the
# documented ``int(round(float(price) * scale))`` contract.
_GRID_PRICES = ["100.5", "580", "0.0001", "12345.6789", "7777.7777", "1.2345"]
# Sub-tick half-ties exercise the configured scaling rule outside the tick grid.
_TIE_PRICES = ["100.50005", "100.49995", "580.00005", "999.99995"]


# --------------------------------------------------------------------------- #
# The #367-critical invariant: Decimal == float == int, on every path.
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("build", _SHAPE)
@pytest.mark.parametrize("raw", _GRID_PRICES)
def test_tick_decimal_equals_float_equals_int_scaled(normalizer, build, raw):
    """A 1.5.3 Decimal price scales identically to the 1.3.3 float (and int) it
    represents, and matches the documented contract on tick-grid prices."""
    expected = int(round(float(Decimal(raw)) * _SCALE))

    dec = normalizer.normalize_tick(build("2330", Decimal(raw)))
    flt = normalizer.normalize_tick(build("2330", float(raw)))
    assert isinstance(dec, TickEvent) and isinstance(flt, TickEvent)
    assert dec.price == flt.price == expected, f"{raw!r}: dec={dec.price} flt={flt.price} exp={expected}"

    if Decimal(raw) == Decimal(raw).to_integral_value():
        ivt = normalizer.normalize_tick(build("2330", int(Decimal(raw))))
        assert isinstance(ivt, TickEvent)
        assert ivt.price == expected


@pytest.mark.parametrize("raw", _TIE_PRICES)
def test_tick_half_even_tie_decimal_equals_float(normalizer, raw):
    dec = normalizer.normalize_tick(_tick_dict("2330", Decimal(raw)))
    flt = normalizer.normalize_tick(_tick_dict("2330", float(raw)))
    assert isinstance(dec, TickEvent) and isinstance(flt, TickEvent)
    assert dec.price == flt.price == int(round(float(raw) * _SCALE))


def test_bidask_decimal_matches_float_book(normalizer):
    """Decimal and float books produce byte-identical scaled levels — real 1.5.3
    vs legacy 1.3.3 payload equivalence on the bid/ask path."""
    bp_f = [580.0, 579.5, 579.0, 578.5, 578.0]
    ap_f = [580.5, 581.0, 581.5, 582.0, 582.5]
    bv = [1, 2, 3, 4, 5]
    av = [5, 4, 3, 2, 1]
    ev_f = normalizer.normalize_bidask(_bidask_dict("2330", bp_f, bv, ap_f, av))
    ev_d = normalizer.normalize_bidask(
        _bidask_dict("2330", [Decimal(str(p)) for p in bp_f], bv, [Decimal(str(p)) for p in ap_f], av)
    )
    assert isinstance(ev_f, BidAskEvent) and isinstance(ev_d, BidAskEvent)
    assert _levels(ev_d, "bid") == _levels(ev_f, "bid")
    assert _levels(ev_d, "ask") == _levels(ev_f, "ask")


# --------------------------------------------------------------------------- #
# Degenerate safety: never raise out of a callback; drop to None on Python path.
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("build", _SHAPE)
@pytest.mark.parametrize(
    "bad", [None, 0, -1, Decimal("0"), Decimal("-5"), float("nan"), Decimal("NaN"), float("inf"), Decimal("Infinity")]
)
def test_tick_degenerate_price_dropped_without_raising(normalizer, build, bad):
    """None/zero/negative/NaN prices return None on every path and never raise."""
    assert normalizer.normalize_tick(build("2330", bad)) is None


def test_tick_missing_symbol_returns_none(normalizer):
    payload = _tick_dict("2330", Decimal("100.5"))
    payload["code"] = None
    assert normalizer.normalize_tick(payload) is None


# --------------------------------------------------------------------------- #
# BidAsk book shapes and the Decimal scaling path.
# --------------------------------------------------------------------------- #
def _levels(event: BidAskEvent, side: str) -> list[list[int]]:
    raw = event.bids if side == "bid" else event.asks
    return [[int(p), int(v)] for p, v in raw]


def test_bidask_decimal_book_scales_to_positive_ints(normalizer):
    """A full 5-level Decimal book scales every present level to a positive int."""
    bp = [Decimal("580.0"), Decimal("579.5"), Decimal("579.0"), Decimal("578.5"), Decimal("578.0")]
    ap = [Decimal("580.5"), Decimal("581.0"), Decimal("581.5"), Decimal("582.0"), Decimal("582.5")]
    bv = [1, 2, 3, 4, 5]
    av = [5, 4, 3, 2, 1]
    event = normalizer.normalize_bidask(_bidask_dict("2330", bp, bv, ap, av))
    assert isinstance(event, BidAskEvent)
    exp_bids = [[int(round(float(p) * _SCALE)), v] for p, v in zip(bp, bv)]
    exp_asks = [[int(round(float(p) * _SCALE)), v] for p, v in zip(ap, av)]
    assert _levels(event, "bid") == exp_bids
    assert _levels(event, "ask") == exp_asks
    assert all(p > 0 for p, _ in _levels(event, "bid"))
    assert all(p > 0 for p, _ in _levels(event, "ask"))


def test_bidask_decimal_fractional_prices_scale_to_ints(normalizer):
    bp = [Decimal("100.25"), Decimal("100.20")]
    ap = [Decimal("100.30"), Decimal("100.35")]
    event = normalizer.normalize_bidask(_bidask_dict("2330", bp, [3, 7], ap, [4, 6]))
    assert isinstance(event, BidAskEvent)
    assert _levels(event, "bid") == [[1002500, 3], [1002000, 7]]
    assert _levels(event, "ask") == [[1003000, 4], [1003500, 6]]


def test_bidask_one_sided_book_scales_present_side(normalizer):
    """A one-sided (bid-only) Decimal book keeps the present side scaled and
    leaves the missing side empty — no crash, no float leakage."""
    event = normalizer.normalize_bidask(_bidask_dict("2330", [Decimal("580.0"), Decimal("579.5")], [1, 2], [], []))
    assert isinstance(event, BidAskEvent)
    assert _levels(event, "bid") == [[5800000, 1], [5795000, 2]]
    assert list(event.asks) == []


def test_bidask_empty_book_does_not_raise(normalizer):
    """A wholly empty book normalizes without raising (empty event or None)."""
    event = normalizer.normalize_bidask(_bidask_dict("2330", [], [], [], []))
    if isinstance(event, BidAskEvent):
        assert list(event.bids) == []
        assert list(event.asks) == []


def test_bidask_ragged_book_scales_paired_levels(normalizer):
    """A ragged book (more prices than volumes) scales only the paired levels —
    ``zip`` truncation must not raise or fabricate a level."""
    event = normalizer.normalize_bidask(
        _bidask_dict(
            "2330",
            [Decimal("580.0"), Decimal("579.5"), Decimal("579.0")],
            [1, 2],
            [Decimal("580.5")],
            [4],
        )
    )
    assert isinstance(event, BidAskEvent)
    assert _levels(event, "bid") == [[5800000, 1], [5795000, 2]]
    assert _levels(event, "ask") == [[5805000, 4]]


def test_no_float_leaks_into_scaled_prices(normalizer):
    """Defense-in-depth: every emitted price is an exact integral value, never a
    fractional float, on both tick and bidask paths (precision law)."""
    tick = normalizer.normalize_tick(_tick_dict("2330", Decimal("123.4567")))
    assert isinstance(tick, TickEvent)
    assert type(tick.price) is int
    book = normalizer.normalize_bidask(_bidask_dict("2330", [Decimal("123.4567")], [1], [Decimal("123.4600")], [1]))
    assert isinstance(book, BidAskEvent)
    for p, _v in list(book.bids) + list(book.asks):
        assert not math.isnan(float(p))
        assert int(p) == p  # integral value, no fractional remainder
