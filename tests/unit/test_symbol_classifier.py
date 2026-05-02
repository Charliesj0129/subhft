"""Tests for the shared is_futures_symbol classifier."""

from __future__ import annotations

import pytest

from hft_platform.core.symbol_classifier import is_futures_symbol


@pytest.mark.parametrize(
    "symbol",
    [
        "TMFE6",
        "TMFD6",
        "TMFR1",
        "TXFE6",
        "TXFD6",
        "MXFE6",
        "MXFD6",
        "TEFE6",
        "TFFE6",
        "tmfe6",  # lowercase still matches
    ],
)
def test_futures_symbols_classified_true(symbol: str) -> None:
    assert is_futures_symbol(symbol) is True


@pytest.mark.parametrize(
    "symbol",
    [
        "2330",  # TSMC
        "0050",  # Yuanta ETF
        "2317",  # Hon Hai
        "1101",  # Taiwan Cement
        "9999",  # arbitrary stock code
    ],
)
def test_stock_symbols_classified_false(symbol: str) -> None:
    assert is_futures_symbol(symbol) is False


def test_options_symbols_still_match_heuristic() -> None:
    # TXO options codes embed "TX" so the heuristic treats them as futures-like;
    # this is consistent with the pre-existing reconciliation behavior.
    assert is_futures_symbol("TXO33600E6") is True


def test_empty_and_none_like_inputs_return_false() -> None:
    assert is_futures_symbol("") is False
