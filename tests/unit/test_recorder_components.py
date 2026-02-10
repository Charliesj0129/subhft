import time

import pytest

from hft_platform.recorder.batcher import Batcher
from hft_platform.recorder.writer import DataWriter


class _Writer:
    def __init__(self):
        self.calls = []

    async def write(self, table, data):
        self.calls.append((table, list(data)))


@pytest.mark.asyncio
async def test_batcher_flush_on_limit():
    writer = _Writer()
    batcher = Batcher("hft.table", flush_limit=2, flush_interval_ms=10000, writer=writer)

    await batcher.add({"id": 1})
    assert writer.calls == []

    await batcher.add({"id": 2})
    assert writer.calls
    assert writer.calls[0][0] == "hft.table"
    assert len(writer.calls[0][1]) == 2


@pytest.mark.asyncio
async def test_batcher_flush_on_interval():
    writer = _Writer()
    batcher = Batcher("hft.table", flush_limit=10, flush_interval_ms=1, writer=writer)

    await batcher.add({"id": 1})
    batcher.last_flush_time = time.time() - 1

    await batcher.check_flush()
    assert writer.calls


@pytest.mark.asyncio
async def test_batcher_backpressure_drop_newest():
    writer = _Writer()
    batcher = Batcher(
        "hft.table",
        flush_limit=10,
        flush_interval_ms=10000,
        writer=writer,
        max_buffer_size=1,
        backpressure_policy="drop_newest",
    )

    await batcher.add({"id": 1})
    await batcher.add({"id": 2})

    assert batcher.dropped_count == 1
    assert len(batcher.buffer) == 1
    assert batcher.buffer[0]["id"] == 1


@pytest.mark.asyncio
async def test_batcher_backpressure_drop_oldest():
    writer = _Writer()
    batcher = Batcher(
        "hft.table",
        flush_limit=10,
        flush_interval_ms=10000,
        writer=writer,
        max_buffer_size=1,
        backpressure_policy="drop_oldest",
    )

    await batcher.add({"id": 1})
    await batcher.add({"id": 2})

    assert batcher.dropped_count == 1
    assert len(batcher.buffer) == 1
    assert batcher.buffer[0]["id"] == 2


@pytest.mark.asyncio
async def test_data_writer_wal_fallback(tmp_path, monkeypatch):
    monkeypatch.setenv("HFT_CLICKHOUSE_ENABLED", "0")
    monkeypatch.setenv("HFT_DISABLE_CLICKHOUSE", "1")

    writer = DataWriter(wal_dir=str(tmp_path))
    writer.connect()

    await writer.write("hft.orders", [{"order_id": "O1"}])

    files = list(tmp_path.glob("hft.orders_*.jsonl"))
    assert files, "WAL file should be written when ClickHouse disabled"
