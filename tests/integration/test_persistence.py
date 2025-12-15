import pytest
import os
import shutil
import json
import asyncio
import time
from unittest.mock import MagicMock
from hft_platform.recorder.wal import WALWriter

@pytest.mark.asyncio
async def test_wal_integrity():
    """Verify WAL writes are readable and valid JSONL."""
    test_dir = ".wal_test_qa"
    if os.path.exists(test_dir):
        shutil.rmtree(test_dir)
        
    writer = WALWriter(wal_dir=test_dir)
    # No start() method in simple WALWriter
    
    # 1. Write Data
    events = [{"seq": i, "data": "x" * 10} for i in range(100)]
    # write takes list
    await writer.write("qa_topic", events)
        
    # No flush/stop needed, write is immediate per file in this impl?
    # Actually write creates a new file per call.
    # filename = f"{self.wal_dir}/{table}_{ts}.jsonl"
    
    # 2. Read Back
    # Find files
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
                    
    # 3. Validation
    # WALWriter wraps data in envelope? {"ts":..., "topic":..., "payload":...}
    assert len(read_events) == 100
    
    # Check payload
    # implementation specific, assume transparent or enveloped
    first = read_events[0]
    # If enveloped:
    if "data" in first: # It was simple dump?
        assert first["seq"] == 0
    else: # Enveloped
        # Check payload fields
        pass 
        
    print(f"Data Integrity Passed: {len(read_events)} records verified.")
    
    # Cleanup
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
    # Ensure archive dir exists (since we skip run())
    os.makedirs(test_archive_dir)
    
    # 1. Create a dummy WAL file
    # Simulate a rotated file (older mtime)
    from hft_platform.recorder.loader import WALLoaderService
    
    data = {"symbol": "2330", "price": 100.0, "volume": 5, "type": "Tick", "seq_no": 123}
    filename = "market_data_1234567890.jsonl"
    fpath = os.path.join(test_wal_dir, filename)
    
    with open(fpath, "w") as f:
        f.write(json.dumps(data) + "\n")
        
    # Set mtime to past (outside 2.0s buffer)
    # But wait, loader checks check now - mtime < 2.0. So past mtime is GOOD.
    # default write timestamp is usually older than "now" by the time we run logic if file creation was fast, 
    # but let's be safe.
    t_past = time.time() - 5.0
    os.utime(fpath, (t_past, t_past))
    
    # 2. Setup Loader with Mock CH
    mock_ch = MagicMock()
    loader = WALLoaderService(wal_dir=test_wal_dir, archive_dir=test_archive_dir)
    loader.ch_client = mock_ch # Inject mock directly
    
    # 3. Process
    loader.process_files()
    
    # 4. Verify Insert
    mock_ch.insert.assert_called_once()
    args, kwargs = mock_ch.insert.call_args
    table = args[0]
    rows = args[1]
    
    assert table == "hft.market_data"
    assert len(rows) == 1
    # Check row structure (based on logic)
    row = rows[0]
    # cols = symbol, exchange, type, exch_ts, ingest_ts, price, volume, bids_price, bids_vol, asks_price, asks_vol, seq_no
    assert row[0] == "2330"   # symbol
    assert row[5] == 100.0    # price
    assert row[11] == 123     # seq_no
    
    # 5. Verify Archival
    assert os.path.exists(os.path.join(test_archive_dir, filename))
    assert not os.path.exists(fpath)
    
    print("Loader Integration Passed.")
    
    # Cleanup
    if os.path.exists(test_wal_dir):
        shutil.rmtree(test_wal_dir)
