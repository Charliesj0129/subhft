import asyncio
import pytest
import os
import shutil
from hft_platform.recorder.worker import RecorderService
from hft_platform.recorder.wal import WALWriter, WALReplayer
from hft_platform.observability.metrics import MetricsRegistry

@pytest.mark.asyncio
async def test_recorder_pipeline():
    wal_dir = ".wal_test"
    if os.path.exists(wal_dir):
        shutil.rmtree(wal_dir)
    
    queue = asyncio.Queue()
    recorder = RecorderService(queue)
    
    # Mock Writer that fails
    class FailingWriter:
        async def write(self, table, data):
            raise Exception("Db Down")
            
    # Instruct batcher to use failing writer AND backup WAL logic (needs injection)
    # Since we didn't inject WAL into Batcher yet, we test components separately for now
    # or improve Batcher injection.
    
    # Updated: Let's test Batcher -> WAL logic explicitly
    # 1. Test WAL Writer
    writer = WALWriter(wal_dir)
    test_data = [{"k": "v"}]
    await writer.write("test_table", test_data)
    
    files = os.listdir(wal_dir)
    assert len(files) == 1
    assert "test_table" in files[0]
    
    # 2. Test Replay
    replayed = []
    async def mock_send(table, data):
        replayed.extend(data)
        return True
        
    replayer = WALReplayer(wal_dir, mock_send)
    await replayer.replay()
    
    assert len(replayed) == 1
    assert replayed[0]["k"] == "v"
    assert len(os.listdir(wal_dir)) == 0 # Should be deleted

    # 3. Cleanup
    if os.path.exists(wal_dir):
        shutil.rmtree(wal_dir)

@pytest.mark.asyncio
async def test_batcher_logic():
    from hft_platform.recorder.batcher import Batcher
    
    flushed = []
    class MockWriter:
        async def write(self, table, data):
            flushed.append(data)

    b = Batcher("t", flush_limit=2, writer=MockWriter())
    await b.add({"a": 1})
    assert len(flushed) == 0 # Not full
    
    await b.add({"a": 2})
    # Size 2 >= limit 2 -> Flush active
    # Note: add calls _flush_locked which awaits writer.write
    assert len(flushed) == 1
    assert len(flushed[0]) == 2
