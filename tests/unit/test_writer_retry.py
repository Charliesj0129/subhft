"""Unit tests for DataWriter connection retry and exponential backoff."""

import unittest
from unittest.mock import MagicMock, patch

from hft_platform.recorder.writer import DataWriter


class TestDataWriterRetry(unittest.TestCase):
    """Test DataWriter connection retry with exponential backoff."""

    def setUp(self):
        # Disable ClickHouse by default
        self.env_patcher = patch.dict(
            "os.environ",
            {
                "HFT_CLICKHOUSE_ENABLED": "0",
                "HFT_CH_MAX_RETRIES": "3",
                "HFT_CH_BASE_DELAY_S": "0.1",
                "HFT_CH_MAX_BACKOFF_S": "1.0",
                "HFT_CH_JITTER_FACTOR": "0.1",
            },
        )
        self.env_patcher.start()

    def tearDown(self):
        self.env_patcher.stop()

    def test_default_backoff_config(self):
        """Test default exponential backoff configuration."""
        writer = DataWriter()
        self.assertEqual(writer._max_retries, 3)
        self.assertEqual(writer._base_delay_s, 0.1)
        self.assertEqual(writer._max_backoff_s, 1.0)
        self.assertEqual(writer._jitter_factor, 0.1)

    def test_compute_backoff_delay_exponential(self):
        """Test backoff delay increases exponentially."""
        writer = DataWriter()
        writer._jitter_factor = 0.0  # Disable jitter for predictable testing

        delay_0 = writer._compute_backoff_delay(0)
        delay_1 = writer._compute_backoff_delay(1)
        delay_2 = writer._compute_backoff_delay(2)

        # With base_delay=0.1, expect: 0.1, 0.2, 0.4
        self.assertAlmostEqual(delay_0, 0.1, places=2)
        self.assertAlmostEqual(delay_1, 0.2, places=2)
        self.assertAlmostEqual(delay_2, 0.4, places=2)

    def test_compute_backoff_delay_max_cap(self):
        """Test backoff delay is capped at max_backoff_s."""
        writer = DataWriter()
        writer._jitter_factor = 0.0
        writer._max_backoff_s = 0.5

        delay_10 = writer._compute_backoff_delay(10)
        self.assertLessEqual(delay_10, 0.5)

    def test_compute_backoff_delay_with_jitter(self):
        """Test backoff delay includes jitter."""
        writer = DataWriter()
        writer._jitter_factor = 0.5

        delays = [writer._compute_backoff_delay(0) for _ in range(10)]
        # With jitter, delays should vary
        self.assertGreater(max(delays) - min(delays), 0.01)

    def test_compute_backoff_delay_minimum(self):
        """Test backoff delay has minimum of 0.1s."""
        writer = DataWriter()
        writer._base_delay_s = 0.01
        writer._jitter_factor = 0.0

        delay = writer._compute_backoff_delay(0)
        self.assertGreaterEqual(delay, 0.1)

    @patch("hft_platform.recorder.writer.clickhouse_connect")
    def test_connect_success_first_attempt(self, mock_ch):
        """Test successful connection on first attempt."""
        mock_client = MagicMock()
        mock_ch.get_client.return_value = mock_client

        with patch.dict("os.environ", {"HFT_CLICKHOUSE_ENABLED": "1"}):
            writer = DataWriter()
            with patch.object(writer, "_start_heartbeat_thread"):
                writer.connect()

        self.assertTrue(writer.connected)
        self.assertEqual(writer._connect_attempts, 0)
        mock_ch.get_client.assert_called_once()

    @patch("hft_platform.recorder.writer.clickhouse_connect")
    @patch("hft_platform.recorder.writer.time.sleep")
    def test_connect_retry_on_failure(self, mock_sleep, mock_ch):
        """Test connection retry with backoff on failure."""
        mock_ch.get_client.side_effect = [
            ConnectionError("Connection refused"),
            ConnectionError("Connection refused"),
            MagicMock(),  # Success on third attempt
        ]

        with patch.dict(
            "os.environ",
            {
                "HFT_CLICKHOUSE_ENABLED": "1",
                "HFT_CH_MAX_RETRIES": "3",
                "HFT_CH_BASE_DELAY_S": "0.1",
            },
        ):
            writer = DataWriter()
            with patch.object(writer, "_start_heartbeat_thread"):
                writer.connect()
            # Stop heartbeat thread to prevent additional sleeps
            writer._heartbeat_running = False

        self.assertTrue(writer.connected)
        self.assertEqual(mock_ch.get_client.call_count, 3)
        self.assertEqual(mock_sleep.call_count, 2)

    @patch("hft_platform.recorder.writer.clickhouse_connect")
    @patch("hft_platform.recorder.writer.time.sleep")
    def test_connect_fails_after_max_retries(self, mock_sleep, mock_ch):
        """Test connection fails after max retries exhausted."""
        mock_ch.get_client.side_effect = ConnectionError("Connection refused")

        with patch.dict(
            "os.environ",
            {
                "HFT_CLICKHOUSE_ENABLED": "1",
                "HFT_CH_MAX_RETRIES": "3",
            },
        ):
            writer = DataWriter()
            with patch.object(writer, "_start_heartbeat_thread"):
                writer.connect()

        self.assertFalse(writer.connected)
        self.assertEqual(mock_ch.get_client.call_count, 3)

    def test_wal_only_mode_when_disabled(self):
        """Test WAL-only mode when ClickHouse is disabled."""
        with patch.dict("os.environ", {"HFT_CLICKHOUSE_ENABLED": "0"}):
            writer = DataWriter()
            writer.connect()

        self.assertFalse(writer.connected)
        self.assertIsNone(writer.ch_client)


class TestDataWriterBackoffFormula(unittest.TestCase):
    """Test exponential backoff formula edge cases."""

    def test_backoff_formula_with_zero_attempt(self):
        """Backoff at attempt 0 should be base_delay."""
        writer = DataWriter()
        writer._base_delay_s = 1.0
        writer._jitter_factor = 0.0

        delay = writer._compute_backoff_delay(0)
        self.assertAlmostEqual(delay, 1.0, places=2)

    def test_backoff_formula_with_large_attempt(self):
        """Backoff should be capped regardless of attempt number."""
        writer = DataWriter()
        writer._base_delay_s = 1.0
        writer._max_backoff_s = 30.0
        writer._jitter_factor = 0.0

        delay = writer._compute_backoff_delay(100)
        self.assertLessEqual(delay, 30.0)


if __name__ == "__main__":
    unittest.main()
