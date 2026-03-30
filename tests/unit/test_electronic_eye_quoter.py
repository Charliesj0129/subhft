"""Tests for Quoter sub-engine logic."""
from __future__ import annotations

import pytest


def test_compute_theo_and_edge():
    from hft_platform.strategies.electronic_eye import _compute_edge
    edge = _compute_edge(theo_price=100.0, market_bid=97.0, market_ask=99.0, min_edge_ticks=2, tick_size=1.0)
    assert edge.has_bid_edge is True
    assert edge.has_ask_edge is True
    assert edge.bid_price == pytest.approx(98.0)
    assert edge.ask_price == pytest.approx(102.0)


def test_compute_edge_no_edge():
    from hft_platform.strategies.electronic_eye import _compute_edge
    edge = _compute_edge(theo_price=100.0, market_bid=99.0, market_ask=101.0, min_edge_ticks=2, tick_size=1.0)
    assert edge.has_bid_edge is False
    assert edge.has_ask_edge is False


def test_scale_price_to_int():
    from hft_platform.strategies.electronic_eye import _scale_to_int
    assert _scale_to_int(100.5, 10000) == 1005000
    assert isinstance(_scale_to_int(100.5, 10000), int)


def test_scale_price_to_int_zero():
    from hft_platform.strategies.electronic_eye import _scale_to_int
    assert _scale_to_int(0.0, 10000) == 0


def test_quoter_respects_max_contracts():
    from hft_platform.strategies.electronic_eye import QuoterState
    qs = QuoterState(max_contracts_per_strike=5)
    qs.record_quote("TXO20000D6", qty=3)
    qs.record_quote("TXO20000D6", qty=3)
    assert qs.current_qty("TXO20000D6") == 6
    assert qs.can_quote("TXO20000D6", additional=1) is False


def test_quoter_allows_within_limit():
    from hft_platform.strategies.electronic_eye import QuoterState
    qs = QuoterState(max_contracts_per_strike=5)
    qs.record_quote("TXO20000D6", qty=2)
    assert qs.can_quote("TXO20000D6", additional=3) is True


def test_quoter_cancel_reduces_qty():
    from hft_platform.strategies.electronic_eye import QuoterState
    qs = QuoterState(max_contracts_per_strike=5)
    qs.record_quote("TXO20000D6", qty=4)
    qs.record_cancel("TXO20000D6", qty=2)
    assert qs.current_qty("TXO20000D6") == 2
    assert qs.can_quote("TXO20000D6", additional=3) is True
