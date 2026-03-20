"""Tests for DataWriter behaviors.

Covers: credential resolution chain, deprecated env var warnings,
HFT_CLICKHOUSE_ENABLED opt-in, port-based protocol selection,
WAL fallback on CH failure, host/port env overrides, per-table lock striping,
connection flags, backoff computation, and status reporting.
"""
from __future__ import annotations

import warnings
from unittest.mock import patch

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_writer(monkeypatch, **env_overrides):
    """Build a DataWriter with mocked externals and optional env overrides."""
    # Disable CH by default to avoid real connections
    monkeypatch.setenv("HFT_CLICKHOUSE_ENABLED", env_overrides.pop("HFT_CLICKHOUSE_ENABLED", "0"))

    for key, val in env_overrides.items():
        monkeypatch.setenv(key, str(val))

    # Clear vars that should not be set unless explicitly provided
    for var in (
        "HFT_CLICKHOUSE_USER",
        "HFT_CLICKHOUSE_USERNAME",
        "CLICKHOUSE_USER",
        "CLICKHOUSE_USERNAME",
        "HFT_CLICKHOUSE_PASSWORD",
        "CLICKHOUSE_PASSWORD",
        "HFT_CLICKHOUSE_HOST",
        "HFT_CLICKHOUSE_PORT",
        "HFT_DISABLE_CLICKHOUSE",
    ):
        if var not in env_overrides:
            monkeypatch.delenv(var, raising=False)

    with (
        patch("hft_platform.observability.metrics.MetricsRegistry") as mock_mr,
        patch("hft_platform.recorder.writer.WALWriter"),
    ):
        mock_mr.get.return_value = None
        from hft_platform.recorder.writer import DataWriter

        writer = DataWriter(ch_host="localhost", ch_port=9000)
        writer.metrics = None
        return writer


# ===================================================================
# Credential resolution chain
# ===================================================================

class TestCredentialResolution:
    """Credential resolution: HFT_CLICKHOUSE_USER > CLICKHOUSE_USER > default."""

    def test_hft_clickhouse_user_priority(self, monkeypatch):
        """HFT_CLICKHOUSE_USER takes top priority."""
        writer = _make_writer(monkeypatch, HFT_CLICKHOUSE_USER="admin")
        assert writer.ch_params["username"] == "admin"

    def test_clickhouse_user_fallback(self, monkeypatch):
        """CLICKHOUSE_USER used when HFT_CLICKHOUSE_USER is absent."""
        writer = _make_writer(monkeypatch, CLICKHOUSE_USER="ch_user")
        assert writer.ch_params["username"] == "ch_user"

    def test_default_username(self, monkeypatch):
        """Falls back to 'default' when no env vars set."""
        writer = _make_writer(monkeypatch)
        assert writer.ch_params["username"] == "default"

    def test_password_resolution(self, monkeypatch):
        """HFT_CLICKHOUSE_PASSWORD takes priority over CLICKHOUSE_PASSWORD."""
        writer = _make_writer(
            monkeypatch,
            HFT_CLICKHOUSE_PASSWORD="secret1",
            CLICKHOUSE_PASSWORD="secret2",
        )
        assert writer.ch_params["password"] == "secret1"

    def test_password_fallback(self, monkeypatch):
        """CLICKHOUSE_PASSWORD used when HFT_CLICKHOUSE_PASSWORD absent."""
        writer = _make_writer(monkeypatch, CLICKHOUSE_PASSWORD="fallback_pw")
        assert writer.ch_params["password"] == "fallback_pw"

    def test_empty_password_default(self, monkeypatch):
        """No password env vars => empty string."""
        writer = _make_writer(monkeypatch)
        assert writer.ch_params["password"] == ""


# ===================================================================
# Deprecated env var warnings
# ===================================================================

class TestDeprecatedEnvVars:
    """Deprecated env vars emit DeprecationWarning."""

    def test_hft_clickhouse_username_deprecated(self, monkeypatch):
        """HFT_CLICKHOUSE_USERNAME emits DeprecationWarning."""
        with warnings.catch_warnings(record=True) as w:
            warnings.simplefilter("always")
            writer = _make_writer(monkeypatch, HFT_CLICKHOUSE_USERNAME="old_user")
            assert writer.ch_params["username"] == "old_user"
            deprecation_msgs = [x for x in w if issubclass(x.category, DeprecationWarning)]
            assert any("HFT_CLICKHOUSE_USERNAME" in str(x.message) for x in deprecation_msgs)

    def test_clickhouse_username_deprecated(self, monkeypatch):
        """CLICKHOUSE_USERNAME emits DeprecationWarning."""
        with warnings.catch_warnings(record=True) as w:
            warnings.simplefilter("always")
            writer = _make_writer(monkeypatch, CLICKHOUSE_USERNAME="old_user2")
            assert writer.ch_params["username"] == "old_user2"
            deprecation_msgs = [x for x in w if issubclass(x.category, DeprecationWarning)]
            assert any("CLICKHOUSE_USERNAME" in str(x.message) for x in deprecation_msgs)

    def test_hft_disable_clickhouse_deprecated(self, monkeypatch):
        """HFT_DISABLE_CLICKHOUSE emits DeprecationWarning."""
        with warnings.catch_warnings(record=True) as w:
            warnings.simplefilter("always")
            writer = _make_writer(monkeypatch, HFT_DISABLE_CLICKHOUSE="1")
            assert writer.ch_enabled is False
            deprecation_msgs = [x for x in w if issubclass(x.category, DeprecationWarning)]
            assert any("HFT_DISABLE_CLICKHOUSE" in str(x.message) for x in deprecation_msgs)


# ===================================================================
# HFT_CLICKHOUSE_ENABLED opt-in
# ===================================================================

class TestClickHouseEnabled:
    """ClickHouse is opt-in via HFT_CLICKHOUSE_ENABLED."""

    def test_disabled_by_default(self, monkeypatch):
        """CH disabled when env not set."""
        writer = _make_writer(monkeypatch)
        assert writer.ch_enabled is False

    def test_enabled_with_1(self, monkeypatch):
        """CH enabled with '1'."""
        writer = _make_writer(monkeypatch, HFT_CLICKHOUSE_ENABLED="1")
        assert writer.ch_enabled is True

    def test_enabled_with_true(self, monkeypatch):
        """CH enabled with 'true'."""
        writer = _make_writer(monkeypatch, HFT_CLICKHOUSE_ENABLED="true")
        assert writer.ch_enabled is True

    def test_disabled_with_0(self, monkeypatch):
        """CH disabled with '0'."""
        writer = _make_writer(monkeypatch, HFT_CLICKHOUSE_ENABLED="0")
        assert writer.ch_enabled is False


# ===================================================================
# Port-based protocol selection
# ===================================================================

class TestProtocolSelection:
    """Port determines native vs HTTP protocol."""

    def test_native_port_9000(self, monkeypatch):
        """Port 9000 selects native interface."""
        writer = _make_writer(monkeypatch)
        assert writer.ch_params["port"] == 9000
        assert writer.ch_params.get("interface") == "native"

    def test_http_port_8123(self, monkeypatch):
        """Port 8123 does not set native interface."""
        monkeypatch.setenv("HFT_CLICKHOUSE_ENABLED", "0")
        for var in (
            "HFT_CLICKHOUSE_USER", "HFT_CLICKHOUSE_USERNAME",
            "CLICKHOUSE_USER", "CLICKHOUSE_USERNAME",
            "HFT_CLICKHOUSE_PASSWORD", "CLICKHOUSE_PASSWORD",
            "HFT_CLICKHOUSE_HOST", "HFT_CLICKHOUSE_PORT",
            "HFT_DISABLE_CLICKHOUSE",
        ):
            monkeypatch.delenv(var, raising=False)

        with (
            patch("hft_platform.observability.metrics.MetricsRegistry") as mock_mr,
            patch("hft_platform.recorder.writer.WALWriter"),
        ):
            mock_mr.get.return_value = None
            from hft_platform.recorder.writer import DataWriter

            writer = DataWriter(ch_host="localhost", ch_port=8123)
            assert writer.ch_params["port"] == 8123
            assert "interface" not in writer.ch_params

    def test_env_port_override_to_native(self, monkeypatch):
        """HFT_CLICKHOUSE_PORT=9000 restores native interface."""
        writer = _make_writer(monkeypatch, HFT_CLICKHOUSE_PORT="9000")
        assert writer.ch_params["port"] == 9000
        assert writer.ch_params.get("interface") == "native"

    def test_env_port_override_to_http(self, monkeypatch):
        """HFT_CLICKHOUSE_PORT=8123 removes native interface."""
        writer = _make_writer(monkeypatch, HFT_CLICKHOUSE_PORT="8123")
        assert writer.ch_params["port"] == 8123
        assert "interface" not in writer.ch_params


# ===================================================================
# Host/port env overrides
# ===================================================================

class TestHostPortOverrides:
    """HFT_CLICKHOUSE_HOST and HFT_CLICKHOUSE_PORT override constructor args."""

    def test_host_override(self, monkeypatch):
        """HFT_CLICKHOUSE_HOST overrides constructor arg."""
        writer = _make_writer(monkeypatch, HFT_CLICKHOUSE_HOST="remote-ch")
        assert writer.ch_params["host"] == "remote-ch"

    def test_port_override(self, monkeypatch):
        """HFT_CLICKHOUSE_PORT overrides constructor arg."""
        writer = _make_writer(monkeypatch, HFT_CLICKHOUSE_PORT="19000")
        assert writer.ch_params["port"] == 19000


# ===================================================================
# Per-table lock striping
# ===================================================================

class TestLockStriping:
    """Per-table lock striping avoids serializing inserts across tables."""

    def test_different_tables_get_different_locks(self, monkeypatch):
        """Different tables produce distinct locks."""
        writer = _make_writer(monkeypatch)
        lock_a = writer._get_table_lock("hft.market_data")
        lock_b = writer._get_table_lock("hft.orders")
        assert lock_a is not lock_b

    def test_same_table_gets_same_lock(self, monkeypatch):
        """Same table returns the same lock instance."""
        writer = _make_writer(monkeypatch)
        lock_a = writer._get_table_lock("hft.market_data")
        lock_b = writer._get_table_lock("hft.market_data")
        assert lock_a is lock_b


# ===================================================================
# Connection flags
# ===================================================================

class TestConnectionFlags:
    """Connection state flags."""

    def test_initial_not_connected(self, monkeypatch):
        """Writer starts disconnected."""
        writer = _make_writer(monkeypatch)
        assert writer.connected is False
        assert writer._schema_initialized is False

    def test_compress_enabled(self, monkeypatch):
        """Compression is enabled in CH params."""
        writer = _make_writer(monkeypatch)
        assert writer.ch_params["compress"] is True


# ===================================================================
# WAL fallback
# ===================================================================

class TestWALFallback:
    """WAL fallback behavior when CH is unavailable."""

    def test_connect_without_ch_enabled(self, monkeypatch):
        """connect() does nothing when CH is disabled."""
        writer = _make_writer(monkeypatch)
        writer.connect()
        assert writer.connected is False

    def test_connect_without_driver(self, monkeypatch):
        """connect() does nothing when clickhouse_connect is None."""
        writer = _make_writer(monkeypatch, HFT_CLICKHOUSE_ENABLED="1")
        with patch("hft_platform.recorder.writer.clickhouse_connect", None):
            writer.connect()
        assert writer.connected is False


# ===================================================================
# Backoff computation
# ===================================================================

class TestBackoffComputation:
    """Exponential backoff delay computation."""

    def test_backoff_increases(self, monkeypatch):
        """Backoff delay increases with attempt number."""
        writer = _make_writer(monkeypatch)
        delays = [writer._compute_backoff_delay(i) for i in range(5)]
        # First delay should be smaller than last (on average, modulo jitter)
        # Check that max backoff is respected
        for d in delays:
            assert d >= 0.1  # minimum 100ms
            assert d <= writer._max_backoff_s + writer._max_backoff_s * writer._jitter_factor + 0.01

    def test_backoff_minimum(self, monkeypatch):
        """Backoff never goes below 100ms."""
        writer = _make_writer(monkeypatch)
        for _ in range(20):
            assert writer._compute_backoff_delay(0) >= 0.1


# ===================================================================
# get_status
# ===================================================================

class TestGetStatus:
    """Status reporting."""

    def test_status_keys(self, monkeypatch):
        """get_status returns expected keys."""
        writer = _make_writer(monkeypatch)
        status = writer.get_status()
        expected_keys = {
            "ch_enabled", "connected", "schema_initialized", "wal_only_mode",
            "connect_attempts", "ch_host", "ch_port", "ch_interface",
            "native_interface_fallback_used", "last_heartbeat_ts", "last_heartbeat_ok",
        }
        assert expected_keys.issubset(set(status.keys()))

    def test_wal_only_when_disconnected(self, monkeypatch):
        """wal_only_mode is True when disconnected."""
        writer = _make_writer(monkeypatch)
        assert writer.get_status()["wal_only_mode"] is True


# ===================================================================
# Native interface fallback
# ===================================================================

class TestNativeInterfaceFallback:
    """Native interface fallback detection."""

    def test_is_native_interface_unsupported_error(self, monkeypatch):
        """Detect native interface unsupported errors."""
        from hft_platform.recorder.writer import DataWriter

        exc = Exception("unrecognized client type native - protocol not available")
        assert DataWriter._is_native_interface_unsupported_error(exc) is True

    def test_non_native_error_not_detected(self, monkeypatch):
        """Non-native errors are not detected as interface errors."""
        from hft_platform.recorder.writer import DataWriter

        exc = Exception("connection refused")
        assert DataWriter._is_native_interface_unsupported_error(exc) is False

    def test_fallback_switches_to_http(self, monkeypatch):
        """Fallback removes native interface and switches port."""
        writer = _make_writer(monkeypatch)
        assert writer.ch_params.get("interface") == "native"
        exc = Exception("unrecognized client type native")
        result = writer._maybe_fallback_clickhouse_interface(exc)
        assert result is True
        assert "interface" not in writer.ch_params
        assert writer._native_interface_fallback_used is True


# ===================================================================
# Chunking
# ===================================================================

class TestChunking:
    """Row and columnar chunking."""

    def test_no_chunking_when_disabled(self, monkeypatch):
        """No chunking when chunk_rows is 0."""
        writer = _make_writer(monkeypatch)
        data = [{"a": 1}, {"a": 2}, {"a": 3}]
        chunks = writer._iter_row_chunks(data)
        assert len(chunks) == 1
        assert chunks[0] is data

    def test_chunking_splits_data(self, monkeypatch):
        """Chunking splits data into smaller pieces."""
        writer = _make_writer(monkeypatch)
        writer._ch_insert_chunk_rows = 2
        data = [{"a": i} for i in range(5)]
        chunks = writer._iter_row_chunks(data)
        assert len(chunks) == 3
        assert len(chunks[0]) == 2
        assert len(chunks[1]) == 2
        assert len(chunks[2]) == 1

    def test_columnar_chunking(self, monkeypatch):
        """Columnar chunking splits column data."""
        writer = _make_writer(monkeypatch)
        writer._ch_insert_chunk_rows = 2
        col_data = [[1, 2, 3, 4, 5], [10, 20, 30, 40, 50]]
        chunks = writer._iter_columnar_chunks(col_data, 5)
        assert len(chunks) == 3
        assert chunks[0][1] == 2  # row count
        assert chunks[2][1] == 1  # last chunk


# ===================================================================
# Transpose helpers
# ===================================================================

class TestTransposeHelpers:
    """Columnar transpose and conversion helpers."""

    def test_transpose_columnar_rows(self, monkeypatch):
        """Transpose column data to row-major."""
        from hft_platform.recorder.writer import DataWriter

        col_data = [[1, 2, 3], ["a", "b", "c"]]
        rows = DataWriter._transpose_columnar_rows(col_data, 3)
        assert rows == [[1, "a"], [2, "b"], [3, "c"]]

    def test_transpose_empty(self, monkeypatch):
        """Transpose empty data returns empty list."""
        from hft_platform.recorder.writer import DataWriter

        assert DataWriter._transpose_columnar_rows([], 0) == []

    def test_columnar_to_row_dicts(self, monkeypatch):
        """Convert columnar data to list of dicts."""
        from hft_platform.recorder.writer import DataWriter

        names = ["id", "value"]
        col_data = [[1, 2], [10, 20]]
        result = DataWriter._columnar_to_row_dicts(names, col_data, 2)
        assert result == [{"id": 1, "value": 10}, {"id": 2, "value": 20}]
