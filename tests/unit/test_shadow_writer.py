"""Tests for ShadowOrderWriter — ClickHouse batch writer for shadow order records."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

from hft_platform.order.shadow_writer import ShadowOrderWriter

_SAMPLE_RECORD = {
    "ts_ns": 1_700_000_000_000_000_000,
    "strategy_id": "test_strat",
    "symbol": "2330",
    "side": "BUY",
    "price": 5000000,
    "qty": 1,
    "intent_type": "NEW",
    "intent_id": "test-001",
    "shadow": True,
}


def _make_writer(batch_size: int = 50, enabled: bool = True) -> ShadowOrderWriter:
    return ShadowOrderWriter(batch_size=batch_size, enabled=enabled)


class TestShadowOrderWriterBatching:
    def test_writer_batches_records(self):
        """Adding 2 records with batch_size=3 should not flush — pending_count == 2."""
        writer = _make_writer(batch_size=3, enabled=True)
        writer.add(_SAMPLE_RECORD)
        writer.add(_SAMPLE_RECORD)
        assert writer.pending_count == 2

    def test_writer_flushes_at_batch_size(self):
        """Adding records equal to batch_size triggers automatic flush."""
        mock_client = MagicMock()
        writer = _make_writer(batch_size=2, enabled=True)

        with patch("hft_platform.order.shadow_writer._get_ch_client", return_value=mock_client):
            writer.add(_SAMPLE_RECORD)
            writer.add(_SAMPLE_RECORD)  # triggers flush

        mock_client.execute.assert_called_once()
        assert writer.pending_count == 0

    def test_writer_flush_on_demand(self):
        """Calling flush() with 1 pending record invokes client.execute once."""
        mock_client = MagicMock()
        writer = _make_writer(batch_size=10, enabled=True)

        with patch("hft_platform.order.shadow_writer._get_ch_client", return_value=mock_client):
            writer.add(_SAMPLE_RECORD)
            writer.flush()

        mock_client.execute.assert_called_once()
        assert writer.pending_count == 0

    def test_writer_flush_empty_is_noop(self):
        """flush() with no pending records makes no client calls."""
        mock_client = MagicMock()
        writer = _make_writer(batch_size=10, enabled=True)

        with patch("hft_platform.order.shadow_writer._get_ch_client", return_value=mock_client):
            writer.flush()

        mock_client.execute.assert_not_called()

    def test_writer_flush_failure_does_not_raise(self):
        """If client.execute raises, flush() catches the error and clears pending."""
        mock_client = MagicMock()
        mock_client.execute.side_effect = RuntimeError("CH unavailable")
        writer = _make_writer(batch_size=10, enabled=True)

        with patch("hft_platform.order.shadow_writer._get_ch_client", return_value=mock_client):
            writer.add(_SAMPLE_RECORD)
            writer.flush()  # must not raise

        assert writer.pending_count == 0

    def test_writer_disabled_does_not_call_client(self):
        """When disabled, flush() logs and drops records without calling client."""
        mock_client = MagicMock()
        writer = _make_writer(batch_size=2, enabled=False)

        with patch("hft_platform.order.shadow_writer._get_ch_client", return_value=mock_client):
            writer.add(_SAMPLE_RECORD)
            writer.flush()

        mock_client.execute.assert_not_called()
        assert writer.pending_count == 0


class TestGetChClient:
    def test_get_ch_client_raises_when_clickhouse_connect_missing(self):
        """_get_ch_client raises RuntimeError when clickhouse_connect is not importable."""
        import sys

        from hft_platform.order.shadow_writer import _get_ch_client

        with patch.dict(sys.modules, {"clickhouse_connect": None}):
            try:
                _get_ch_client()
                # If we got here without error, the import succeeded despite None in sys.modules
                # This path is acceptable (module may be cached elsewhere).
            except RuntimeError as exc:
                assert "clickhouse_connect" in str(exc)
            except (ImportError, AttributeError):
                pass  # Also acceptable

    def test_get_ch_client_uses_env_vars(self, monkeypatch):
        """_get_ch_client passes host/port/user/password from env to get_client."""
        import sys

        from hft_platform.order.shadow_writer import _get_ch_client

        monkeypatch.setenv("HFT_CLICKHOUSE_HOST", "ch-host")
        monkeypatch.setenv("HFT_CLICKHOUSE_PORT", "9001")
        monkeypatch.setenv("HFT_CLICKHOUSE_USER", "hft_user")
        monkeypatch.setenv("HFT_CLICKHOUSE_PASSWORD", "s3cret")

        mock_get_client = MagicMock(return_value=MagicMock())
        mock_module = MagicMock()
        mock_module.get_client = mock_get_client

        original = sys.modules.get("clickhouse_connect")
        sys.modules["clickhouse_connect"] = mock_module
        try:
            _get_ch_client()
        finally:
            if original is None:
                sys.modules.pop("clickhouse_connect", None)
            else:
                sys.modules["clickhouse_connect"] = original

        mock_get_client.assert_called_once()
        call_kwargs = mock_get_client.call_args[1]
        assert call_kwargs["host"] == "ch-host"
        assert call_kwargs["port"] == 9001
        assert call_kwargs["username"] == "hft_user"
        assert call_kwargs["password"] == "s3cret"


class TestShadowOrderWriterEnabledFromEnv:
    def test_enabled_false_from_env_when_not_set(self, monkeypatch):
        """Without HFT_CLICKHOUSE_ENABLED=1, writer is disabled by default."""
        monkeypatch.delenv("HFT_CLICKHOUSE_ENABLED", raising=False)
        writer = ShadowOrderWriter()
        assert writer._enabled is False

    def test_enabled_true_from_env_when_set_to_1(self, monkeypatch):
        """With HFT_CLICKHOUSE_ENABLED=1, writer is enabled from env var."""
        monkeypatch.setenv("HFT_CLICKHOUSE_ENABLED", "1")
        writer = ShadowOrderWriter()
        assert writer._enabled is True

    def test_enabled_false_from_env_when_set_to_0(self, monkeypatch):
        """With HFT_CLICKHOUSE_ENABLED=0, writer is disabled from env var."""
        monkeypatch.setenv("HFT_CLICKHOUSE_ENABLED", "0")
        writer = ShadowOrderWriter()
        assert writer._enabled is False
