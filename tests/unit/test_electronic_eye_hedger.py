"""Tests for Hedger sub-engine logic."""
from __future__ import annotations
import pytest


def test_should_hedge_above_threshold():
    from hft_platform.strategies.electronic_eye import HedgerState
    h = HedgerState(delta_threshold_lots=3, cooldown_ms=1000, max_hedge_qty=10)
    assert h.should_hedge(hedge_lots=5, now_ns=1_000_000_000) is True


def test_should_not_hedge_below_threshold():
    from hft_platform.strategies.electronic_eye import HedgerState
    h = HedgerState(delta_threshold_lots=3, cooldown_ms=1000, max_hedge_qty=10)
    assert h.should_hedge(hedge_lots=2, now_ns=1_000_000_000) is False


def test_should_not_hedge_during_cooldown():
    from hft_platform.strategies.electronic_eye import HedgerState
    h = HedgerState(delta_threshold_lots=3, cooldown_ms=1000, max_hedge_qty=10)
    h.record_hedge(now_ns=1_000_000_000)
    assert h.should_hedge(hedge_lots=5, now_ns=1_500_000_000) is False


def test_should_hedge_after_cooldown():
    from hft_platform.strategies.electronic_eye import HedgerState
    h = HedgerState(delta_threshold_lots=3, cooldown_ms=1000, max_hedge_qty=10)
    h.record_hedge(now_ns=1_000_000_000)
    assert h.should_hedge(hedge_lots=5, now_ns=2_500_000_000) is True


def test_clamp_hedge_qty():
    from hft_platform.strategies.electronic_eye import HedgerState
    h = HedgerState(delta_threshold_lots=3, cooldown_ms=1000, max_hedge_qty=10)
    assert h.clamp_qty(15) == 10
    assert h.clamp_qty(-15) == -10
    assert h.clamp_qty(5) == 5


def test_hedge_side_positive_delta_sells():
    from hft_platform.contracts.strategy import Side
    from hft_platform.strategies.electronic_eye import HedgerState
    h = HedgerState(delta_threshold_lots=3, cooldown_ms=1000, max_hedge_qty=10)
    side, qty = h.hedge_direction(hedge_lots=5)
    assert side == Side.SELL and qty == 5


def test_hedge_side_negative_delta_buys():
    from hft_platform.contracts.strategy import Side
    from hft_platform.strategies.electronic_eye import HedgerState
    h = HedgerState(delta_threshold_lots=3, cooldown_ms=1000, max_hedge_qty=10)
    side, qty = h.hedge_direction(hedge_lots=-5)
    assert side == Side.BUY and qty == 5
