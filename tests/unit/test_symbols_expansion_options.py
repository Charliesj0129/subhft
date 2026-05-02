"""Tests for option-specific metadata enrichment in build_entry().

TDD tests written BEFORE the implementation. These should initially fail
with KeyError / missing fields, then pass after _enrich_option_entry() is added.
"""

from __future__ import annotations

from hft_platform.config._symbols_expansion import build_entry
from hft_platform.config._symbols_types import SymbolBuildResult


def _make_result() -> SymbolBuildResult:
    return SymbolBuildResult(symbols=[], errors=[], warnings=[])


# ---------------------------------------------------------------------------
# right field
# ---------------------------------------------------------------------------


def test_build_entry_option_populates_right_from_option_right() -> None:
    """option_right='OptionCall' in contract → entry['right'] == 'C'."""
    result = _make_result()
    contract = {
        "code": "TXO22500C6",
        "option_right": "OptionCall",
        "strike_price": 22500,
        "delivery_date": "2026-04-15",
    }
    entry = build_entry("TXO22500C6", {"product_type": "option"}, contract, result)
    assert entry is not None
    assert entry["right"] == "C"


# ---------------------------------------------------------------------------
# strike field
# ---------------------------------------------------------------------------


def test_build_entry_option_populates_strike() -> None:
    """contract with strike_price=22500 → entry['strike'] == 22500."""
    result = _make_result()
    contract = {
        "code": "TXO22500C6",
        "option_right": "OptionCall",
        "strike_price": 22500,
        "delivery_date": "2026-04-15",
    }
    entry = build_entry("TXO22500C6", {"product_type": "option"}, contract, result)
    assert entry is not None
    assert entry["strike"] == 22500


# ---------------------------------------------------------------------------
# expiry field
# ---------------------------------------------------------------------------


def test_build_entry_option_populates_expiry() -> None:
    """contract with delivery_date='2026-04-15' → entry['expiry'] == '2026-04-15'."""
    result = _make_result()
    contract = {
        "code": "TXO22500C6",
        "option_right": "OptionCall",
        "strike_price": 22500,
        "delivery_date": "2026-04-15",
    }
    entry = build_entry("TXO22500C6", {"product_type": "option"}, contract, result)
    assert entry is not None
    assert entry["expiry"] == "2026-04-15"


# ---------------------------------------------------------------------------
# point_value field
# ---------------------------------------------------------------------------


def test_build_entry_option_defaults_point_value() -> None:
    """TXO contract with no attrs point_value → entry['point_value'] == 50."""
    result = _make_result()
    contract = {
        "code": "TXO22500C6",
        "option_right": "OptionCall",
        "strike_price": 22500,
        "delivery_date": "2026-04-15",
    }
    entry = build_entry("TXO22500C6", {"product_type": "option"}, contract, result)
    assert entry is not None
    assert entry["point_value"] == 50


def test_build_entry_option_point_value_from_attrs() -> None:
    """attrs with point_value=100 takes precedence over default → entry['point_value'] == 100."""
    result = _make_result()
    contract = {
        "code": "TXO22500C6",
        "option_right": "OptionCall",
        "strike_price": 22500,
        "delivery_date": "2026-04-15",
    }
    entry = build_entry("TXO22500C6", {"product_type": "option", "point_value": 100}, contract, result)
    assert entry is not None
    assert entry["point_value"] == 100


# ---------------------------------------------------------------------------
# price_scale field
# ---------------------------------------------------------------------------


def test_build_entry_option_defaults_price_scale() -> None:
    """No price_scale in attrs or contract → defaults to 10000."""
    result = _make_result()
    contract = {
        "code": "TXO22500C6",
        "option_right": "OptionCall",
        "strike_price": 22500,
        "delivery_date": "2026-04-15",
    }
    entry = build_entry("TXO22500C6", {"product_type": "option"}, contract, result)
    assert entry is not None
    assert entry.get("price_scale", 10000) == 10000


# ---------------------------------------------------------------------------
# underlying field
# ---------------------------------------------------------------------------


def test_build_entry_option_underlying_mapping() -> None:
    """TXO code → entry['underlying'] == 'TX'."""
    result = _make_result()
    contract = {
        "code": "TXO22500C6",
        "option_right": "OptionCall",
        "strike_price": 22500,
        "delivery_date": "2026-04-15",
    }
    entry = build_entry("TXO22500C6", {"product_type": "option"}, contract, result)
    assert entry is not None
    assert entry["underlying"] == "TX"


# ---------------------------------------------------------------------------
# Warning on missing strike / expiry
# ---------------------------------------------------------------------------


def test_build_entry_option_warns_on_missing_strike() -> None:
    """Contract with no strike_price or strike → warning contains 'strike'."""
    result = _make_result()
    contract = {"code": "TXO22500C6", "option_right": "OptionCall", "delivery_date": "2026-04-15"}
    build_entry("TXO22500C6", {"product_type": "option"}, contract, result)
    assert any("strike" in w for w in result.warnings), f"No strike warning in: {result.warnings}"


def test_build_entry_option_warns_on_missing_expiry() -> None:
    """Contract with no delivery_date, expiry, expiry_date → warning contains 'expiry'."""
    result = _make_result()
    contract = {"code": "TXO22500C6", "option_right": "OptionCall", "strike_price": 22500}
    build_entry("TXO22500C6", {"product_type": "option"}, contract, result)
    assert any("expiry" in w for w in result.warnings), f"No expiry warning in: {result.warnings}"


# ---------------------------------------------------------------------------
# Non-option contract is not modified
# ---------------------------------------------------------------------------


def test_build_entry_non_option_unchanged() -> None:
    """Futures contract → 'right' and 'strike' keys should NOT be present."""
    result = _make_result()
    contract = {"code": "TXFD6", "delivery_date": "2026-06-18"}
    entry = build_entry("TXFD6", {"product_type": "futures", "exchange": "FUT"}, contract, result)
    assert entry is not None
    assert "right" not in entry
    assert "strike" not in entry
