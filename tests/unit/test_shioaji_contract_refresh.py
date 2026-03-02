from __future__ import annotations

from unittest.mock import MagicMock, patch

from hft_platform.feed_adapter.shioaji_client import ShioajiClient


def _build_client(tmp_path):
    cfg = tmp_path / "symbols.yaml"
    cfg.write_text("symbols:\n  - code: '2330'\n    exchange: 'TSE'\n", encoding="utf-8")
    patcher = patch("hft_platform.feed_adapter.shioaji_client.sj")
    mock_sj = patcher.start()
    mock_api = MagicMock()
    mock_sj.Shioaji.return_value = mock_api
    client = ShioajiClient(config_path=str(cfg))
    client.api = mock_api
    return client, patcher


def test_contract_refresh_thread_triggers_immediate_refresh_when_stale(tmp_path):
    client, patcher = _build_client(tmp_path)
    try:
        calls = {"n": 0}

        def _refresh_once():
            calls["n"] += 1
            client._contract_refresh_running = False

        client._contract_refresh_s = 0.01
        with (
            patch.object(type(client._contracts_runtime), "is_contract_cache_stale", return_value=True),
            patch.object(type(client._contracts_runtime), "refresh_contracts_and_symbols", side_effect=_refresh_once),
            patch("hft_platform.feed_adapter.shioaji.contracts_runtime.time.sleep", return_value=None),
        ):
            client._start_contract_refresh_thread()
            if client._contract_refresh_thread is not None:
                client._contract_refresh_thread.join(timeout=1.0)

        assert calls["n"] == 1
        assert client._contract_refresh_running is False
    finally:
        client.close()
        patcher.stop()


def test_contract_refresh_thread_stops_cleanly(tmp_path):
    client, patcher = _build_client(tmp_path)
    try:
        client._contract_refresh_s = 3600.0

        def _sleep_once(_):
            client._contract_refresh_running = False

        with (
            patch.object(type(client._contracts_runtime), "is_contract_cache_stale", return_value=False),
            patch.object(type(client._contracts_runtime), "refresh_contracts_and_symbols"),
            patch("hft_platform.feed_adapter.shioaji.contracts_runtime.time.sleep", side_effect=_sleep_once),
        ):
            client._start_contract_refresh_thread()
            if client._contract_refresh_thread is not None:
                client._contract_refresh_thread.join(timeout=1.0)

        assert client._contract_refresh_running is False
        assert not (client._contract_refresh_thread and client._contract_refresh_thread.is_alive())
    finally:
        client.close()
        patcher.stop()
