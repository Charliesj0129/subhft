import os
import shutil

import pytest

from hft_platform.recorder.wal import WALWriter


@pytest.mark.asyncio
async def test_wal_writer_roundtrip():
    wal_dir = ".wal_test"
    if os.path.exists(wal_dir):
        shutil.rmtree(wal_dir)

    writer = WALWriter(wal_dir)
    test_data = [{"k": "v"}]
    await writer.write("test_table", test_data)

    files = os.listdir(wal_dir)
    assert len(files) == 1
    assert "test_table" in files[0]

    if os.path.exists(wal_dir):
        shutil.rmtree(wal_dir)


@pytest.mark.asyncio
async def test_batcher_logic():
    from hft_platform.recorder.batcher import Batcher

    flushed = []

    class MockWriter:
        async def write(self, table, data):
            flushed.append(data)

        async def write_columnar(self, table, cols, data, count):
            # Reconstruct row dicts for backward-compat
            rows = [{cols[j]: data[j][i] for j in range(len(cols))} for i in range(count)]
            flushed.append(rows)

    b = Batcher("t", flush_limit=2, writer=MockWriter())
    await b.add({"a": 1})
    assert len(flushed) == 0  # Not full

    await b.add({"a": 2})
    # Size 2 >= limit 2 -> Flush active
    # Note: add calls _flush_locked which awaits writer.write
    assert len(flushed) == 1
    assert len(flushed[0]) == 2
