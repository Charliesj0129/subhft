"""Tests for FeeCalculator — pure integer arithmetic fee computation."""

from __future__ import annotations

from pathlib import Path

import pytest

from hft_platform.tca.fee_calculator import FeeCalculator
from hft_platform.tca.types import FeeBreakdown

# --- fixtures ---

_FUTURES_YAML = """\
futures:
  TX:
    commission_per_contract: 60
    tax_rate_bps: 2.0
    tax_side: sell
    tick_size: 1
    point_value: 200

  MTX:
    commission_per_contract: 30
    tax_rate_bps: 2.0
    tax_side: sell
    tick_size: 1
    point_value: 50

  XMT:
    commission_per_contract: 13
    tax_per_contract: 7
    tax_side: both
    tick_size: 1
    point_value: 10

symbol_map:
  TXF: TX
  TXFL5: TX
  TXFR1: TX
  MXF: MTX
  MXFR1: MTX
  XMT: XMT
"""


@pytest.fixture()
def yaml_path(tmp_path: Path) -> Path:
    p = tmp_path / "futures.yaml"
    p.write_text(_FUTURES_YAML)
    return p


@pytest.fixture()
def calc(yaml_path: Path) -> FeeCalculator:
    return FeeCalculator.from_yaml(str(yaml_path))


# --- tests ---


def test_buy_side_no_tax(calc: FeeCalculator) -> None:
    """Buy side should have zero tax (tax_side = sell only)."""
    result = calc.compute("TXF", "BUY", 1, 20000 * 10000)
    assert result.tax == 0
    assert result.commission == 60 * 10000  # 60 NTD * 10000
    assert result.total == result.commission


def test_sell_side_has_tax(calc: FeeCalculator) -> None:
    """Sell side should include tax."""
    # TX: price=20000 (scaled x10000 → 200000000), qty=1, point_value=200, tick_size=1
    # notional = price * qty * point_value / tick_size = 20000 * 1 * 200 / 1 = 4_000_000 NTD
    # tax = notional * 2.0 bps = 4_000_000 * 0.0002 = 800 NTD → 800 * 10000 = 8_000_000
    price_scaled = 20000 * 10000  # 20000 points scaled x10000
    result = calc.compute("TXF", "SELL", 1, price_scaled)
    assert result.tax > 0
    assert result.commission == 60 * 10000
    # tax: notional_x100 = price_scaled * qty * point_value * 100 // tick_size_x100
    #     = 200_000_000 * 1 * 200 * 100 // 100 = 40_000_000_000
    # tax_scaled = notional_x100 * tax_rate_x100 // (10000 * 100)
    #     = 40_000_000_000 * 200 // 1_000_000 = 8_000_000
    assert result.tax == 8_000_000
    assert result.total == result.commission + result.tax


def test_unknown_symbol_returns_zero(calc: FeeCalculator) -> None:
    """Unknown symbol should return zero fees."""
    result = calc.compute("UNKNOWN_SYM", "SELL", 5, 10000 * 10000)
    assert result.commission == 0
    assert result.tax == 0
    assert result.total == 0


def test_mtx_lower_commission(calc: FeeCalculator) -> None:
    """MTX has lower commission than TX."""
    result = calc.compute("MXF", "BUY", 1, 20000 * 10000)
    assert result.commission == 30 * 10000


def test_contract_month_symbol_resolves(calc: FeeCalculator) -> None:
    """TXFL5 should resolve to TX product code."""
    result = calc.compute("TXFL5", "BUY", 2, 20000 * 10000)
    assert result.commission == 60 * 2 * 10000


def test_zero_qty(calc: FeeCalculator) -> None:
    """Zero quantity should produce zero fees."""
    result = calc.compute("TXF", "SELL", 0, 20000 * 10000)
    assert result.commission == 0
    assert result.tax == 0
    assert result.total == 0


def test_result_type(calc: FeeCalculator) -> None:
    """Result must be FeeBreakdown instance."""
    result = calc.compute("TXF", "BUY", 1, 20000 * 10000)
    assert isinstance(result, FeeBreakdown)


def test_tax_uses_integer_arithmetic(calc: FeeCalculator) -> None:
    """All fee fields must be int (no float on live path)."""
    result = calc.compute("TXF", "SELL", 3, 19999 * 10000)
    assert isinstance(result.commission, int)
    assert isinstance(result.tax, int)
    assert isinstance(result.total, int)


def test_from_yaml(yaml_path: Path) -> None:
    """from_yaml classmethod should load and construct correctly."""
    calc = FeeCalculator.from_yaml(str(yaml_path))
    assert calc is not None
    # Verify TX schedule loaded
    result = calc.compute("TXF", "BUY", 1, 20000 * 10000)
    assert result.commission == 60 * 10000


def test_xmt_buy_has_tax_both_sides(calc: FeeCalculator) -> None:
    """XMT BUY should have tax because tax_side=both."""
    result = calc.compute("XMT", "BUY", 1, 100 * 10000)
    assert result.tax > 0
    assert result.commission == 13 * 10000


def test_xmt_sell_has_tax_both_sides(calc: FeeCalculator) -> None:
    """XMT SELL should have tax because tax_side=both."""
    result = calc.compute("XMT", "SELL", 1, 100 * 10000)
    assert result.tax > 0
    assert result.commission == 13 * 10000


def test_xmt_flat_tax_per_contract(calc: FeeCalculator) -> None:
    """XMT tax = 7 * qty * 10000 (flat per-contract, not percentage-based)."""
    qty = 3
    result = calc.compute("XMT", "BUY", qty, 100 * 10000)
    expected_tax = 7 * qty * 10000
    assert result.tax == expected_tax
    expected_commission = 13 * qty * 10000
    assert result.commission == expected_commission
    assert result.total == expected_commission + expected_tax


def test_unknown_symbol_logs_warning(calc: FeeCalculator) -> None:
    """Unknown symbol should return zero fees and log a warning."""
    import unittest.mock as mock

    with mock.patch("hft_platform.tca.fee_calculator.logger") as mock_logger:
        result = calc.compute("NOSUCHSYM", "BUY", 1, 10000 * 10000)
    assert result.commission == 0
    assert result.tax == 0
    assert result.total == 0
    mock_logger.warning.assert_called_once()
    call_kwargs = mock_logger.warning.call_args
    assert "unknown_symbol" in call_kwargs[0][0]
