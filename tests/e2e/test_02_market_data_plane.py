"""E2E tests: Plane 2 — Market Data Plane.

Covers:
  Exchange → Normalizer → LOBEngine → FeatureEngine → RingBufferBus

TestChain (5 tests, mark ``e2e_chain``):
  Verifies each transform step in isolation with deterministic inputs.

TestIntegration (3 tests, mark ``e2e_integration``):
  Verifies the async MarketDataService wiring end-to-end.
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import numpy as np
import pytest
import pytest_asyncio

# Skip module if rust_core is unavailable.
rc = pytest.importorskip("hft_platform.rust_core")

from hft_platform.engine.event_bus import RingBufferBus
from hft_platform.events import BidAskEvent, LOBStatsEvent, TickEvent
from hft_platform.feature.engine import FeatureEngine
from hft_platform.feed_adapter.lob_engine import LOBEngine
from hft_platform.feed_adapter.normalizer import MarketDataNormalizer, SymbolMetadata

from .conftest import DEFAULT_TS_NS

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_PRICE = 500.0  # float price as the broker SDK would deliver
_SCALE = 10_000
_EXPECTED_PRICE = int(_PRICE * _SCALE)  # 5_000_000


def _make_symbols_yaml(tmp_path, *, use_code_key: bool = True) -> str:
    """Write a symbols.yaml that uses the ``code:`` key expected by SymbolMetadata._load()."""
    key = "code" if use_code_key else "symbol"
    content = f"""\
symbols:
  - {key}: "2330"
    exchange: TSE
    price_scale: {_SCALE}
    lot_size: 1000
    tick_size: 1
  - {key}: "TXFD6"
    exchange: TAIFEX
    price_scale: {_SCALE}
    lot_size: 1
    tick_size: 10000
"""
    path = tmp_path / "symbols.yaml"
    path.write_text(content)
    return str(path)


def _raw_tick(code: str = "2330") -> dict:
    return {
        "code": code,
        "close": _PRICE,
        "volume": 100,
        "total_volume": 5000,
        "ts": DEFAULT_TS_NS / 1e9,
        "simtrade": 0,
    }


def _raw_bidask(code: str = "2330") -> dict:
    return {
        "code": code,
        "bid_price": [500.0, 499.0, 498.0, 497.0, 496.0],
        "bid_volume": [100, 200, 300, 400, 500],
        "ask_price": [501.0, 502.0, 503.0, 504.0, 505.0],
        "ask_volume": [100, 200, 300, 400, 500],
        "ts": DEFAULT_TS_NS / 1e9,
    }


def _make_lob_stats_event(symbol: str = "2330") -> LOBStatsEvent:
    return LOBStatsEvent(
        symbol=symbol,
        ts=DEFAULT_TS_NS,
        imbalance=0.1,
        best_bid=_EXPECTED_PRICE - _SCALE,
        best_ask=_EXPECTED_PRICE + _SCALE,
        bid_depth=500,
        ask_depth=500,
    )


# ---------------------------------------------------------------------------
# TestChain — individual transform steps
# ---------------------------------------------------------------------------


@pytest.mark.e2e_chain
class TestChain:
    """Verify each transform step in the market data pipeline."""

    def test_tick_normalization_scaled_int(self, tmp_path) -> None:
        """Raw tick dict → TickEvent with price == close * 10_000."""
        yaml_path = _make_symbols_yaml(tmp_path)
        meta = SymbolMetadata(config_path=yaml_path)
        normalizer = MarketDataNormalizer(metadata=meta)

        result = normalizer.normalize_tick(_raw_tick())

        assert isinstance(result, TickEvent), f"Expected TickEvent, got {type(result)}"
        assert result.price == _EXPECTED_PRICE, (
            f"Expected {_EXPECTED_PRICE}, got {result.price}"
        )
        assert result.symbol == "2330"

    def test_bidask_normalization_book_shape(self, tmp_path) -> None:
        """Raw bidask dict → BidAskEvent with correct book shape and dtype."""
        yaml_path = _make_symbols_yaml(tmp_path)
        meta = SymbolMetadata(config_path=yaml_path)
        normalizer = MarketDataNormalizer(metadata=meta)

        result = normalizer.normalize_bidask(_raw_bidask())

        assert isinstance(result, BidAskEvent), f"Expected BidAskEvent, got {type(result)}"
        assert result.bids.shape == (5, 2), f"Expected (5,2), got {result.bids.shape}"
        assert result.bids.dtype == np.int64, f"Expected int64, got {result.bids.dtype}"
        # First bid price == 500 * 10_000
        assert result.bids[0, 0] == _EXPECTED_PRICE, (
            f"Expected {_EXPECTED_PRICE}, got {result.bids[0, 0]}"
        )

    def test_lob_engine_stats_computation(self, tmp_path) -> None:
        """BidAskEvent → LOBEngine → LOBStatsEvent with valid mid-price and spread."""
        yaml_path = _make_symbols_yaml(tmp_path)
        meta = SymbolMetadata(config_path=yaml_path)
        normalizer = MarketDataNormalizer(metadata=meta)

        bidask_event = normalizer.normalize_bidask(_raw_bidask())
        assert isinstance(bidask_event, BidAskEvent)

        lob = LOBEngine()
        result = lob.process_event(bidask_event)

        assert isinstance(result, LOBStatsEvent), (
            f"Expected LOBStatsEvent, got {type(result)}"
        )
        assert result.mid_price_x2 > 0, "mid_price_x2 should be positive"
        assert result.spread_scaled > 0, "spread_scaled should be positive"

    def test_feature_engine_27_features(self) -> None:
        """FeatureEngine(lob_shared_v3) exposes exactly 27 feature IDs."""
        engine = FeatureEngine(feature_set_id="lob_shared_v3")
        ids = engine.feature_ids()
        assert len(ids) == 27, f"Expected 27 features, got {len(ids)}: {ids}"

    def test_normalize_to_feature_full_chain(self, tmp_path) -> None:
        """Full chain: raw bidask → normalize → LOB → feature engine tracks symbol."""
        yaml_path = _make_symbols_yaml(tmp_path)
        meta = SymbolMetadata(config_path=yaml_path)
        normalizer = MarketDataNormalizer(metadata=meta)
        lob = LOBEngine()
        feature_engine = FeatureEngine(feature_set_id="lob_shared_v3")

        for _ in range(5):
            bidask_event = normalizer.normalize_bidask(_raw_bidask())
            assert isinstance(bidask_event, BidAskEvent)
            stats = lob.process_event(bidask_event)
            if isinstance(stats, LOBStatsEvent):
                feature_engine.process_lob_stats(stats)

        assert feature_engine.has_symbol("2330"), (
            "FeatureEngine should track symbol '2330' after 5 updates"
        )


# ---------------------------------------------------------------------------
# TestIntegration — async MarketDataService wiring
# ---------------------------------------------------------------------------


@pytest.mark.e2e_integration
class TestIntegration:
    """Verify MarketDataService async wiring with mocked broker client."""

    def _make_mock_client(self) -> MagicMock:
        client = MagicMock()
        client.login = AsyncMock(return_value=None)
        client.validate_symbols = AsyncMock(return_value=None)
        client.fetch_snapshots = AsyncMock(return_value=[])
        client.subscribe_basket = AsyncMock(return_value=None)
        return client

    @pytest.mark.asyncio
    async def test_md_service_publishes_to_bus(self, tmp_path, monkeypatch) -> None:
        """Injecting a raw tick tuple → TickEvent arrives on the bus."""
        monkeypatch.setenv("HFT_FEATURE_ENGINE_ENABLED", "0")

        yaml_path = _make_symbols_yaml(tmp_path)
        meta = SymbolMetadata(config_path=yaml_path)
        bus = RingBufferBus(size=1024)
        raw_queue: asyncio.Queue = asyncio.Queue(maxsize=64)
        client = self._make_mock_client()

        with patch("hft_platform.services.market_data.MetricsRegistry") as mock_reg_cls:
            mock_reg = MagicMock()
            mock_reg_cls.get.return_value = mock_reg
            # Stub out all metric attributes accessed in __init__ / run
            mock_reg.feed_events_total.labels.return_value = MagicMock()
            mock_reg.raw_queue_depth = MagicMock()
            mock_reg.raw_queue_dropped_total = MagicMock()
            mock_reg.process_raw_error_total = MagicMock()
            mock_reg.normalize_error_total = MagicMock()
            mock_reg.feed_last_event_ts.labels.return_value = MagicMock()

            from hft_platform.services.market_data import MarketDataService

            svc = MarketDataService(
                bus=bus,
                raw_queue=raw_queue,
                client=client,
                symbol_metadata=meta,
                publish_full_events=True,
            )

        task = asyncio.create_task(svc.run())
        try:
            # Give service time to reach the queue-read loop
            await asyncio.sleep(0.05)

            # Inject a raw tick tuple (exchange, payload)
            await raw_queue.put((None, _raw_tick()))

            # Collect events from bus
            events: list = []
            deadline = asyncio.get_event_loop().time() + 2.0
            async for event in bus.consume(start_cursor=-1):
                events.append(event)
                if any(isinstance(e, TickEvent) for e in events):
                    break
                if asyncio.get_event_loop().time() > deadline:
                    break

            tick_events = [e for e in events if isinstance(e, TickEvent)]
            assert tick_events, "No TickEvent arrived on the bus"
            tick = tick_events[0]
            assert tick.symbol == "2330"
            assert tick.price == _EXPECTED_PRICE
        finally:
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):
                pass

    @pytest.mark.asyncio
    async def test_md_service_bidask_lob_chain(self, tmp_path, monkeypatch) -> None:
        """Injecting a raw bidask → both BidAskEvent and LOBStatsEvent appear on bus."""
        monkeypatch.setenv("HFT_FEATURE_ENGINE_ENABLED", "0")

        yaml_path = _make_symbols_yaml(tmp_path)
        meta = SymbolMetadata(config_path=yaml_path)
        bus = RingBufferBus(size=1024)
        raw_queue: asyncio.Queue = asyncio.Queue(maxsize=64)
        client = self._make_mock_client()

        with patch("hft_platform.services.market_data.MetricsRegistry") as mock_reg_cls:
            mock_reg = MagicMock()
            mock_reg_cls.get.return_value = mock_reg
            mock_reg.feed_events_total.labels.return_value = MagicMock()
            mock_reg.raw_queue_depth = MagicMock()
            mock_reg.raw_queue_dropped_total = MagicMock()
            mock_reg.process_raw_error_total = MagicMock()
            mock_reg.normalize_error_total = MagicMock()
            mock_reg.feed_last_event_ts.labels.return_value = MagicMock()

            from hft_platform.services.market_data import MarketDataService

            svc = MarketDataService(
                bus=bus,
                raw_queue=raw_queue,
                client=client,
                symbol_metadata=meta,
                publish_full_events=True,
            )

        task = asyncio.create_task(svc.run())
        try:
            await asyncio.sleep(0.05)

            await raw_queue.put((None, _raw_bidask()))

            events: list = []
            deadline = asyncio.get_event_loop().time() + 2.0
            async for event in bus.consume(start_cursor=-1):
                events.append(event)
                has_bidask = any(isinstance(e, BidAskEvent) for e in events)
                has_lob = any(isinstance(e, LOBStatsEvent) for e in events)
                if has_bidask and has_lob:
                    break
                if asyncio.get_event_loop().time() > deadline:
                    break

            assert any(isinstance(e, BidAskEvent) for e in events), (
                "No BidAskEvent arrived on the bus"
            )
            assert any(isinstance(e, LOBStatsEvent) for e in events), (
                "No LOBStatsEvent arrived on the bus"
            )
        finally:
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):
                pass

    @pytest.mark.asyncio
    async def test_md_service_recorder_direct_write(self, tmp_path, monkeypatch) -> None:
        """Injecting a raw tick → recorder_queue receives a record."""
        monkeypatch.setenv("HFT_FEATURE_ENGINE_ENABLED", "0")
        monkeypatch.setenv("HFT_MD_RECORD_DIRECT", "1")

        yaml_path = _make_symbols_yaml(tmp_path)
        meta = SymbolMetadata(config_path=yaml_path)
        bus = RingBufferBus(size=1024)
        raw_queue: asyncio.Queue = asyncio.Queue(maxsize=64)
        recorder_queue: asyncio.Queue = asyncio.Queue(maxsize=64)
        client = self._make_mock_client()

        with patch("hft_platform.services.market_data.MetricsRegistry") as mock_reg_cls:
            mock_reg = MagicMock()
            mock_reg_cls.get.return_value = mock_reg
            mock_reg.feed_events_total.labels.return_value = MagicMock()
            mock_reg.raw_queue_depth = MagicMock()
            mock_reg.raw_queue_dropped_total = MagicMock()
            mock_reg.process_raw_error_total = MagicMock()
            mock_reg.normalize_error_total = MagicMock()
            mock_reg.feed_last_event_ts.labels.return_value = MagicMock()

            from hft_platform.services.market_data import MarketDataService

            svc = MarketDataService(
                bus=bus,
                raw_queue=raw_queue,
                client=client,
                symbol_metadata=meta,
                recorder_queue=recorder_queue,
                publish_full_events=True,
            )

        task = asyncio.create_task(svc.run())
        try:
            await asyncio.sleep(0.05)

            await raw_queue.put((None, _raw_tick()))

            # Wait for recorder_queue to receive something
            record = None
            try:
                record = await asyncio.wait_for(recorder_queue.get(), timeout=2.0)
            except asyncio.TimeoutError:
                pass

            assert record is not None, (
                "recorder_queue did not receive a record after injecting a tick"
            )
        finally:
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):
                pass
