"""Tests for OutlierTradeRemovalGate."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from hft_platform.alpha._sub_gates.outlier_trade_removal import OutlierTradeRemovalGate


@dataclass
class _FakeResult:
    trade_pnl: list[float] = field(default_factory=list)
    daily_pnl: list[Any] = field(default_factory=list)


class TestOutlierTradeRemovalGate:
    def test_passes_when_sign_robust_to_drop(self) -> None:
        gate = OutlierTradeRemovalGate()
        r = _FakeResult(trade_pnl=[10.0] * 200)
        out = gate.evaluate(r, config=None, thresholds={"outlier_trade_removal_pct": 5.0})
        assert out.passed is True

    def test_fails_when_top_trades_carry_all_edge(self) -> None:
        # 5 large winners dominate; after dropping top 5% by |value|, residual flips negative.
        # total = 5*1000 + 195*(-1) = +4805  (positive, passes naive check)
        # after drop: 190 * (-1) = -190      (negative, sign-flip -> gate rejects)
        gate = OutlierTradeRemovalGate()
        trades = [1000.0] * 5 + [-1.0] * 195
        r = _FakeResult(trade_pnl=trades)
        out = gate.evaluate(r, config=None, thresholds={"outlier_trade_removal_pct": 5.0})
        assert out.passed is False
        assert out.metrics["pnl_after_drop"] < 0.0

    def test_falls_back_to_daily_when_no_trade_pnl(self) -> None:
        gate = OutlierTradeRemovalGate()
        r = _FakeResult(daily_pnl=[200.0, 1.0, 1.0, 1.0])
        out = gate.evaluate(r, config=None, thresholds={"outlier_trade_removal_pct": 25.0})
        assert out.passed is True

    def test_no_data_fails(self) -> None:
        gate = OutlierTradeRemovalGate()
        r = _FakeResult()
        out = gate.evaluate(r, config=None, thresholds={"outlier_trade_removal_pct": 5.0})
        assert out.passed is False
        assert "no trade or daily pnl" in out.details

    def test_zero_pct_passes(self) -> None:
        gate = OutlierTradeRemovalGate()
        r = _FakeResult(trade_pnl=[10.0, -1.0])
        out = gate.evaluate(r, config=None, thresholds={"outlier_trade_removal_pct": 0.0})
        assert out.passed is True
