from __future__ import annotations

import time
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


def test_forced_relogin_uses_session_policy_and_clears_flag(tmp_path):
    client, patcher = _build_client(tmp_path)
    try:
        policy = MagicMock()
        policy.request_reconnect.side_effect = RuntimeError("boom")
        client._session_policy = policy
        client._allow_quote_recovery = MagicMock(return_value=True)

        client._start_forced_relogin("unit-test")

        deadline = time.time() + 1.0
        while client._pending_quote_relogining and time.time() < deadline:
            time.sleep(0.01)

        assert client._pending_quote_relogining is False
        policy.request_reconnect.assert_called_once_with(reason="unit-test", force=True)
    finally:
        client.close()
        patcher.stop()


def test_quote_pending_relogin_clears_flag_after_attempt(tmp_path):
    client, patcher = _build_client(tmp_path)
    try:
        client._quote_force_relogin_s = 0.01
        client._pending_quote_resubscribe = True
        client._session_policy = MagicMock()
        client._session_policy.request_reconnect.return_value = False
        client._allow_quote_recovery = MagicMock(return_value=True)

        client._schedule_force_relogin()

        deadline = time.time() + 1.0
        while client._pending_quote_relogining and time.time() < deadline:
            time.sleep(0.01)

        assert client._pending_quote_relogining is False
        client._session_policy.request_reconnect.assert_called_once_with(reason="quote_pending", force=True)
    finally:
        client.close()
        patcher.stop()


def test_close_marks_all_thread_metrics_down(tmp_path):
    client, patcher = _build_client(tmp_path)
    try:
        client._set_thread_alive_metric = MagicMock()
        client.close()
    finally:
        patcher.stop()

    expected = {
        "quote_watchdog",
        "callback_retry",
        "event_callback_retry",
        "quote_relogin",
        "force_relogin",
        "session_refresh",
        "sub_retry",
        "contract_refresh",
    }
    seen = {call.args[0] for call in client._set_thread_alive_metric.call_args_list if call.args}
    assert expected.issubset(seen)
