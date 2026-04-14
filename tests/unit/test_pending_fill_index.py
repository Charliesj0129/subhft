"""Tests for pending fill index strategy_id resolution.

Verifies the two-step bypass that resolves strategy_id for deal callbacks
when order_id_map has no seed data (Shioaji futures: place_order returns
empty broker IDs).
"""

import asyncio
from typing import Any
from unittest.mock import MagicMock, patch

from hft_platform.execution.normalizer import ExecutionNormalizer, RawExecEvent
from hft_platform.order.adapter import OrderAdapter

# ---------------------------------------------------------------------------
# OrderAdapter.resolve_strategy_from_deal
# ---------------------------------------------------------------------------


@patch("hft_platform.order.adapter.OrderAdapter.load_config")
class TestResolveStrategyFromDeal:
    def _make_adapter(self, mock_load: Any) -> OrderAdapter:
        queue = asyncio.Queue()
        client = MagicMock()
        adapter = OrderAdapter("config/dummy.yaml", queue, client)
        return adapter

    def test_resolves_correct_strategy_id(self, mock_load: Any) -> None:
        adapter = self._make_adapter(mock_load)
        adapter._pending_fill_index["TMFD6:SELL"] = ["r47:intent_001"]

        result = adapter.resolve_strategy_from_deal("TMFD6", "Sell")

        assert result == "r47"

    def test_consumes_entry_on_resolve(self, mock_load: Any) -> None:
        adapter = self._make_adapter(mock_load)
        adapter._pending_fill_index["TMFD6:SELL"] = ["r47:intent_001"]

        first = adapter.resolve_strategy_from_deal("TMFD6", "Sell")
        second = adapter.resolve_strategy_from_deal("TMFD6", "Sell")

        assert first == "r47"
        assert second is None
        assert "TMFD6:SELL" not in adapter._pending_fill_index

    def test_fifo_order_multiple_pending(self, mock_load: Any) -> None:
        adapter = self._make_adapter(mock_load)
        adapter._pending_fill_index["TMFD6:BUY"] = [
            "strat_a:001",
            "strat_b:002",
        ]

        first = adapter.resolve_strategy_from_deal("TMFD6", "Buy")
        second = adapter.resolve_strategy_from_deal("TMFD6", "Buy")
        third = adapter.resolve_strategy_from_deal("TMFD6", "Buy")

        assert first == "strat_a"
        assert second == "strat_b"
        assert third is None

    def test_returns_none_for_unknown_symbol(self, mock_load: Any) -> None:
        adapter = self._make_adapter(mock_load)
        adapter._pending_fill_index["TMFD6:SELL"] = ["r47:intent_001"]

        result = adapter.resolve_strategy_from_deal("TXFD6", "Sell")

        assert result is None

    def test_returns_none_for_wrong_side(self, mock_load: Any) -> None:
        adapter = self._make_adapter(mock_load)
        adapter._pending_fill_index["TMFD6:SELL"] = ["r47:intent_001"]

        result = adapter.resolve_strategy_from_deal("TMFD6", "Buy")

        assert result is None

    def test_action_case_insensitive(self, mock_load: Any) -> None:
        adapter = self._make_adapter(mock_load)
        adapter._pending_fill_index["TMFD6:BUY"] = ["r47:intent_001"]

        result = adapter.resolve_strategy_from_deal("TMFD6", "buy")

        assert result == "r47"

    def test_order_key_without_colon(self, mock_load: Any) -> None:
        """If order_key has no colon, return it as-is as strategy_id."""
        adapter = self._make_adapter(mock_load)
        adapter._pending_fill_index["TMFD6:SELL"] = ["r47"]

        result = adapter.resolve_strategy_from_deal("TMFD6", "Sell")

        assert result == "r47"


# ---------------------------------------------------------------------------
# ExecutionNormalizer._resolve_from_injected
# ---------------------------------------------------------------------------


class TestResolveFromInjected:
    def test_resolves_injected_strategy_id(self) -> None:
        norm = ExecutionNormalizer()
        raw = RawExecEvent(
            topic="deal",
            data={"_resolved_strategy_id": "r47", "payload": {"price": 100}},
            ingest_ts_ns=0,
        )

        result = norm._resolve_from_injected(raw)

        assert result == "r47"

    def test_returns_none_when_not_injected(self) -> None:
        norm = ExecutionNormalizer()
        raw = RawExecEvent(
            topic="deal",
            data={"payload": {"price": 100}},
            ingest_ts_ns=0,
        )

        result = norm._resolve_from_injected(raw)

        assert result is None

    def test_injected_resolver_is_highest_priority(self) -> None:
        """Verify _resolve_from_injected is first in resolver chain."""
        norm = ExecutionNormalizer()

        assert norm.strategy_id_resolvers[0] == norm._resolve_from_injected

    def test_resolve_strategy_id_uses_injected_over_order_id_map(self) -> None:
        """End-to-end: injected value wins over order_id_map."""
        order_id_map = {"vA0Ln": "other_strat:999"}
        norm = ExecutionNormalizer(order_id_map=order_id_map)
        raw = RawExecEvent(
            topic="deal",
            data={
                "_resolved_strategy_id": "r47",
                "payload": {"ordno": "vA0Ln", "price": 100},
            },
            ingest_ts_ns=0,
        )

        result = norm._resolve_strategy_id(raw)

        assert result == "r47"

    def test_falls_back_to_order_id_map_when_not_injected(self) -> None:
        """When no injected value, falls back to order_id_map resolver."""
        order_id_map = {"vA0Ln": "other_strat:999"}
        norm = ExecutionNormalizer(order_id_map=order_id_map)
        raw = RawExecEvent(
            topic="deal",
            data={"payload": {"ordno": "vA0Ln", "price": 100}},
            ingest_ts_ns=0,
        )

        result = norm._resolve_strategy_id(raw)

        # OrderIdResolver extracts strategy_id from "strategy_id:intent_id"
        assert result == "other_strat"
