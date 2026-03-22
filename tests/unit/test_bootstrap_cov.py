"""Coverage tests for services/bootstrap.py — targeting 80%+ line coverage."""
from __future__ import annotations

import os
from unittest.mock import MagicMock, patch

import pytest


def _make_bootstrapper(settings=None):
    from hft_platform.services.bootstrap import SystemBootstrapper

    return SystemBootstrapper(settings or {})


# ---------------------------------------------------------------------------
# _get_redis_lease_params
# ---------------------------------------------------------------------------


def test_get_redis_lease_params_defaults(monkeypatch):
    monkeypatch.delenv("HFT_REDIS_HOST", raising=False)
    monkeypatch.delenv("REDIS_HOST", raising=False)
    monkeypatch.delenv("HFT_REDIS_PORT", raising=False)
    monkeypatch.delenv("REDIS_PORT", raising=False)
    monkeypatch.delenv("HFT_REDIS_PASSWORD", raising=False)
    monkeypatch.delenv("REDIS_PASSWORD", raising=False)
    monkeypatch.delenv("REDIS_PASS", raising=False)
    b = _make_bootstrapper()
    params = b._get_redis_lease_params()
    assert params["host"] == "redis"
    assert params["port"] == 6379
    assert params["password"] == ""
    assert "key" in params
    assert "ttl_s" in params
    assert "timeout_s" in params


def test_get_redis_lease_params_env_override(monkeypatch):
    monkeypatch.setenv("HFT_REDIS_HOST", "my-redis")
    monkeypatch.setenv("HFT_REDIS_PORT", "6380")
    monkeypatch.setenv("HFT_REDIS_PASSWORD", "secret")
    b = _make_bootstrapper()
    params = b._get_redis_lease_params()
    assert params["host"] == "my-redis"
    assert params["port"] == 6380
    assert params["password"] == "secret"


def test_get_redis_lease_params_redis_host_fallback(monkeypatch):
    monkeypatch.delenv("HFT_REDIS_HOST", raising=False)
    monkeypatch.setenv("REDIS_HOST", "redis-fallback")
    b = _make_bootstrapper()
    params = b._get_redis_lease_params()
    assert params["host"] == "redis-fallback"


def test_get_redis_lease_params_redis_port_fallback(monkeypatch):
    monkeypatch.delenv("HFT_REDIS_PORT", raising=False)
    monkeypatch.setenv("REDIS_PORT", "6381")
    b = _make_bootstrapper()
    params = b._get_redis_lease_params()
    assert params["port"] == 6381


def test_get_redis_lease_params_redis_pass_fallback(monkeypatch):
    monkeypatch.delenv("HFT_REDIS_PASSWORD", raising=False)
    monkeypatch.delenv("REDIS_PASSWORD", raising=False)
    monkeypatch.setenv("REDIS_PASS", "pass123")
    b = _make_bootstrapper()
    params = b._get_redis_lease_params()
    assert params["password"] == "pass123"


def test_get_redis_lease_params_ttl_env(monkeypatch):
    monkeypatch.setenv("HFT_FEED_SESSION_OWNER_TTL_S", "600")
    b = _make_bootstrapper()
    params = b._get_redis_lease_params()
    assert params["ttl_s"] == 600


def test_get_redis_lease_params_ttl_minimum(monkeypatch):
    monkeypatch.setenv("HFT_FEED_SESSION_OWNER_TTL_S", "1")
    b = _make_bootstrapper()
    params = b._get_redis_lease_params()
    assert params["ttl_s"] == 30


# ---------------------------------------------------------------------------
# _lease_is_stale
# ---------------------------------------------------------------------------


def test_lease_is_stale_missing():
    from hft_platform.services.bootstrap import SystemBootstrapper

    assert SystemBootstrapper._lease_is_stale(-2, 0) is True


def test_lease_is_stale_no_expire():
    from hft_platform.services.bootstrap import SystemBootstrapper

    assert SystemBootstrapper._lease_is_stale(-1, 0) is True


def test_lease_is_stale_within_takeover():
    from hft_platform.services.bootstrap import SystemBootstrapper

    assert SystemBootstrapper._lease_is_stale(10, 30) is True


def test_lease_is_stale_above_takeover():
    from hft_platform.services.bootstrap import SystemBootstrapper

    assert SystemBootstrapper._lease_is_stale(100, 30) is False


def test_lease_is_stale_no_takeover_positive_ttl():
    from hft_platform.services.bootstrap import SystemBootstrapper

    assert SystemBootstrapper._lease_is_stale(100, 0) is False


# ---------------------------------------------------------------------------
# _read_int_resp
# ---------------------------------------------------------------------------


def test_read_int_resp_int():
    from hft_platform.services.bootstrap import SystemBootstrapper

    assert SystemBootstrapper._read_int_resp(42) == 42


def test_read_int_resp_str():
    from hft_platform.services.bootstrap import SystemBootstrapper

    assert SystemBootstrapper._read_int_resp("99") == 99


def test_read_int_resp_none():
    from hft_platform.services.bootstrap import SystemBootstrapper

    assert SystemBootstrapper._read_int_resp(None, default=-2) == -2


def test_read_int_resp_bad():
    from hft_platform.services.bootstrap import SystemBootstrapper

    assert SystemBootstrapper._read_int_resp("abc", default=-99) == -99


# ---------------------------------------------------------------------------
# _record_lease_metric
# ---------------------------------------------------------------------------


def test_record_lease_metric_no_registry():
    from hft_platform.services.bootstrap import SystemBootstrapper

    with patch("hft_platform.observability.metrics.MetricsRegistry.get", side_effect=Exception("no metrics")):
        SystemBootstrapper._record_lease_metric("preflight", "acquired")


def test_record_lease_metric_with_registry():
    from hft_platform.services.bootstrap import SystemBootstrapper

    m = MagicMock()
    m.feed_session_lease_ops_total.labels.return_value = MagicMock()
    with patch("hft_platform.observability.metrics.MetricsRegistry.get", return_value=m):
        SystemBootstrapper._record_lease_metric("preflight", "acquired")
        m.feed_session_lease_ops_total.labels.assert_called_once()


# ---------------------------------------------------------------------------
# _check_session_ownership
# ---------------------------------------------------------------------------


def test_check_session_ownership_non_engine_role():
    b = _make_bootstrapper()
    result = b._check_session_ownership("monitor")
    assert result is False


def test_check_session_ownership_connection_failure():
    b = _make_bootstrapper()
    with patch("socket.create_connection", side_effect=ConnectionRefusedError("refused")):
        result = b._check_session_ownership("engine")
    assert result is False


# ---------------------------------------------------------------------------
# _stop_lease_refresh_thread
# ---------------------------------------------------------------------------


def test_stop_lease_refresh_thread_no_thread():
    b = _make_bootstrapper()
    b._lease_refresh_running = False
    b._lease_refresh_thread = None
    b._stop_lease_refresh_thread()
    assert b._lease_refresh_thread is None


def test_stop_lease_refresh_thread_alive():
    import threading

    b = _make_bootstrapper()
    b._lease_refresh_running = True
    event = threading.Event()

    def _run():
        event.wait(timeout=2)

    t = threading.Thread(target=_run, daemon=True)
    t.start()
    b._lease_refresh_thread = t
    b._stop_lease_refresh_thread()
    assert b._lease_refresh_thread is None


# ---------------------------------------------------------------------------
# teardown
# ---------------------------------------------------------------------------


def test_teardown_non_engine_role():
    b = _make_bootstrapper()
    b._last_role = "monitor"
    b._stop_lease_refresh_thread = MagicMock()
    b.teardown()
    b._stop_lease_refresh_thread.assert_called_once()


def test_teardown_engine_role_connection_failure():
    b = _make_bootstrapper()
    b._last_role = "engine"
    b._stop_lease_refresh_thread = MagicMock()
    with patch("socket.create_connection", side_effect=ConnectionRefusedError("refused")):
        b.teardown()


# ---------------------------------------------------------------------------
# _build_feature_engine
# ---------------------------------------------------------------------------


def test_build_feature_engine_disabled(monkeypatch):
    monkeypatch.setenv("HFT_FEATURE_ENGINE_ENABLED", "0")
    b = _make_bootstrapper()
    result = b._build_feature_engine()
    fe, _, _, _, _ = result
    assert fe is None


def test_build_feature_engine_enabled(monkeypatch):
    monkeypatch.setenv("HFT_FEATURE_ENGINE_ENABLED", "1")
    b = _make_bootstrapper()
    with patch("hft_platform.services.bootstrap.FeatureEngine") as MockFE:
        mock_fe = MagicMock()
        mock_fe.feature_set_id.return_value = "v1"
        MockFE.return_value = mock_fe
        with patch("hft_platform.services.bootstrap.load_feature_profile_registry", return_value=None):
            with patch("hft_platform.services.bootstrap.load_feature_rollout_controller", return_value=None):
                result = b._build_feature_engine()
    fe = result[0]
    assert fe is not None


# ---------------------------------------------------------------------------
# _build_broker_clients
# ---------------------------------------------------------------------------


def test_build_broker_clients_non_engine_role():
    from hft_platform.services.bootstrap import _RoleGuardedNoopClient

    b = _make_bootstrapper()
    md, order = b._build_broker_clients(
        role="monitor",
        symbols_path="config/base/symbols.yaml",
        base_shioaji_cfg={},
        broker_id="shioaji",
    )
    assert isinstance(md, _RoleGuardedNoopClient)
    assert isinstance(order, _RoleGuardedNoopClient)


def test_build_broker_clients_order_mode_sim(monkeypatch):
    from hft_platform.services.bootstrap import _RoleGuardedNoopClient

    monkeypatch.setenv("HFT_ORDER_MODE", "sim")
    b = _make_bootstrapper()
    md, order = b._build_broker_clients(
        role="maintenance",
        symbols_path="config/base/symbols.yaml",
        base_shioaji_cfg={},
        broker_id="shioaji",
    )
    assert isinstance(order, _RoleGuardedNoopClient)


def test_build_broker_clients_order_simulation_env(monkeypatch):
    monkeypatch.setenv("HFT_ORDER_SIMULATION", "1")
    monkeypatch.delenv("HFT_ORDER_MODE", raising=False)
    b = _make_bootstrapper()
    md, order = b._build_broker_clients(
        role="monitor",
        symbols_path="config/base/symbols.yaml",
        base_shioaji_cfg={},
        broker_id="shioaji",
    )
    from hft_platform.services.bootstrap import _RoleGuardedNoopClient
    assert isinstance(md, _RoleGuardedNoopClient)


# ---------------------------------------------------------------------------
# _RoleGuardedNoopClient
# ---------------------------------------------------------------------------


def test_role_guarded_noop_client_methods():
    from hft_platform.services.bootstrap import _RoleGuardedNoopClient

    c = _RoleGuardedNoopClient("monitor")
    assert c.login() is False
    assert c.reconnect() is False
    c.subscribe_basket(lambda x: None)
    assert c.fetch_snapshots() == []
    assert c.validate_symbols() == []
    assert c.get_positions() == []
    assert c.list_position_detail() == []
    result = c.place_order()
    assert result["status"] == "blocked"
    result = c.cancel_order(None)
    assert result["status"] == "blocked"
    result = c.update_order(None)
    assert result["status"] == "blocked"
    c.close()
    assert c.logged_in is False
    c.shutdown()
    assert c.logged_in is False


# ---------------------------------------------------------------------------
# _resolve_broker_id
# ---------------------------------------------------------------------------


def test_resolve_broker_id_shioaji(monkeypatch):
    from hft_platform.services.bootstrap import SystemBootstrapper

    monkeypatch.setenv("HFT_BROKER", "shioaji")
    assert SystemBootstrapper._resolve_broker_id() == "shioaji"


def test_resolve_broker_id_fubon(monkeypatch):
    from hft_platform.services.bootstrap import SystemBootstrapper

    monkeypatch.setenv("HFT_BROKER", "fubon")
    assert SystemBootstrapper._resolve_broker_id() == "fubon"


def test_resolve_broker_id_unknown(monkeypatch):
    from hft_platform.services.bootstrap import SystemBootstrapper

    monkeypatch.setenv("HFT_BROKER", "bogus")
    with pytest.raises(ValueError, match="Unknown HFT_BROKER"):
        SystemBootstrapper._resolve_broker_id()


# ---------------------------------------------------------------------------
# Queue size defaults
# ---------------------------------------------------------------------------


def test_queue_size_defaults():
    from hft_platform.services.bootstrap import SystemBootstrapper

    assert SystemBootstrapper.DEFAULT_RAW_QUEUE_SIZE == 65536
    assert SystemBootstrapper.DEFAULT_RISK_QUEUE_SIZE == 4096
    assert SystemBootstrapper.DEFAULT_RECORDER_QUEUE_SIZE == 16384


# ---------------------------------------------------------------------------
# _encode_resp / _read_resp
# ---------------------------------------------------------------------------


def test_encode_resp():
    from hft_platform.services.bootstrap import _encode_resp

    result = _encode_resp("SET", "key", "value")
    assert b"*3" in result
    assert b"SET" in result


def test_read_resp_simple_string():
    import io
    from hft_platform.services.bootstrap import _read_resp

    stream = io.BytesIO(b"+OK\r\n")
    result = _read_resp(stream)
    assert result == "OK"


def test_read_resp_integer():
    import io
    from hft_platform.services.bootstrap import _read_resp

    stream = io.BytesIO(b":42\r\n")
    result = _read_resp(stream)
    assert result == 42


def test_read_resp_bulk_string():
    import io
    from hft_platform.services.bootstrap import _read_resp

    stream = io.BytesIO(b"$5\r\nhello\r\n")
    result = _read_resp(stream)
    assert result == "hello"


def test_read_resp_null_bulk():
    import io
    from hft_platform.services.bootstrap import _read_resp

    stream = io.BytesIO(b"$-1\r\n")
    result = _read_resp(stream)
    assert result is None


def test_read_resp_error():
    import io
    from hft_platform.services.bootstrap import _read_resp

    stream = io.BytesIO(b"-ERR something went wrong\r\n")
    with pytest.raises(RuntimeError, match="redis error"):
        _read_resp(stream)


def test_read_resp_empty():
    import io
    from hft_platform.services.bootstrap import _read_resp

    stream = io.BytesIO(b"")
    with pytest.raises(RuntimeError, match="empty redis response"):
        _read_resp(stream)


def test_read_resp_unknown_prefix():
    import io
    from hft_platform.services.bootstrap import _read_resp

    stream = io.BytesIO(b"!badprefix\r\n")
    with pytest.raises(RuntimeError, match="unsupported redis response"):
        _read_resp(stream)
