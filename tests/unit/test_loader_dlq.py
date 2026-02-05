"""Unit tests for WALLoaderService batch insert retry and DLQ behavior."""

import json
import os
import shutil
import tempfile
import unittest
from unittest.mock import MagicMock, patch

from hft_platform.recorder.loader import WALLoaderService


class TestLoaderInsertRetry(unittest.TestCase):
    """Test WALLoaderService insert retry with exponential backoff."""

    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        self.wal_dir = os.path.join(self.temp_dir, "wal")
        self.archive_dir = os.path.join(self.temp_dir, "archive")
        os.makedirs(self.wal_dir)
        os.makedirs(self.archive_dir)

        self.env_patcher = patch.dict(
            "os.environ",
            {
                "HFT_INSERT_MAX_RETRIES": "3",
                "HFT_INSERT_BASE_DELAY_S": "0.01",
                "HFT_INSERT_MAX_BACKOFF_S": "0.1",
            },
        )
        self.env_patcher.start()

    def tearDown(self):
        self.env_patcher.stop()
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_insert_retry_config(self):
        """Test insert retry configuration is loaded."""
        loader = WALLoaderService(wal_dir=self.wal_dir, archive_dir=self.archive_dir)
        self.assertEqual(loader._insert_max_retries, 3)
        self.assertEqual(loader._insert_base_delay_s, 0.01)
        self.assertEqual(loader._insert_max_backoff_s, 0.1)

    def test_insert_batch_success_first_try(self):
        """Test successful insert on first attempt."""
        loader = WALLoaderService(wal_dir=self.wal_dir, archive_dir=self.archive_dir)
        loader.ch_client = MagicMock()

        rows = [
            {"symbol": "2330", "price": 100.0, "volume": 1000, "ts": 1234567890},
        ]

        result = loader.insert_batch("market_data", rows)
        self.assertTrue(result)
        loader.ch_client.insert.assert_called_once()

    @patch("time.sleep")
    def test_insert_batch_retry_on_failure(self, mock_sleep):
        """Test insert retries on transient failure."""
        loader = WALLoaderService(wal_dir=self.wal_dir, archive_dir=self.archive_dir)
        loader.ch_client = MagicMock()
        loader.ch_client.insert.side_effect = [
            ConnectionError("Connection lost"),
            ConnectionError("Connection lost"),
            None,  # Success on third attempt
        ]

        rows = [
            {"symbol": "2330", "price": 100.0, "volume": 1000, "ts": 1234567890},
        ]

        result = loader.insert_batch("market_data", rows)
        self.assertTrue(result)
        self.assertEqual(loader.ch_client.insert.call_count, 3)
        self.assertEqual(mock_sleep.call_count, 2)

    @patch("time.sleep")
    def test_insert_batch_fails_after_max_retries(self, mock_sleep):
        """Test insert fails after max retries exhausted."""
        loader = WALLoaderService(wal_dir=self.wal_dir, archive_dir=self.archive_dir)
        loader.ch_client = MagicMock()
        loader.ch_client.insert.side_effect = ConnectionError("Connection lost")

        rows = [
            {"symbol": "2330", "price": 100.0, "volume": 1000, "ts": 1234567890},
        ]

        result = loader.insert_batch("market_data", rows)
        self.assertFalse(result)
        self.assertEqual(loader.ch_client.insert.call_count, 3)

    def test_insert_backoff_calculation(self):
        """Test backoff delay calculation."""
        loader = WALLoaderService(wal_dir=self.wal_dir, archive_dir=self.archive_dir)

        delay_0 = loader._compute_insert_backoff(0)
        delay_1 = loader._compute_insert_backoff(1)
        delay_2 = loader._compute_insert_backoff(2)

        # Delays should increase (approximately exponential with some jitter)
        self.assertGreater(delay_1, delay_0 * 0.5)  # Allow for jitter
        self.assertGreater(delay_2, delay_1 * 0.5)

    def test_insert_batch_no_client_returns_false(self):
        """Test insert returns False when no client available."""
        loader = WALLoaderService(wal_dir=self.wal_dir, archive_dir=self.archive_dir)
        loader.ch_client = None

        rows = [
            {"symbol": "2330", "price": 100.0, "volume": 1000, "ts": 1234567890},
        ]

        result = loader.insert_batch("market_data", rows)
        self.assertFalse(result)


class TestLoaderDLQ(unittest.TestCase):
    """Test WALLoaderService Dead Letter Queue functionality."""

    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        self.wal_dir = os.path.join(self.temp_dir, "wal")
        self.archive_dir = os.path.join(self.temp_dir, "archive")
        os.makedirs(self.wal_dir)
        os.makedirs(self.archive_dir)

    def tearDown(self):
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_write_to_dlq_creates_file(self):
        """Test _write_to_dlq creates DLQ file."""
        loader = WALLoaderService(wal_dir=self.wal_dir, archive_dir=self.archive_dir)

        rows = [
            {"symbol": "2330", "price": 100.0, "volume": 1000},
            {"symbol": "2317", "price": 50.0, "volume": 500},
        ]

        loader._write_to_dlq("market_data", rows, "test_error")

        # Check DLQ directory was created
        dlq_dir = os.path.join(self.wal_dir, "dlq")
        self.assertTrue(os.path.exists(dlq_dir))

        # Check file was created
        dlq_files = os.listdir(dlq_dir)
        self.assertEqual(len(dlq_files), 1)
        self.assertTrue(dlq_files[0].startswith("market_data_"))
        self.assertTrue(dlq_files[0].endswith(".jsonl"))

    def test_write_to_dlq_content_format(self):
        """Test _write_to_dlq writes correct content format."""
        loader = WALLoaderService(wal_dir=self.wal_dir, archive_dir=self.archive_dir)

        rows = [
            {"symbol": "2330", "price": 100.0},
        ]

        loader._write_to_dlq("market_data", rows, "connection_failed")

        dlq_dir = os.path.join(self.wal_dir, "dlq")
        dlq_files = os.listdir(dlq_dir)
        dlq_path = os.path.join(dlq_dir, dlq_files[0])

        with open(dlq_path, "r") as f:
            lines = f.readlines()

        # First line should be metadata
        meta = json.loads(lines[0])
        self.assertTrue(meta.get("_dlq_meta"))
        self.assertEqual(meta.get("table"), "market_data")
        self.assertEqual(meta.get("error"), "connection_failed")
        self.assertEqual(meta.get("row_count"), 1)

        # Second line should be the row
        row = json.loads(lines[1])
        self.assertEqual(row.get("symbol"), "2330")


class TestLoaderCorruptFileQuarantine(unittest.TestCase):
    """Test WALLoaderService corrupt file quarantine functionality."""

    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        self.wal_dir = os.path.join(self.temp_dir, "wal")
        self.archive_dir = os.path.join(self.temp_dir, "archive")
        os.makedirs(self.wal_dir)
        os.makedirs(self.archive_dir)

    def tearDown(self):
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_quarantine_corrupt_file(self):
        """Test corrupt file is moved to quarantine directory."""
        loader = WALLoaderService(wal_dir=self.wal_dir, archive_dir=self.archive_dir)

        # Create a corrupt file
        corrupt_file = os.path.join(self.wal_dir, "test_corrupt.jsonl")
        with open(corrupt_file, "w") as f:
            f.write("not valid json\n")

        loader._quarantine_corrupt_file(corrupt_file, "test_corrupt.jsonl", "JSON decode error")

        # Check corrupt directory was created
        corrupt_dir = os.path.join(self.wal_dir, "corrupt")
        self.assertTrue(os.path.exists(corrupt_dir))

        # Check file was moved
        self.assertFalse(os.path.exists(corrupt_file))
        quarantined = os.path.join(corrupt_dir, "test_corrupt.jsonl")
        self.assertTrue(os.path.exists(quarantined))

    def test_quarantine_creates_directory(self):
        """Test quarantine creates corrupt directory if not exists."""
        loader = WALLoaderService(wal_dir=self.wal_dir, archive_dir=self.archive_dir)

        corrupt_file = os.path.join(self.wal_dir, "bad_file.jsonl")
        with open(corrupt_file, "w") as f:
            f.write("corrupt\n")

        # Ensure corrupt dir doesn't exist
        corrupt_dir = os.path.join(self.wal_dir, "corrupt")
        self.assertFalse(os.path.exists(corrupt_dir))

        loader._quarantine_corrupt_file(corrupt_file, "bad_file.jsonl", "test")

        self.assertTrue(os.path.exists(corrupt_dir))


if __name__ == "__main__":
    unittest.main()
