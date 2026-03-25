"""Tests for PositionFlattener."""

from __future__ import annotations

from hft_platform.contracts.strategy import IntentType, Side
from hft_platform.ops.position_flattener import PositionFlattener


class TestPositionFlattener:
    def test_flatten_empty_positions_returns_empty(self) -> None:
        flattener = PositionFlattener()
        results = flattener.flatten_all({})
        assert results == []

    def test_flatten_zero_qty_skipped(self) -> None:
        flattener = PositionFlattener()
        results = flattener.flatten_all({"2330": 0})
        assert results == []

    def test_flatten_long_position_generates_sell(self) -> None:
        flattener = PositionFlattener()
        results = flattener.flatten_all({"2330": 10})
        assert len(results) == 1
        r = results[0]
        assert r.success is True
        assert r.symbol == "2330"
        assert r.qty == 10
        assert r.intent is not None
        assert r.intent.side == Side.SELL
        assert r.intent.intent_type == IntentType.FORCE_FLAT

    def test_flatten_short_position_generates_buy(self) -> None:
        flattener = PositionFlattener()
        results = flattener.flatten_all({"TXFD6": -5})
        assert len(results) == 1
        r = results[0]
        assert r.intent is not None
        assert r.intent.side == Side.BUY
        assert r.intent.qty == 5

    def test_flatten_multiple_positions(self) -> None:
        flattener = PositionFlattener()
        positions = {"2330": 10, "2317": -3, "2454": 0}
        results = flattener.flatten_all(positions)
        assert len(results) == 2
        symbols = {r.symbol for r in results}
        assert symbols == {"2330", "2317"}

    def test_flatten_result_to_dict(self) -> None:
        flattener = PositionFlattener()
        results = flattener.flatten_all({"2330": 1})
        d = results[0].to_dict()
        assert d["symbol"] == "2330"
        assert d["success"] is True
        assert d["qty"] == 1
