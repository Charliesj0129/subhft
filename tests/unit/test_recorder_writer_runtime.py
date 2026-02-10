import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest

from hft_platform.recorder import writer as writer_module
from hft_platform.recorder.writer import DataWriter


@pytest.mark.asyncio
async def test_writer_write_fallback_on_insert_error(tmp_path, monkeypatch):
    writer = DataWriter(wal_dir=str(tmp_path))
    writer.connected = True
    writer.ch_client = MagicMock()
    writer._ts_max_future_ns = 0

    writer._ch_insert = MagicMock(side_effect=RuntimeError("boom"))
    writer.wal.write = AsyncMock()

    async def fake_to_thread(func, *args, **kwargs):
        return func(*args, **kwargs)

    monkeypatch.setattr(writer_module.asyncio, "to_thread", fake_to_thread)

    await writer.write("hft.market_data", [{"exch_ts": 1, "ingest_ts": 1}])

    writer._ch_insert.assert_called_once()
    writer.wal.write.assert_awaited_once()


@pytest.mark.asyncio
async def test_writer_write_success_no_wal(tmp_path, monkeypatch):
    writer = DataWriter(wal_dir=str(tmp_path))
    writer.connected = True
    writer.ch_client = MagicMock()
    writer._ts_max_future_ns = 0

    writer._ch_insert = MagicMock()
    writer.wal.write = AsyncMock()

    async def fake_to_thread(func, *args, **kwargs):
        return func(*args, **kwargs)

    monkeypatch.setattr(writer_module.asyncio, "to_thread", fake_to_thread)

    await writer.write("hft.market_data", [{"exch_ts": 1, "ingest_ts": 1}])

    writer._ch_insert.assert_called_once()
    writer.wal.write.assert_not_awaited()


@pytest.mark.asyncio
async def test_writer_write_empty_is_noop(tmp_path):
    writer = DataWriter(wal_dir=str(tmp_path))
    writer.wal.write = AsyncMock()
    await writer.write("hft.market_data", [])
    writer.wal.write.assert_not_awaited()


@pytest.mark.asyncio
async def test_writer_write_drops_all_rows(tmp_path, monkeypatch):
    writer = DataWriter(wal_dir=str(tmp_path))
    writer.wal.write = AsyncMock()
    writer._ts_max_future_ns = 1
    monkeypatch.setattr(writer_module.timebase, "now_ns", lambda: 0)

    await writer.write(
        "hft.market_data",
        [{"exch_ts": 10_000, "ingest_ts": 10_000}],
    )

    writer.wal.write.assert_not_awaited()


def test_ch_insert_noop_on_empty(tmp_path):
    writer = DataWriter(wal_dir=str(tmp_path))
    writer.ch_client = MagicMock()
    writer._ch_insert("hft.table", [])
    writer.ch_client.insert.assert_not_called()


def test_writer_ch_insert_builds_rows(tmp_path):
    writer = DataWriter(wal_dir=str(tmp_path))
    writer.ch_client = MagicMock()
    data = [{"a": 1, "b": 2}, {"a": 3, "b": 4}]

    writer._ch_insert("hft.table", data)

    args, kwargs = writer.ch_client.insert.call_args
    assert args[0] == "hft.table"
    assert args[1] == [[1, 2], [3, 4]]
    assert kwargs["column_names"] == ["a", "b"]


def test_writer_do_heartbeat_check(tmp_path):
    writer = DataWriter(wal_dir=str(tmp_path))

    assert writer._do_heartbeat_check() is False

    writer.ch_client = MagicMock()
    writer.ch_client.command.side_effect = RuntimeError("boom")
    assert writer._do_heartbeat_check() is False

    writer.ch_client.command.side_effect = None
    writer.ch_client.command.return_value = 1
    assert writer._do_heartbeat_check() is True


def test_init_schema_failure_disables_connection(tmp_path, monkeypatch):
    writer = DataWriter(wal_dir=str(tmp_path))
    writer.connected = True
    writer.ch_client = MagicMock()

    def _boom(_client):
        raise RuntimeError("fail")

    monkeypatch.setattr(writer_module, "apply_schema", _boom)

    writer._init_schema()

    assert writer.connected is False
    assert writer._schema_initialized is False


def test_init_schema_view_repair_failure(tmp_path, monkeypatch):
    writer = DataWriter(wal_dir=str(tmp_path))
    writer.connected = True
    writer.ch_client = MagicMock()

    monkeypatch.setattr(writer_module, "apply_schema", lambda _client: None)
    monkeypatch.setattr(
        writer_module, "ensure_price_scaled_views", lambda _client: (_ for _ in ()).throw(RuntimeError("boom"))
    )

    writer._init_schema()

    assert writer._schema_initialized is True
    assert writer.connected is True
