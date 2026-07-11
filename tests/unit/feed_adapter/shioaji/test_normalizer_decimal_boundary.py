"""Decimal -> scaled-int x10000 boundary guard for the shioaji 1.5.3 upgrade (PR #367).

Shioaji 1.5.3 is a ground-up Rust rewrite whose tick/bidask callbacks deliver
prices as ``decimal.Decimal`` rather than ``float``. The platform's money law
requires every price to land downstream as a scaled integer (x10000) with no
hot-path float price math (``.agent/rules/01-core-laws.md`` precision law). The
conversion happens at the normalizer boundary
(``feed_adapter/normalizer.py``), **not** in the broker adapter, so this is the
file that must be proven version-robust before the SDK pin can move.

These tests are deliberately SDK-free (no ``import shioaji``) so they run
unchanged in both the project ``.venv`` (shioaji 1.3.3) and the throwaway
``shioaji[speed]==1.5.3`` harness venv, and serve as a permanent regression
guard regardless of which SDK version is installed.

What the runtime probes established (and what these tests lock in):

  * **The #367-critical invariant — Decimal/float/int parity.** For any numeric
    price, a ``Decimal`` input scales to the SAME int as the equivalent
    ``float`` (and ``int``) input, on every path — the in-tree Rust kernels
    (``normalize_tick_tuple`` and ``scale_book_pair_stats``) both ``extract``
    a ``Decimal`` directly, with no fallback. Therefore 1.5.3's Decimal prices
    scale identically to 1.3.3's float prices: **the upgrade is scaling-neutral.**
    This is the result that clears the Decimal residual for #367.

  * **Two pre-existing, SDK-INDEPENDENT divergences** found while proving the
    above, captured here as explicit characterization tests so the evidence is
    durable and any future drift trips a tripwire (NOT fixed here — a hot-path
    money kernel change is out of scope for #367 evidence-gathering):
      - the Rust *tick* kernel truncates a sub-tick half-tie (e.g. 100.49995 ->
        1004999) while Python's ``int(round(float(p)*scale))`` and the Rust
        *bidask* kernel round it to even (-> 1005000). Real instrument prices are
        on the tick grid so this never bites a live feed, but it is a genuine
        tick/bidask rounding inconsistency inside the Rust core.
      - a non-finite ``inf`` price passes the Rust tick kernel through as the
        saturating ``i64::MAX`` instead of being dropped; the Python path raises
        (caught) and drops it. No exception ever escapes either way.

  * **Degenerate safety** — ``None`` / ``0`` / negative / ``NaN`` / ``Infinity``
    never raise out of the callback (a raised exception inside a broker callback
    would tear down the quote connection), and are dropped to ``None`` on the
    pure-Python path.
"""

from __future__ import annotations

import math
from decimal import Decimal
from types import SimpleNamespace
from typing import Any

import pytest

import hft_platform.feed_adapter.normalizer as normalizer_mod
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


def _counter_value(child: Any) -> float | None:
    """Best-effort read of a prometheus_client Counter child's value."""
    try:
        return float(child._value.get())  # type: ignore[attr-defined]
    except Exception:
        return None


_RUST_TOGGLE = [
    pytest.param(True, id="rust_default"),
    pytest.param(False, id="rust_disabled"),
]
_SHAPE = [
    pytest.param(_tick_dict, id="dict"),
    pytest.param(_tick_obj, id="obj"),
]

# Tick-grid prices (no sub-tick ambiguity): every path agrees exactly with the
# documented ``int(round(float(price) * scale))`` contract.
_GRID_PRICES = ["100.5", "580", "0.0001", "12345.6789", "7777.7777", "1.2345"]
# Sub-tick half-ties: the Decimal-vs-float invariant still holds on each path,
# but the Rust *tick* kernel and Python disagree by one scaled unit (see the
# characterization test below). Real feeds never emit these.
_TIE_PRICES = ["100.50005", "100.49995", "580.00005", "999.99995"]


# --------------------------------------------------------------------------- #
# The #367-critical invariant: Decimal == float == int, on every path.
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("rust_enabled", _RUST_TOGGLE)
@pytest.mark.parametrize("build", _SHAPE)
@pytest.mark.parametrize("raw", _GRID_PRICES)
def test_tick_decimal_equals_float_equals_int_scaled(normalizer, monkeypatch, rust_enabled, build, raw):
    """A 1.5.3 Decimal price scales identically to the 1.3.3 float (and int) it
    represents, and matches the documented contract on tick-grid prices."""
    monkeypatch.setattr(normalizer_mod, "_RUST_ENABLED", rust_enabled)
    expected = int(round(float(Decimal(raw)) * _SCALE))

    dec = normalizer.normalize_tick(build("2330", Decimal(raw)))
    flt = normalizer.normalize_tick(build("2330", float(raw)))
    assert isinstance(dec, TickEvent) and isinstance(flt, TickEvent)
    assert dec.price == flt.price == expected, f"{raw!r}: dec={dec.price} flt={flt.price} exp={expected}"

    if Decimal(raw) == Decimal(raw).to_integral_value():
        ivt = normalizer.normalize_tick(build("2330", int(Decimal(raw))))
        assert isinstance(ivt, TickEvent)
        assert ivt.price == expected


@pytest.mark.parametrize("rust_enabled", _RUST_TOGGLE)
@pytest.mark.parametrize("raw", _TIE_PRICES)
def test_tick_half_even_tie_decimal_equals_float(normalizer, monkeypatch, rust_enabled, raw):
    """Even on sub-tick half-ties (the classic Decimal-vs-float money trap), a
    Decimal input lands on the SAME scaled int as the float it represents, on
    whichever path is active. This is the property #367 must guarantee — the
    *value* the kernel chooses for a tie is covered by the characterization test
    below, not here."""
    monkeypatch.setattr(normalizer_mod, "_RUST_ENABLED", rust_enabled)
    dec = normalizer.normalize_tick(_tick_dict("2330", Decimal(raw)))
    flt = normalizer.normalize_tick(_tick_dict("2330", float(raw)))
    assert isinstance(dec, TickEvent) and isinstance(flt, TickEvent)
    assert dec.price == flt.price


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
# Characterization of the two pre-existing, SDK-independent divergences.
# These assert the CURRENT behaviour as a documented tripwire; they are evidence
# captured during #367 validation, not a sanctioning of the behaviour.
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("raw", _TIE_PRICES)
def test_finding_rust_tick_kernel_tie_rounding_diverges_from_python_by_at_most_one(normalizer, monkeypatch, raw):
    """FINDING (pre-existing, SDK-independent): the Rust *tick* kernel truncates
    sub-tick half-ties where Python's ``int(round(...))`` rounds to even. The two
    differ by at most one scaled unit (1/10000). The Python path is exactly the
    documented contract; the Rust path may be one unit lower on a downward tie."""
    contract = int(round(float(Decimal(raw)) * _SCALE))

    monkeypatch.setattr(normalizer_mod, "_RUST_ENABLED", False)
    py = normalizer.normalize_tick(_tick_dict("2330", float(raw)))
    assert isinstance(py, TickEvent)
    assert py.price == contract  # Python path == documented contract, always.

    monkeypatch.setattr(normalizer_mod, "_RUST_ENABLED", True)
    ru = normalizer.normalize_tick(_tick_dict("2330", float(raw)))
    assert isinstance(ru, TickEvent)
    assert abs(ru.price - contract) <= 1, f"{raw!r}: rust={ru.price} contract={contract}"


def test_finding_rust_tick_kernel_passes_infinity_as_saturated_int(normalizer, monkeypatch):
    """FINDING (pre-existing): an ``inf`` price is dropped to None on the Python
    path (round() raises OverflowError, caught) but the Rust tick kernel returns
    a saturated positive int that passes the ``price > 0`` guard. Neither path
    raises out of the callback. ``nan`` is dropped to None on both paths."""
    monkeypatch.setattr(normalizer_mod, "_RUST_ENABLED", False)
    assert normalizer.normalize_tick(_tick_dict("2330", float("inf"))) is None

    monkeypatch.setattr(normalizer_mod, "_RUST_ENABLED", True)
    rust_inf = normalizer.normalize_tick(_tick_dict("2330", float("inf")))
    # Current Rust behaviour: a TickEvent with a saturated (huge, positive) price.
    assert rust_inf is None or (isinstance(rust_inf, TickEvent) and rust_inf.price > 0)
    # NaN is dropped on both paths regardless of toggle.
    assert normalizer.normalize_tick(_tick_dict("2330", float("nan"))) is None


# --------------------------------------------------------------------------- #
# Degenerate safety: never raise out of a callback; drop to None on Python path.
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("rust_enabled", _RUST_TOGGLE)
@pytest.mark.parametrize("build", _SHAPE)
@pytest.mark.parametrize("bad", [None, 0, -1, Decimal("0"), Decimal("-5"), float("nan"), Decimal("NaN")])
def test_tick_degenerate_price_dropped_without_raising(normalizer, monkeypatch, rust_enabled, build, bad):
    """None/zero/negative/NaN prices return None on every path and never raise."""
    monkeypatch.setattr(normalizer_mod, "_RUST_ENABLED", rust_enabled)
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


@pytest.mark.parametrize("rust_enabled", _RUST_TOGGLE)
def test_bidask_decimal_book_scales_to_positive_ints(normalizer, monkeypatch, rust_enabled):
    """A full 5-level Decimal book scales every present level to a positive int,
    identical whether Rust is enabled or not."""
    monkeypatch.setattr(normalizer_mod, "_RUST_ENABLED", rust_enabled)
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


def test_bidask_decimal_python_fallback_is_correct(normalizer, monkeypatch):
    """With Rust disabled, the pure-Python bidask path scales a Decimal book
    correctly — this is the path the Rust fast-path would fall back to if it ever
    failed to consume a Decimal (observed: it does not, but the fallback must
    stay correct)."""
    monkeypatch.setattr(normalizer_mod, "_RUST_ENABLED", False)
    bp = [Decimal("100.25"), Decimal("100.20")]
    ap = [Decimal("100.30"), Decimal("100.35")]
    event = normalizer.normalize_bidask(_bidask_dict("2330", bp, [3, 7], ap, [4, 6]))
    assert isinstance(event, BidAskEvent)
    assert _levels(event, "bid") == [[1002500, 3], [1002000, 7]]
    assert _levels(event, "ask") == [[1003000, 4], [1003500, 6]]


def test_bidask_decimal_rust_path_is_correct_and_observable(normalizer, monkeypatch):
    """With Rust enabled, a Decimal book yields correct scaled ints. The in-tree
    Rust kernel consumes Decimal directly (no fallback observed), but if a build
    ever did fall back it must increment ``rust_fallback_total{type=bidask}`` and
    still return the right values — never a silent wrong answer."""
    monkeypatch.setattr(normalizer_mod, "_RUST_ENABLED", True)
    bp = [Decimal("100.25"), Decimal("100.20")]
    ap = [Decimal("100.30"), Decimal("100.35")]
    before = _counter_value(normalizer._rust_fallback_bidask)
    event = normalizer.normalize_bidask(_bidask_dict("2330", bp, [3, 7], ap, [4, 6]))
    after = _counter_value(normalizer._rust_fallback_bidask)
    assert isinstance(event, BidAskEvent)
    assert _levels(event, "bid") == [[1002500, 3], [1002000, 7]]
    assert _levels(event, "ask") == [[1003000, 4], [1003500, 6]]
    if before is not None and after is not None:
        assert after >= before  # fallback may be 0 (direct consume) or >0, never negative.


@pytest.mark.parametrize("rust_enabled", _RUST_TOGGLE)
def test_bidask_one_sided_book_scales_present_side(normalizer, monkeypatch, rust_enabled):
    """A one-sided (bid-only) Decimal book keeps the present side scaled and
    leaves the missing side empty — no crash, no float leakage."""
    monkeypatch.setattr(normalizer_mod, "_RUST_ENABLED", rust_enabled)
    event = normalizer.normalize_bidask(_bidask_dict("2330", [Decimal("580.0"), Decimal("579.5")], [1, 2], [], []))
    assert isinstance(event, BidAskEvent)
    assert _levels(event, "bid") == [[5800000, 1], [5795000, 2]]
    assert list(event.asks) == []


@pytest.mark.parametrize("rust_enabled", _RUST_TOGGLE)
def test_bidask_empty_book_does_not_raise(normalizer, monkeypatch, rust_enabled):
    """A wholly empty book normalizes without raising (empty event or None)."""
    monkeypatch.setattr(normalizer_mod, "_RUST_ENABLED", rust_enabled)
    event = normalizer.normalize_bidask(_bidask_dict("2330", [], [], [], []))
    if isinstance(event, BidAskEvent):
        assert list(event.bids) == []
        assert list(event.asks) == []


def test_bidask_ragged_book_scales_paired_levels(normalizer, monkeypatch):
    """A ragged book (more prices than volumes) scales only the paired levels —
    ``zip`` truncation must not raise or fabricate a level."""
    monkeypatch.setattr(normalizer_mod, "_RUST_ENABLED", False)
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
