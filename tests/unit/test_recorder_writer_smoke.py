"""Smoke tests for recorder/writer.py."""

from unittest.mock import patch

import pytest

from hft_platform.recorder.writer import DataWriter


@pytest.fixture()
def w(tmp_path):
    with patch.dict("os.environ", {"HFT_CLICKHOUSE_ENABLED": "0"}, clear=False):
        return DataWriter(wal_dir=str(tmp_path))


def test_defaults(w):
    assert not w.connected and not w.ch_enabled


def test_backoff(w):
    assert w._compute_backoff_delay(0) >= 0.1


def test_lock_reuse(w):
    assert w._get_table_lock("t") is w._get_table_lock("t")


def test_connect_wal(w):
    w.connect()
    assert not w.connected


@pytest.mark.asyncio
async def test_write_empty(w):
    await w.write("t", [])


def test_chunks(w):
    w._ch_insert_chunk_rows = 0
    assert len(w._iter_row_chunks([1, 2, 3])) == 1


def test_native_unsupported():
    assert DataWriter._is_native_interface_unsupported_error(Exception("unrecognized client type native"))
