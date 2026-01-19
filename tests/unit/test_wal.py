from pathlib import Path

import pytest

from hft_platform.recorder.wal import WALReplayer, WALWriter


@pytest.mark.asyncio
async def test_wal_writer_and_replayer(tmp_path):
    writer = WALWriter(str(tmp_path))
    await writer.write("orders", [{"order_id": "O1"}])

    files = list(Path(tmp_path).glob("orders_*.jsonl"))
    assert files, "WAL file should be written"

    seen = {}

    async def _sender(table, data):
        seen["table"] = table
        seen["rows"] = data
        return True

    replayer = WALReplayer(str(tmp_path), _sender)
    await replayer.replay()

    assert seen["table"] == "orders"
    assert seen["rows"][0]["order_id"] == "O1"
    assert not list(Path(tmp_path).glob("orders_*.jsonl"))
