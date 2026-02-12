import json
import os
import shutil
import tempfile
import unittest
from unittest.mock import MagicMock, mock_open, patch

from hft_platform.recorder.loader import WALLoaderService
from hft_platform.recorder.writer import DataWriter


class TestDataWriter(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp_dir = tempfile.mkdtemp()
        self.patcher = patch("hft_platform.recorder.writer.clickhouse_connect")
        self.mock_ch = self.patcher.start()
        # Ensure clickhouse is enabled by env or default behavior checks
        # Based on writer.py, it checks HFT_CLICKHOUSE_ENABLED
        self.env_patcher = patch.dict(os.environ, {"HFT_CLICKHOUSE_ENABLED": "1"})
        self.env_patcher.start()

    def tearDown(self):
        self.patcher.stop()
        self.env_patcher.stop()
        shutil.rmtree(self.tmp_dir)

    def test_connect_success(self):
        writer = DataWriter(wal_dir=self.tmp_dir)
        # Mock client
        mock_client = MagicMock()
        self.mock_ch.get_client.return_value = mock_client

        with (
            patch("os.path.exists", return_value=True),
            patch("builtins.open", mock_open(read_data="CREATE TABLE foo;")),
        ):
            writer.connect()

        self.assertTrue(writer.connected)
        self.mock_ch.get_client.assert_called_once()
        mock_client.command.assert_called_with("CREATE TABLE foo")

    async def test_write_clickhouse_success(self):
        writer = DataWriter(wal_dir=self.tmp_dir)
        writer.ch_client = MagicMock()
        writer.connected = True

        data = [{"col1": 1, "col2": "a"}, {"col1": 2, "col2": "b"}]
        await writer.write("test_table", data)

        # Verify insert called
        writer.ch_client.insert.assert_called()
        args = writer.ch_client.insert.call_args
        self.assertEqual(args[0][0], "test_table")
        # values should be list of lists
        self.assertEqual(args[0][1], [[1, "a"], [2, "b"]])
        self.assertEqual(args[1]["column_names"], ["col1", "col2"])

    async def test_write_fallback_wal(self):
        writer = DataWriter(wal_dir=self.tmp_dir)
        writer.ch_client = MagicMock()
        writer.connected = True
        # Disable WAL batch writer so individual WAL files are created
        writer._wal_batch_enabled = False
        # Simulate CH error
        writer.ch_client.insert.side_effect = Exception("CH Down")

        data = [{"col1": 1}]
        await writer.write("test_table", data)

        # Should have written to WAL
        wal_files = [f for f in os.listdir(self.tmp_dir) if f.endswith(".jsonl")]
        self.assertTrue(len(wal_files) > 0)
        self.assertTrue(wal_files[0].startswith("test_table"))


import time


class TestWALLoader(unittest.TestCase):
    def setUp(self):
        self.wal_dir = tempfile.mkdtemp()
        self.archive_dir = tempfile.mkdtemp()
        self.loader = WALLoaderService(wal_dir=self.wal_dir, archive_dir=self.archive_dir)
        self.loader.ch_client = MagicMock()  # Mock connected client

    def tearDown(self):
        shutil.rmtree(self.wal_dir)
        shutil.rmtree(self.archive_dir)

    def test_process_files_market_data(self):
        # Create a dummy WAL file
        fname = "market_data_12345.jsonl"
        fpath = os.path.join(self.wal_dir, fname)
        row = {"symbol": "2330", "price": 100, "volume": 5, "exch_ts": 1000}
        with open(fpath, "w") as f:
            f.write(json.dumps(row) + "\n")

        # Force mtime to be old enough
        # We need to ensure logic in loader sees it as old.
        # Loader: if now - mtime < 2.0: continue
        # So we set mtime to now - 10
        old_time = time.time() - 10
        os.utime(fpath, (old_time, old_time))

        self.loader.process_files()

        # Check insert called
        self.loader.ch_client.insert.assert_called()
        call_args = self.loader.ch_client.insert.call_args
        self.assertEqual(call_args[0][0], "hft.market_data")  # mapped table
        # Check archive
        self.assertFalse(os.path.exists(fpath))
        self.assertTrue(os.path.exists(os.path.join(self.archive_dir, fname)))

    def test_unknown_table(self):
        fname = "unknown_123.jsonl"
        fpath = os.path.join(self.wal_dir, fname)
        with open(fpath, "w") as f:
            f.write("{}\n")
        old_time = time.time() - 10
        os.utime(fpath, (old_time, old_time))

        self.loader.process_files()
        # Should skip/log warning, file remains?
        # Logic says: if unknown -> continue (skip). So file stays in WAL dir?
        # Or ideally it should be moved to error?
        # For current impl, it just logs warning and continues loop.
        self.assertTrue(os.path.exists(fpath))
