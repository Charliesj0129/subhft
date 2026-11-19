import asyncio
import json
import os
import shutil
import time
from unittest.mock import MagicMock, patch

import pytest

from hft_platform.recorder.wal import WALWriter


@pytest.mark.asyncio
async def test_wal_integrity():
    """Verify WAL writes are readable and valid JSONL."""
    test_dir = ".wal_test_qa"
    if os.path.exists(test_dir):
        shutil.rmtree(test_dir)

    writer = WALWriter(wal_dir=test_dir)

    events = [{"seq": i, "data": "x" * 10} for i in range(100)]
    await writer.write("qa_topic", events)

    files = []
    for root, _, fs in os.walk(test_dir):
        for f in fs:
            if f.endswith(".jsonl"):
                files.append(os.path.join(root, f))

    assert len(files) > 0, "No WAL files created"

    read_events = []
    for fpath in files:
        with open(fpath, "r") as f:
            for line in f:
                if line.strip():
                    read_events.append(json.loads(line))

    assert len(read_events) == 100

    first = read_events[0]
    if "data" in first:
        assert first["seq"] == 0
    else:
        pass

    print(f"Data Integrity Passed: {len(read_events)} records verified.")

    shutil.rmtree(test_dir)


if __name__ == "__main__":
    asyncio.run(test_wal_integrity())


@pytest.mark.asyncio
async def test_loader_integration():
    """Verify WALLoaderService processes files and inserts into ClickHouse (Mocked)."""
    test_wal_dir = ".wal_test_loader"
    test_archive_dir = ".wal_test_loader/archive"
    if os.path.exists(test_wal_dir):
        shutil.rmtree(test_wal_dir)
    os.makedirs(test_wal_dir)
    os.makedirs(test_archive_dir)

    from hft_platform.recorder.loader import WALLoaderService

    data = {"symbol": "2330", "price": 100.0, "volume": 5, "type": "Tick", "seq_no": 123}
    filename = "market_data_1234567890.jsonl"
    fpath = os.path.join(test_wal_dir, filename)

    with open(fpath, "w") as f:
        f.write(json.dumps(data) + "\n")

    t_past = time.time() - 5.0
    os.utime(fpath, (t_past, t_past))

    mock_ch = MagicMock()
    with patch.dict(os.environ, {"HFT_WAL_DEDUP_ENABLED": "0"}):
        loader = WALLoaderService(wal_dir=test_wal_dir, archive_dir=test_archive_dir)
    loader.ch_client = mock_ch

    loader.process_files()

    mock_ch.insert.assert_called_once()
    args, kwargs = mock_ch.insert.call_args
    table = args[0]
    rows = args[1]

    assert table == "hft.market_data"
    assert len(rows) == 1
    row = rows[0]
    assert row[0] == "2330"
    assert row[5] == 100_000_000
    assert row[11] == 123

    assert os.path.exists(os.path.join(test_archive_dir, filename))
    assert not os.path.exists(fpath)

    print("Loader Integration Passed.")

    if os.path.exists(test_wal_dir):
        shutil.rmtree(test_wal_dir)
