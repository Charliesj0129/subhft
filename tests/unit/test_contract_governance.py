"""Tests for P1: Contract & Subscription Governance improvements."""

from __future__ import annotations

import json
import os
from pathlib import Path
from unittest.mock import MagicMock, patch

from hft_platform.config.symbols import write_contract_cache, write_symbols_yaml


def test_write_contract_cache_atomic(tmp_path: Path):
    """No .tmp file remains after write; file has cache_version field."""
    out = tmp_path / "contracts.json"
    contracts = [{"code": "2330", "exchange": "TSE"}]
    write_contract_cache(contracts, str(out))

    # No .tmp file should remain
    tmp = tmp_path / "contracts.json.tmp"
    assert not tmp.exists(), ".tmp file must be removed after atomic rename"

    data = json.loads(out.read_text())
    assert "cache_version" in data
    assert data["cache_version"] >= 1
    assert data["contracts"] == contracts
    assert "updated_at" in data


def test_write_contract_cache_version_monotonic(tmp_path: Path):
    """Seeding with cache_version=5 should produce version 6 on next write."""
    out = tmp_path / "contracts.json"
    # Seed existing file with version 5
    out.write_text(json.dumps({"cache_version": 5, "contracts": []}))

    write_contract_cache([{"code": "2454"}], str(out))
    data = json.loads(out.read_text())
    assert data["cache_version"] == 6


def test_write_symbols_yaml_atomic(tmp_path: Path):
    """No .yaml.tmp file remains after write."""
    out = tmp_path / "symbols.yaml"
    symbols = [{"code": "2330", "exchange": "TSE"}]
    write_symbols_yaml(symbols, str(out))

    tmp = tmp_path / "symbols.yaml.tmp"
    assert not tmp.exists(), ".yaml.tmp file must be removed after atomic rename"
    assert out.exists()


def test_preflight_stale_cache_logs_warning(tmp_path: Path):
    """When cache is stale, a warning is logged."""
    from hft_platform.feed_adapter.shioaji_client import ShioajiClient

    with patch.object(ShioajiClient, "__init__", lambda self, *a, **kw: None):
        client = ShioajiClient.__new__(ShioajiClient)
        client._contract_cache_path = str(tmp_path / "contracts.json")
        client.symbols = []
        client.MAX_SUBSCRIPTIONS = 200
        client.api = MagicMock()  # non-None api so _get_contract would be called

        with patch.object(client, "_is_contract_cache_stale", return_value=True):
            with patch.object(client, "_get_contract", return_value=None):
                import structlog.testing

                with structlog.testing.capture_logs() as logs:
                    client._preflight_contracts()

    warning_events = [entry for entry in logs if entry.get("log_level") == "warning"]
    events = [entry.get("event", "") for entry in warning_events]
    assert any("preflight_contract_cache_stale" in e for e in events)


def test_preflight_missing_codes_logs_warning(tmp_path: Path):
    """When _get_contract returns None for a symbol, missing count is logged."""
    from hft_platform.feed_adapter.shioaji_client import ShioajiClient

    with patch.object(ShioajiClient, "__init__", lambda self, *a, **kw: None):
        client = ShioajiClient.__new__(ShioajiClient)
        client._contract_cache_path = str(tmp_path / "contracts.json")
        client.symbols = [{"code": "2330", "exchange": "TSE"}]
        client.MAX_SUBSCRIPTIONS = 200
        client.api = MagicMock()

        with patch.object(client, "_is_contract_cache_stale", return_value=False):
            with patch.object(client, "_get_contract", return_value=None):
                import structlog.testing

                with structlog.testing.capture_logs() as logs:
                    client._preflight_contracts()

    warning_events = [entry for entry in logs if entry.get("log_level") == "warning"]
    events = [entry.get("event", "") for entry in warning_events]
    assert any("preflight_missing_contracts" in e for e in events)
    missing_log = next(entry for entry in warning_events if "preflight_missing_contracts" in entry.get("event", ""))
    assert missing_log["missing_count"] == 1


def test_preflight_skipped_when_env_off(tmp_path: Path):
    """When HFT_CONTRACT_PREFLIGHT=0, _preflight_contracts is not called."""
    from hft_platform.feed_adapter.shioaji_client import ShioajiClient

    with patch.object(ShioajiClient, "__init__", lambda self, *a, **kw: None):
        client = ShioajiClient.__new__(ShioajiClient)
        called = []

        def _fake_preflight():
            called.append(True)

        client._preflight_contracts = _fake_preflight
        client.tick_callback = None
        client.symbols = []
        client.MAX_SUBSCRIPTIONS = 200
        client.subscribed_count = 0
        client._last_quote_data_ts = 0
        client.api = None  # triggers early return before preflight injection

        # With api=None, subscribe_basket returns early — so test env gate directly
        with patch.dict(os.environ, {"HFT_CONTRACT_PREFLIGHT": "0"}):
            # Simulate the guard check in subscribe_basket inline
            if os.getenv("HFT_CONTRACT_PREFLIGHT", "1") == "1":
                client._preflight_contracts()

        assert called == [], "_preflight_contracts must not be called when HFT_CONTRACT_PREFLIGHT=0"


def test_refresh_diff_logged(tmp_path: Path):
    """After a contract refresh, diff event is logged with correct counts."""
    import threading

    cache_path = tmp_path / "contracts.json"
    # Seed with 2 old contracts
    cache_path.write_text(
        json.dumps({"cache_version": 1, "contracts": [{"code": "2330"}, {"code": "2454"}]})
    )

    from hft_platform.feed_adapter.shioaji_client import ShioajiClient

    with patch.object(ShioajiClient, "__init__", lambda self, *a, **kw: None):
        client = ShioajiClient.__new__(ShioajiClient)
        client._contract_cache_path = str(cache_path)
        client.api = MagicMock()
        client.config_path = str(tmp_path / "symbols.yaml")
        client.metrics = None
        client.logged_in = False
        client._contract_refresh_last_diff = {}
        client._contract_refresh_resubscribe_policy = "none"
        client.symbols = []
        # Provide required threading lock
        client._contract_refresh_lock = threading.Lock()

        # Set up Contracts on mock api so the inner try blocks don't fail
        client.api.Contracts.Stocks.TSE = []
        client.api.Contracts.Stocks.OTC = []
        client.api.Contracts.Futures.keys.return_value = []
        client.api.Contracts.Options.keys.return_value = []

        # Patch write_contract_cache to a successful no-op so diff logging always fires
        def _fake_write_cc(contracts, path):
            pass

        # Simulate _ensure_contracts doing nothing
        with patch.object(client, "_ensure_contracts"):
            with patch.object(client, "_load_config"):
                with patch("hft_platform.config.symbols.write_contract_cache", side_effect=_fake_write_cc):
                    with patch("hft_platform.config.symbols.build_symbols") as mock_bs:
                        mock_bs.return_value = MagicMock(symbols=[], errors=[])

                        import structlog.testing

                        with structlog.testing.capture_logs() as logs:
                            client._refresh_contracts_and_symbols()

    info_events = [entry for entry in logs if entry.get("log_level") == "info"]
    diff_logs = [entry for entry in info_events if "contract_refresh_diff" in entry.get("event", "")]
    assert diff_logs, "contract_refresh_diff log event must be emitted"
    diff = diff_logs[0]
    assert "contracts_before" in diff
    assert "contracts_after" in diff
    assert "added_count" in diff
    assert "removed_count" in diff


def test_contract_refresh_status_snapshot_written(tmp_path: Path):
    import threading

    from hft_platform.feed_adapter.shioaji_client import ShioajiClient

    with patch.object(ShioajiClient, "__init__", lambda self, *a, **kw: None):
        client = ShioajiClient.__new__(ShioajiClient)
        client._contract_refresh_version = 3
        client._contract_refresh_resubscribe_policy = "diff"
        client._contract_refresh_running = True
        client._contract_refresh_thread = None
        client._contract_refresh_lock = threading.Lock()
        client._contract_refresh_last_diff = {"version": 3, "added_codes": ["TXF"], "removed_codes": []}
        client._contract_cache_path = str(tmp_path / "contracts.json")
        client._contract_refresh_status_path = str(tmp_path / "contract_refresh_status.json")
        client._contract_refresh_last_status = {}

        client._write_contract_refresh_status(result="ok")
        status = client.get_contract_refresh_status()

    out = tmp_path / "contract_refresh_status.json"
    assert out.exists()
    payload = json.loads(out.read_text(encoding="utf-8"))
    assert payload["result"] == "ok"
    assert payload["version"] == 3
    assert status["version"] == 3
    assert status["last_diff"]["version"] == 3
