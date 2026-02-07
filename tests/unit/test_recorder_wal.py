import asyncio
import json
from pathlib import Path

import pytest

from hft_platform.recorder.wal import WALReplayer, WALWriter


def test_wal_write_sync_atomic_creates_file(tmp_path: Path):
    writer = WALWriter(str(tmp_path))
    fname = tmp_path / "hft.market_data_123.jsonl"
    payload = [{"symbol": "TEST", "price": 123, "volume": 1}]

    writer._write_sync_atomic(str(fname), payload)

    assert fname.exists()
    lines = fname.read_text().splitlines()
    assert len(lines) == 1
    assert json.loads(lines[0]) == payload[0]


@pytest.mark.asyncio
async def test_wal_replayer_replays_and_deletes(tmp_path: Path):
    writer = WALWriter(str(tmp_path))
    fname = tmp_path / "hft.market_data_123.jsonl"
    payload = [{"symbol": "TEST", "price": 123, "volume": 1}]
    writer._write_sync(str(fname), payload)

    seen = []

    async def sender(table, data):
        seen.append((table, data))
        return True

    replayer = WALReplayer(str(tmp_path), sender)
    await replayer.replay()

    assert seen == [("hft.market_data", payload)]
    assert not fname.exists()


@pytest.mark.asyncio
async def test_wal_replayer_stops_on_failure(tmp_path: Path):
    writer = WALWriter(str(tmp_path))
    first = tmp_path / "hft.market_data_1.jsonl"
    second = tmp_path / "hft.market_data_2.jsonl"
    writer._write_sync(str(first), [{"symbol": "A"}])
    writer._write_sync(str(second), [{"symbol": "B"}])

    calls = []

    async def sender(table, data):
        calls.append((table, data))
        return False

    replayer = WALReplayer(str(tmp_path), sender)
    await replayer.replay()

    assert calls == [("hft.market_data", [{"symbol": "A"}])]
    assert first.exists()
    assert second.exists()
