"""Data Flow Verification Tests.

Validates end-to-end data flow integrity through the HFT pipeline:
1. Generate synthetic events
2. Process through WAL
3. Load into ClickHouse
4. Verify data integrity

Reference: data-flow-verify skill (4-step verification flow)
"""

import asyncio
import json
import os
import shutil
import tempfile
import time
import uuid

import pytest

# Skip entire module if ClickHouse driver not available
clickhouse_connect = pytest.importorskip("clickhouse_connect")


@pytest.fixture
def temp_wal_dir():
    """Create temporary WAL directory for testing."""
    path = tempfile.mkdtemp(prefix="hft_wal_test_")
    yield path
    shutil.rmtree(path, ignore_errors=True)


@pytest.fixture
def ch_client():
    """ClickHouse client fixture."""
    host = os.getenv("HFT_CLICKHOUSE_HOST", "localhost")
    port = int(os.getenv("HFT_CLICKHOUSE_PORT", "9000"))
    try:
        client = clickhouse_connect.get_client(host=host, port=port, username="default", password="")
        # Verify connection
        client.query("SELECT 1")
        return client
    except Exception as e:
        pytest.skip(f"ClickHouse not available: {e}")


def generate_test_tick(symbol: str, price_scaled: int, seq: int) -> dict:
    """Generate a test tick event in the expected schema format."""
    ts = time.time_ns()
    return {
        "symbol": symbol,
        "exchange": "TEST",
        "type": "Tick",
        "exch_ts": ts,
        "ingest_ts": ts,
        "price_scaled": price_scaled,
        "volume": 100,
        "bids_price": [price_scaled - 1000],
        "bids_vol": [50],
        "asks_price": [price_scaled + 1000],
        "asks_vol": [50],
        "seq_no": seq,
    }


def generate_test_order(strategy_id: str, symbol: str, price_scaled: int) -> dict:
    """Generate a test order event."""
    ts = time.time_ns()
    return {
        "order_id": str(uuid.uuid4()),
        "strategy_id": strategy_id,
        "symbol": symbol,
        "side": "BUY",
        "price_scaled": price_scaled,
        "qty": 10,
        "status": "NEW",
        "ingest_ts": ts,
        "latency_us": 150,
    }


@pytest.mark.integration
class TestDataFlowVerification:
    """End-to-end data flow verification tests."""

    def test_wal_write_read_integrity(self, temp_wal_dir):
        """Step 1: Verify WAL write and read integrity."""
        from hft_platform.recorder.wal import WALWriter

        writer = WALWriter(temp_wal_dir)
        test_data = [
            generate_test_tick("TEST_SYM", 1000000, i)
            for i in range(10)
        ]

        # Write synchronously
        ts = int(time.time_ns())
        filename = f"{temp_wal_dir}/market_data_{ts}.jsonl"
        writer._write_sync_atomic(filename, test_data)

        # Verify file exists and contents
        assert os.path.exists(filename), "WAL file should exist"

        read_data = []
        with open(filename, "r") as f:
            for line in f:
                read_data.append(json.loads(line))

        assert len(read_data) == len(test_data), "Read count should match write count"
        for orig, read in zip(test_data, read_data):
            assert orig["symbol"] == read["symbol"]
            assert orig["price_scaled"] == read["price_scaled"]
            assert orig["seq_no"] == read["seq_no"]

    @pytest.mark.asyncio
    async def test_wal_async_write(self, temp_wal_dir):
        """Step 1b: Verify async WAL write."""
        from hft_platform.recorder.wal import WALWriter

        writer = WALWriter(temp_wal_dir)
        test_data = [generate_test_tick("ASYNC_TEST", 2000000, i) for i in range(5)]

        await writer.write("market_data", test_data)

        # Verify file was created
        files = list(os.listdir(temp_wal_dir))
        wal_files = [f for f in files if f.startswith("market_data_") and f.endswith(".jsonl")]
        assert len(wal_files) == 1, "Should have one WAL file"

    def test_mapper_produces_scaled_prices(self):
        """Step 2: Verify mapper produces price_scaled fields."""
        from unittest.mock import MagicMock

        from hft_platform.events import TickEvent, TickMeta
        from hft_platform.recorder.mapper import map_event_to_record

        # Create mock metadata
        metadata = MagicMock()
        metadata.exchange.return_value = "TSE"
        metadata.price_scale.return_value = 100  # 2 decimal places

        # Create test event
        meta = TickMeta(source_ts=time.time_ns(), local_ts=time.time_ns(), seq=1)
        event = TickEvent(symbol="TEST", price=1000, volume=100, meta=meta)

        table, record = map_event_to_record(event, metadata)

        assert table == "market_data"
        assert "price_scaled" in record, "Should have price_scaled field"
        assert isinstance(record["price_scaled"], int), "price_scaled should be int"
        assert "price" not in record, "Should not have float price field"

    def test_clickhouse_schema_accepts_scaled_data(self, ch_client):
        """Step 3: Verify ClickHouse schema accepts scaled price data."""
        test_symbol = f"FLOW_TEST_{int(time.time())}"
        ts = time.time_ns()

        # Insert test record with scaled prices
        data = [
            [
                test_symbol,  # symbol
                "TEST",  # exchange
                "Tick",  # type
                ts,  # exch_ts
                ts,  # ingest_ts
                1234567,  # price_scaled (Int64)
                100,  # volume (Int64)
                [1234000],  # bids_price (Array(Int64))
                [50],  # bids_vol
                [1235000],  # asks_price (Array(Int64))
                [50],  # asks_vol
                1,  # seq_no
            ]
        ]
        cols = [
            "symbol", "exchange", "type", "exch_ts", "ingest_ts",
            "price_scaled", "volume", "bids_price", "bids_vol",
            "asks_price", "asks_vol", "seq_no"
        ]

        ch_client.insert("hft.market_data", data, column_names=cols)

        # Query back
        result = ch_client.query(
            f"SELECT price_scaled, bids_price, asks_price FROM hft.market_data WHERE symbol = '{test_symbol}'"
        )

        assert len(result.result_rows) == 1
        row = result.result_rows[0]
        assert row[0] == 1234567, "price_scaled should match"
        assert row[1] == [1234000], "bids_price array should match"
        assert row[2] == [1235000], "asks_price array should match"

        # Cleanup
        ch_client.command(f"ALTER TABLE hft.market_data DELETE WHERE symbol = '{test_symbol}'")

    def test_orders_table_accepts_scaled_prices(self, ch_client):
        """Step 3b: Verify orders table schema."""
        test_order_id = f"ORDER_{int(time.time())}"
        ts = time.time_ns()

        data = [
            [
                test_order_id,  # order_id
                "test_strategy",  # strategy_id
                "TEST_SYM",  # symbol
                "BUY",  # side
                1000000,  # price_scaled
                10,  # qty
                "NEW",  # status
                ts,  # ingest_ts
                250,  # latency_us (Int64)
            ]
        ]
        cols = [
            "order_id", "strategy_id", "symbol", "side",
            "price_scaled", "qty", "status", "ingest_ts", "latency_us"
        ]

        ch_client.insert("hft.orders", data, column_names=cols)

        result = ch_client.query(
            f"SELECT price_scaled, latency_us FROM hft.orders WHERE order_id = '{test_order_id}'"
        )

        assert len(result.result_rows) == 1
        assert result.result_rows[0][0] == 1000000
        assert result.result_rows[0][1] == 250

        # Cleanup
        ch_client.command(f"ALTER TABLE hft.orders DELETE WHERE order_id = '{test_order_id}'")


@pytest.mark.integration
class TestMaterializedViewIntegrity:
    """Verify materialized views correctly aggregate data."""

    def test_ohlcv_mv_structure(self, ch_client):
        """Verify OHLCV materialized view has correct columns."""
        result = ch_client.query("DESCRIBE TABLE hft.ohlcv_1m")
        columns = {row[0]: row[1] for row in result.result_rows}

        assert "open_scaled" in columns, "Should have open_scaled column"
        assert "high_scaled" in columns, "Should have high_scaled column"
        assert "low_scaled" in columns, "Should have low_scaled column"
        assert "close_scaled" in columns, "Should have close_scaled column"
        assert "Int64" in columns.get("open_scaled", ""), "open_scaled should be Int64"

    def test_latency_stats_mv_structure(self, ch_client):
        """Verify latency stats materialized view has correct columns."""
        result = ch_client.query("DESCRIBE TABLE hft.latency_stats_1m")
        columns = {row[0]: row[1] for row in result.result_rows}

        assert "latency_p50" in columns
        assert "latency_p95" in columns
        assert "latency_p99" in columns
        assert "latency_max" in columns


@pytest.mark.integration
class TestBackpressureHandling:
    """Verify backpressure mechanisms work correctly."""

    @pytest.mark.asyncio
    async def test_batcher_backpressure(self):
        """Test batcher handles buffer overflow gracefully."""
        from unittest.mock import AsyncMock

        from hft_platform.recorder.batcher import BackpressurePolicy, Batcher

        mock_writer = AsyncMock()
        batcher = Batcher(
            table_name="test",
            flush_limit=100,
            max_buffer_size=50,  # Small buffer for testing
            backpressure_policy=BackpressurePolicy.DROP_OLDEST,
            writer=mock_writer,
        )

        # Add more items than buffer size
        for i in range(100):
            await batcher.add({"id": i, "data": "test"})

        # Buffer should not exceed max_buffer_size
        assert len(batcher.buffer) <= batcher.max_buffer_size
        assert batcher.dropped_count > 0, "Should have dropped some entries"

    @pytest.mark.asyncio
    async def test_bounded_queue_behavior(self):
        """Test bounded queue rejects when full."""
        queue: asyncio.Queue = asyncio.Queue(maxsize=5)

        # Fill queue
        for i in range(5):
            await queue.put(i)

        assert queue.full()

        # Try to put without blocking should raise
        with pytest.raises(asyncio.QueueFull):
            queue.put_nowait(99)
