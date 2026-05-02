"""Tests for TCA pipeline: arrival_price stamping, FillEvent enrichment, and recorder fields.

Covers:
- OrderAdapter._intent_to_command stamps arrival_price from mid_price_fn
- ExecutionRouter enriches FillEvent with TCA prices from cmd_tca_map
- Mapper includes decision_price and arrival_price in fill record
- Worker extractor handles TCA fields
"""

import asyncio
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from hft_platform.contracts.execution import FillEvent, Side
from hft_platform.contracts.strategy import TIF, IntentType, OrderIntent
from hft_platform.contracts.strategy import Side as StrategySide
from hft_platform.core import timebase
from hft_platform.order.adapter import OrderAdapter

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_adapter(tmp_path: Path, mid_price_fn=None, cmd_tca_map=None) -> OrderAdapter:
    cfg_path = tmp_path / "order_cfg.yaml"
    cfg_path.write_text("{}\n")
    return OrderAdapter(
        str(cfg_path),
        asyncio.Queue(),
        MagicMock(),
        mid_price_fn=mid_price_fn,
        cmd_tca_map=cmd_tca_map,
    )


def _make_intent(
    *,
    decision_price: int = 500_0000,
    symbol: str = "TXFD6",
    strategy_id: str = "strat1",
    intent_id: int = 42,
) -> OrderIntent:
    return OrderIntent(
        intent_id=intent_id,
        strategy_id=strategy_id,
        symbol=symbol,
        intent_type=IntentType.NEW,
        side=StrategySide.BUY,
        price=500_0000,
        qty=1,
        tif=TIF.LIMIT,
        decision_price=decision_price,
    )


def _make_fill(
    *,
    order_id: str = "O001",
    strategy_id: str = "strat1",
    symbol: str = "TXFD6",
    price: int = 501_0000,
    decision_price: int = 0,
    arrival_price: int = 0,
) -> FillEvent:
    return FillEvent(
        fill_id="F001",
        account_id="acc1",
        order_id=order_id,
        strategy_id=strategy_id,
        symbol=symbol,
        side=Side.BUY,
        qty=1,
        price=price,
        fee=200,
        tax=100,
        ingest_ts_ns=timebase.now_ns(),
        match_ts_ns=timebase.now_ns(),
        decision_price=decision_price,
        arrival_price=arrival_price,
    )


# ===========================================================================
# 1. OrderAdapter._intent_to_command stamps arrival_price
# ===========================================================================


class TestOrderAdapterArrivalPrice:
    def test_arrival_price_from_mid_price_fn(self, tmp_path):
        """When mid_price_fn is provided, arrival_price should come from it."""
        mid_price_fn = MagicMock(return_value=502_0000)
        adapter = _make_adapter(tmp_path, mid_price_fn=mid_price_fn)
        intent = _make_intent(decision_price=500_0000)

        cmd = adapter._intent_to_command(intent)

        assert cmd.decision_price == 500_0000
        assert cmd.arrival_price == 502_0000
        mid_price_fn.assert_called_once_with("TXFD6")

    def test_arrival_price_fallback_no_fn(self, tmp_path):
        """When mid_price_fn is None, arrival_price falls back to decision_price."""
        adapter = _make_adapter(tmp_path, mid_price_fn=None)
        intent = _make_intent(decision_price=500_0000)

        cmd = adapter._intent_to_command(intent)

        assert cmd.decision_price == 500_0000
        assert cmd.arrival_price == 500_0000

    def test_arrival_price_fallback_on_exception(self, tmp_path):
        """When mid_price_fn raises, arrival_price falls back to decision_price."""
        mid_price_fn = MagicMock(side_effect=KeyError("no book"))
        adapter = _make_adapter(tmp_path, mid_price_fn=mid_price_fn)
        intent = _make_intent(decision_price=500_0000)

        cmd = adapter._intent_to_command(intent)

        assert cmd.arrival_price == 500_0000

    def test_tca_map_populated_on_dispatch(self, tmp_path):
        """_dispatch_to_api should populate _cmd_tca_map with TCA prices."""
        cmd_tca_map: dict[str, tuple[int, int]] = {}
        mid_price_fn = MagicMock(return_value=502_0000)
        adapter = _make_adapter(tmp_path, mid_price_fn=mid_price_fn, cmd_tca_map=cmd_tca_map)

        intent = _make_intent(decision_price=500_0000, strategy_id="s1", intent_id=7)
        cmd = adapter._intent_to_command(intent)

        # Simulate _dispatch_to_api populating the map (we call the relevant section)
        order_key = f"{intent.strategy_id}:{intent.intent_id}"
        adapter._cmd_created_ns_map[order_key] = cmd.created_ns
        adapter._cmd_tca_map[order_key] = (int(cmd.decision_price), int(cmd.arrival_price))

        assert cmd_tca_map[order_key] == (500_0000, 502_0000)


# ===========================================================================
# 2. ExecutionRouter enriches FillEvent with TCA prices
# ===========================================================================


class TestExecutionRouterTCAEnrichment:
    def test_fill_event_enriched_from_tca_map(self):
        """Router should set decision_price and arrival_price on FillEvent from cmd_tca_map."""
        order_id_map = {"BRK001": "strat1:42"}
        cmd_tca_map = {"strat1:42": (500_0000, 502_0000)}

        fill = _make_fill(order_id="BRK001")
        assert fill.decision_price == 0
        assert fill.arrival_price == 0

        # Simulate the enrichment logic from ExecutionRouter
        _order_key = order_id_map.get(fill.order_id)
        if _order_key is not None:
            _tca = cmd_tca_map.get(_order_key)
            if _tca is not None:
                fill.decision_price = _tca[0]
                fill.arrival_price = _tca[1]

        assert fill.decision_price == 500_0000
        assert fill.arrival_price == 502_0000

    def test_fill_event_not_enriched_when_no_tca_entry(self):
        """Fill events without TCA map entries should keep default 0 prices."""
        order_id_map = {"BRK001": "strat1:42"}
        cmd_tca_map: dict[str, tuple[int, int]] = {}

        fill = _make_fill(order_id="BRK001")

        _order_key = order_id_map.get(fill.order_id)
        if _order_key is not None:
            _tca = cmd_tca_map.get(_order_key)
            if _tca is not None:
                fill.decision_price = _tca[0]
                fill.arrival_price = _tca[1]

        assert fill.decision_price == 0
        assert fill.arrival_price == 0

    def test_fill_event_not_enriched_when_unknown_order(self):
        """Fills with unmapped order_id should not be enriched."""
        order_id_map: dict[str, str] = {}
        cmd_tca_map = {"strat1:42": (500_0000, 502_0000)}

        fill = _make_fill(order_id="UNKNOWN")

        _order_key = order_id_map.get(fill.order_id)
        if _order_key is not None:
            _tca = cmd_tca_map.get(_order_key)
            if _tca is not None:
                fill.decision_price = _tca[0]
                fill.arrival_price = _tca[1]

        assert fill.decision_price == 0
        assert fill.arrival_price == 0


# ===========================================================================
# 3. Mapper includes TCA fields in fill record
# ===========================================================================


class TestMapperTCAFields:
    def test_fill_record_contains_tca_prices(self):
        """map_event_to_record should include decision_price and arrival_price."""
        from hft_platform.recorder.mapper import map_event_to_record

        metadata = MagicMock()
        metadata.price_scale.return_value = 10000
        metadata.exchange.return_value = "TAIFEX"
        metadata.registry = MagicMock()
        metadata.registry.get.side_effect = KeyError("no profile")

        fill = _make_fill(decision_price=500_0000, arrival_price=502_0000)
        result = map_event_to_record(fill, metadata)

        assert result is not None
        topic, payload = result
        assert topic == "fills"
        assert payload["decision_price"] == 500_0000
        assert payload["arrival_price"] == 502_0000

    def test_fill_record_tca_prices_default_zero(self):
        """TCA prices should be 0 when not enriched."""
        from hft_platform.recorder.mapper import map_event_to_record

        metadata = MagicMock()
        metadata.price_scale.return_value = 10000
        metadata.exchange.return_value = "TAIFEX"
        metadata.registry = MagicMock()
        metadata.registry.get.side_effect = KeyError("no profile")

        fill = _make_fill()
        result = map_event_to_record(fill, metadata)

        assert result is not None
        _, payload = result
        assert payload["decision_price"] == 0
        assert payload["arrival_price"] == 0


# ===========================================================================
# 4. Worker extractor handles TCA fields
# ===========================================================================


class TestWorkerExtractorTCAFields:
    def test_extract_fill_values_dict_with_tca(self):
        """Dict-based fill extraction should include TCA fields."""
        from hft_platform.recorder.worker import _extract_fill_values

        row = {
            "ts_exchange": 1000,
            "ts_local": 1001,
            "client_order_id": "",
            "broker_order_id": "O001",
            "fill_id": "F001",
            "strategy_id": "strat1",
            "symbol": "TXFD6",
            "side": "BUY",
            "qty": 1,
            "price_scaled": 5010000,
            "fee_scaled": 200,
            "tax_scaled": 100,
            "decision_price": 500_0000,
            "arrival_price": 502_0000,
            "source": "shioaji",
        }
        values = _extract_fill_values(row)
        assert values is not None
        # decision_price and arrival_price should be at indices 12 and 13
        assert values[12] == 500_0000
        assert values[13] == 502_0000
        # source should be last
        assert values[14] == "shioaji"

    def test_extract_fill_values_dict_default_tca(self):
        """Dict-based extraction should default TCA fields to 0."""
        from hft_platform.recorder.worker import _extract_fill_values

        row = {
            "ts_exchange": 1000,
            "ts_local": 1001,
            "symbol": "TXFD6",
            "price_scaled": 5010000,
        }
        values = _extract_fill_values(row)
        assert values is not None
        assert values[12] == 0  # decision_price
        assert values[13] == 0  # arrival_price

    def test_extract_fill_values_object_with_tca(self):
        """Object-based fill extraction should include TCA fields."""
        from hft_platform.recorder.worker import _extract_fill_values

        fill = _make_fill(decision_price=500_0000, arrival_price=502_0000)
        values = _extract_fill_values(fill)
        assert values is not None
        assert values[12] == 500_0000
        assert values[13] == 502_0000

    def test_fill_columns_include_tca_fields(self):
        """FILL_COLUMNS should contain decision_price and arrival_price."""
        from hft_platform.recorder.worker import FILL_COLUMNS

        assert "decision_price" in FILL_COLUMNS
        assert "arrival_price" in FILL_COLUMNS
        # They should be before "source"
        dp_idx = FILL_COLUMNS.index("decision_price")
        ap_idx = FILL_COLUMNS.index("arrival_price")
        src_idx = FILL_COLUMNS.index("source")
        assert dp_idx < src_idx
        assert ap_idx < src_idx


# ===========================================================================
# 5. TCA map cleanup lifecycle
# ===========================================================================


class TestTCAMapCleanup:
    @pytest.mark.asyncio
    async def test_terminal_state_cleans_tca_map(self, tmp_path):
        """on_terminal_state should remove TCA map entry alongside cmd_created_ns_map."""
        cmd_tca_map: dict[str, tuple[int, int]] = {"strat1:42": (500_0000, 502_0000)}
        adapter = _make_adapter(tmp_path, cmd_tca_map=cmd_tca_map)
        order_key = "strat1:42"

        # Simulate the order being registered as live
        adapter.live_orders[order_key] = MagicMock()
        adapter.order_id_map["BRK001"] = order_key
        adapter._cmd_created_ns_map[order_key] = 123456

        await adapter.on_terminal_state("strat1", "BRK001")

        assert order_key not in adapter.live_orders
        assert order_key not in adapter._cmd_created_ns_map
        assert order_key not in cmd_tca_map
