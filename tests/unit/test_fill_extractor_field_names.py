"""Tests for fill extractor field name resolution (P-08).

Verifies that _extract_fill_values() correctly reads FillEvent fields
(price, fee, tax — without _scaled suffix) as well as dict-based rows
that use the _scaled convention.
"""

from dataclasses import dataclass
from enum import IntEnum

import pytest

from hft_platform.recorder.worker import _extract_fill_values, _getattr_scaled


# ---------------------------------------------------------------------------
# Minimal FillEvent-like fixture matching contracts/execution.py
# ---------------------------------------------------------------------------


class _Side(IntEnum):
    BUY = 0
    SELL = 1


@dataclass(slots=True)
class _FakeFillEvent:
    """Minimal stand-in for FillEvent with plain price/fee/tax fields."""

    fill_id: str
    account_id: str
    order_id: str
    strategy_id: str
    symbol: str
    side: _Side
    qty: int
    price: int
    fee: int
    tax: int
    ingest_ts_ns: int
    match_ts_ns: int
    decision_price: int = 0
    arrival_price: int = 0


@dataclass
class _FakeFillEventScaled:
    """Stand-in that uses the _scaled naming convention (dict-WAL style)."""

    fill_id: str = "F2"
    strategy_id: str = "s1"
    symbol: str = "2330"
    side: int = 0
    qty: int = 1
    price_scaled: int = 5678_0000
    fee_scaled: int = 50_0000
    tax_scaled: int = 30_0000
    ts_exchange: int = 1_000_000
    ts_local: int = 2_000_000
    client_order_id: str = "C2"
    broker_order_id: str = "B2"


# ---------------------------------------------------------------------------
# _getattr_scaled unit tests
# ---------------------------------------------------------------------------


def test_getattr_scaled_returns_plain_field_when_present():
    """Plain field name should take priority over _scaled variant."""

    class _Obj:
        price = 1234_0000
        price_scaled = 9999_0000  # should be ignored

    assert _getattr_scaled(_Obj(), "price") == 1234_0000


def test_getattr_scaled_falls_back_to_scaled_when_plain_missing():
    """_scaled variant is used when the plain field is absent."""

    class _Obj:
        price_scaled = 9999_0000

    assert _getattr_scaled(_Obj(), "price") == 9999_0000


def test_getattr_scaled_returns_none_when_both_missing():
    """Returns None when neither plain nor _scaled attribute exists."""

    class _Obj:
        pass

    assert _getattr_scaled(_Obj(), "price") is None


def test_getattr_scaled_does_not_treat_zero_as_missing():
    """Zero is a valid price — must not fall through to _scaled."""

    class _Obj:
        price = 0
        price_scaled = 9999_0000

    assert _getattr_scaled(_Obj(), "price") == 0


# ---------------------------------------------------------------------------
# _extract_fill_values with FillEvent-style objects
# ---------------------------------------------------------------------------


def test_extract_fill_values_reads_price_from_fill_event():
    """price field on FillEvent must be captured in the output list."""
    ev = _FakeFillEvent(
        fill_id="F1",
        account_id="ACC",
        order_id="O1",
        strategy_id="s1",
        symbol="2330",
        side=_Side.BUY,
        qty=2,
        price=530_0000,
        fee=10_0000,
        tax=5_0000,
        ingest_ts_ns=1_000_000,
        match_ts_ns=2_000_000,
    )
    result = _extract_fill_values(ev)
    assert result is not None, "_extract_fill_values returned None unexpectedly"
    # FILL_COLUMNS index 9 = price_scaled, 10 = fee_scaled, 11 = tax_scaled
    price_val = result[9]
    fee_val = result[10]
    tax_val = result[11]
    assert price_val == 530_0000, f"Expected price 530_0000, got {price_val}"
    assert fee_val == 10_0000, f"Expected fee 10_0000, got {fee_val}"
    assert tax_val == 5_0000, f"Expected tax 5_0000, got {tax_val}"


def test_extract_fill_values_symbol_captured_from_fill_event():
    """symbol field from FillEvent must be propagated."""
    ev = _FakeFillEvent(
        fill_id="F1",
        account_id="ACC",
        order_id="O1",
        strategy_id="s1",
        symbol="0050",
        side=_Side.SELL,
        qty=5,
        price=100_0000,
        fee=20_0000,
        tax=10_0000,
        ingest_ts_ns=1_000_000,
        match_ts_ns=2_000_000,
    )
    result = _extract_fill_values(ev)
    assert result is not None
    # FILL_COLUMNS index 6 = symbol
    assert result[6] == "0050"


# ---------------------------------------------------------------------------
# _extract_fill_values with _scaled-named objects (legacy WAL rows)
# ---------------------------------------------------------------------------


def test_extract_fill_values_reads_price_scaled_from_legacy_object():
    """Objects using _scaled naming (e.g., dict-WAL rows) still work."""
    obj = _FakeFillEventScaled()
    result = _extract_fill_values(obj)
    assert result is not None
    price_val = result[9]
    fee_val = result[10]
    tax_val = result[11]
    assert price_val == 5678_0000, f"Expected price 5678_0000, got {price_val}"
    assert fee_val == 50_0000, f"Expected fee 50_0000, got {fee_val}"
    assert tax_val == 30_0000, f"Expected tax 30_0000, got {tax_val}"


# ---------------------------------------------------------------------------
# _extract_fill_values with dict rows
# ---------------------------------------------------------------------------


def test_extract_fill_values_reads_price_scaled_from_dict():
    """Dict rows using price_scaled/fee_scaled/tax_scaled keys must be read."""
    row = {
        "ts_exchange": 1_000_000,
        "ts_local": 2_000_000,
        "client_order_id": "C1",
        "broker_order_id": "B1",
        "fill_id": "F1",
        "strategy_id": "s1",
        "symbol": "2330",
        "side": "Buy",
        "qty": 3,
        "price_scaled": 530_0000,
        "fee_scaled": 10_0000,
        "tax_scaled": 5_0000,
    }
    result = _extract_fill_values(row)
    assert result is not None
    assert result[9] == 530_0000
    assert result[10] == 10_0000
    assert result[11] == 5_0000
