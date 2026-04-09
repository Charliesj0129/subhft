"""Coverage tests for services/bootstrap.py — targeting uncovered branches."""

from __future__ import annotations

import asyncio
from unittest.mock import MagicMock, patch

import pytest

# ---------------------------------------------------------------------------
# Module-level helpers: _env_float / _env_int
# ---------------------------------------------------------------------------


def test_env_float_valid_value(monkeypatch):
    monkeypatch.setenv("HFT_TEST_BS_FLOAT", "3.7")
    from hft_platform.services.bootstrap import _env_float

    result = _env_float("HFT_TEST_BS_FLOAT", 1.0, 0.1)
    assert result == pytest.approx(3.7)


def test_env_float_uses_default_when_missing(monkeypatch):
    monkeypatch.delenv("HFT_TEST_BS_FLOAT_MISSING", raising=False)
    from hft_platform.services.bootstrap import _env_float

    result = _env_float("HFT_TEST_BS_FLOAT_MISSING", 2.5, 0.0)
    assert result == pytest.approx(2.5)


def test_env_float_clamps_to_min_value(monkeypatch):
    monkeypatch.setenv("HFT_TEST_BS_FLOAT_LOW", "0.001")
    from hft_platform.services.bootstrap import _env_float

    result = _env_float("HFT_TEST_BS_FLOAT_LOW", 1.0, 0.5)
    assert result == pytest.approx(0.5)


def test_env_float_invalid_uses_default(monkeypatch):
    monkeypatch.setenv("HFT_TEST_BS_FLOAT_BAD", "not_a_number")
    from hft_platform.services.bootstrap import _env_float

    result = _env_float("HFT_TEST_BS_FLOAT_BAD", 9.9, 0.0)
    assert result == pytest.approx(9.9)


def test_env_int_valid_value(monkeypatch):
    monkeypatch.setenv("HFT_TEST_BS_INT", "42")
    from hft_platform.services.bootstrap import _env_int

    result = _env_int("HFT_TEST_BS_INT", 0, 0)
    assert result == 42


def test_env_int_clamps_to_min_value(monkeypatch):
    monkeypatch.setenv("HFT_TEST_BS_INT_LOW", "0")
    from hft_platform.services.bootstrap import _env_int

    result = _env_int("HFT_TEST_BS_INT_LOW", 5, 3)
    assert result == 3


def test_env_int_invalid_uses_default(monkeypatch):
    monkeypatch.setenv("HFT_TEST_BS_INT_BAD", "xyz")
    from hft_platform.services.bootstrap import _env_int

    result = _env_int("HFT_TEST_BS_INT_BAD", 7, 1)
    assert result == 7


# ---------------------------------------------------------------------------
# validate_order_mode_safety — missing HFT_LIVE_CONFIRM branch
# ---------------------------------------------------------------------------


def test_validate_order_mode_safety_live_without_confirm(monkeypatch):
    """live order mode + real mode but no HFT_LIVE_CONFIRM → SystemExit."""
    monkeypatch.setenv("HFT_MODE", "real")
    monkeypatch.setenv("HFT_ORDER_MODE", "live")
    monkeypatch.delenv("HFT_LIVE_CONFIRM", raising=False)

    from hft_platform.services.bootstrap import validate_order_mode_safety

    with pytest.raises(SystemExit):
        validate_order_mode_safety()


def test_validate_order_mode_safety_sim_mode_passes(monkeypatch):
    """sim mode with sim order mode → no error."""
    monkeypatch.setenv("HFT_MODE", "sim")
    monkeypatch.setenv("HFT_ORDER_MODE", "sim")

    from hft_platform.services.bootstrap import validate_order_mode_safety

    # Should not raise — sim mode requires no live confirmation
    result = validate_order_mode_safety()
    assert result is None


# ---------------------------------------------------------------------------
# log_shadow_config_summary
# ---------------------------------------------------------------------------


def test_log_shadow_config_summary_with_settings(monkeypatch):
    """log_shadow_config_summary runs without error with shadow settings."""
    monkeypatch.setenv("HFT_ORDER_SHADOW_MODE", "1")
    monkeypatch.setenv("HFT_ORDER_MODE", "sim")

    from hft_platform.services.bootstrap import log_shadow_config_summary

    # Should not raise — shadow config is logged without errors
    result = log_shadow_config_summary(settings={"shadow": {"enabled": True}})
    assert result is None


def test_log_shadow_config_summary_none_settings(monkeypatch):
    """log_shadow_config_summary runs without error when settings=None."""
    monkeypatch.delenv("HFT_ORDER_SHADOW_MODE", raising=False)
    monkeypatch.setenv("HFT_ORDER_MODE", "sim")

    from hft_platform.services.bootstrap import log_shadow_config_summary

    result = log_shadow_config_summary(settings=None)
    assert result is None


# ---------------------------------------------------------------------------
# build_platform_degrade_inputs
# ---------------------------------------------------------------------------


def test_build_platform_degrade_inputs_returns_instance():
    """build_platform_degrade_inputs returns a PlatformDegradeInputs instance."""
    from hft_platform.services.bootstrap import SystemBootstrapper

    bs = SystemBootstrapper(settings={})
    raw_queue = asyncio.Queue()
    raw_exec_queue = asyncio.Queue()
    recorder_queue = asyncio.Queue()
    risk_queue = asyncio.Queue()
    order_queue = asyncio.Queue()

    md_svc = MagicMock()
    recorder = MagicMock()

    with patch("hft_platform.observability.metrics.MetricsRegistry", create=True):
        result = bs.build_platform_degrade_inputs(
            md_service=md_svc,
            recorder=recorder,
            raw_queue=raw_queue,
            raw_exec_queue=raw_exec_queue,
            recorder_queue=recorder_queue,
            risk_queue=risk_queue,
            order_queue=order_queue,
        )

    # Should return an object with reduce_only_reasons
    assert hasattr(result, "reduce_only_reasons")


def test_build_platform_degrade_inputs_metrics_failure():
    """build_platform_degrade_inputs continues when MetricsRegistry raises."""
    from hft_platform.services.bootstrap import SystemBootstrapper

    bs = SystemBootstrapper(settings={})

    with patch("hft_platform.observability.metrics.MetricsRegistry") as MockMR:
        MockMR.get.side_effect = ImportError("metrics unavailable")
        result = bs.build_platform_degrade_inputs(
            md_service=MagicMock(),
            recorder=MagicMock(),
            raw_queue=asyncio.Queue(),
            raw_exec_queue=asyncio.Queue(),
            recorder_queue=asyncio.Queue(),
            risk_queue=asyncio.Queue(),
            order_queue=asyncio.Queue(),
        )

    assert result is not None


# ---------------------------------------------------------------------------
# _build_broker_clients — non-engine role returns noop client
# ---------------------------------------------------------------------------


def test_build_broker_clients_non_engine_role():
    """_build_broker_clients returns noop clients for non-engine roles."""
    from hft_platform.services.bootstrap import SystemBootstrapper, _RoleGuardedNoopClient

    bs = SystemBootstrapper(settings={})
    md, order = bs._build_broker_clients("monitor", "/fake/path", {}, "shioaji")
    assert isinstance(md, _RoleGuardedNoopClient)
    assert isinstance(order, _RoleGuardedNoopClient)


def test_build_broker_clients_engine_shioaji(monkeypatch):
    """_build_broker_clients engine role + shioaji returns ShioajiClientFacade mocks."""
    from hft_platform.services.bootstrap import SystemBootstrapper

    bs = SystemBootstrapper(settings={})
    mock_facade = MagicMock()

    with patch("hft_platform.feed_adapter.shioaji.facade.ShioajiClientFacade", return_value=mock_facade):
        md, order = bs._build_broker_clients("engine", "/fake/path", {}, "shioaji")

    assert md is mock_facade
    assert order is mock_facade


# ---------------------------------------------------------------------------
# _check_session_ownership — success: owner matches → SETEX called
# ---------------------------------------------------------------------------


def test_check_session_ownership_owner_matches(monkeypatch):
    """_check_session_ownership returns True when current owner matches our ID."""
    from hft_platform.services.bootstrap import SystemBootstrapper

    monkeypatch.setenv("HFT_RUNTIME_INSTANCE_ID", "myhost:1234")
    monkeypatch.setenv("HFT_FEED_SESSION_OWNER_KEY", "feed:session:owner")
    monkeypatch.setenv("HFT_REDIS_HOST", "redis")
    monkeypatch.setenv("HFT_REDIS_PORT", "6379")
    monkeypatch.delenv("HFT_REDIS_PASSWORD", raising=False)
    monkeypatch.setenv("HFT_FEED_SESSION_OWNER_TTL_S", "300")
    monkeypatch.setenv("HFT_FEED_SESSION_PREFLIGHT_TIMEOUT_S", "0.5")
    monkeypatch.delenv("HFT_FEED_SESSION_STALE_TAKEOVER_TTL_S", raising=False)

    bs = SystemBootstrapper(settings={})

    # Mock a Redis socket that returns our own owner_id via RESP
    import io

    def _make_stream_for(owner_id: str):
        # GET response: $<len>\r\n<owner_id>\r\n
        payload = f"${len(owner_id)}\r\n{owner_id}\r\n".encode()
        # SETEX response: +OK\r\n
        setex_response = b"+OK\r\n"
        return io.BytesIO(payload + setex_response)

    owner_id = "myhost:1234"
    fake_stream = _make_stream_for(owner_id)

    mock_sock = MagicMock()
    mock_sock.makefile.return_value = fake_stream
    mock_sock.__enter__ = lambda s: s
    mock_sock.__exit__ = MagicMock(return_value=False)
    mock_sock.sendall = MagicMock()

    with patch("socket.create_connection", return_value=mock_sock):
        result = bs._check_session_ownership("engine")

    assert result is True


def test_check_session_ownership_non_engine_returns_false():
    """_check_session_ownership returns False for non-engine roles."""
    from hft_platform.services.bootstrap import SystemBootstrapper

    bs = SystemBootstrapper(settings={})
    result = bs._check_session_ownership("monitor")
    assert result is False


def test_check_session_ownership_connection_failure(monkeypatch):
    """_check_session_ownership returns False on connection failure (swallowed)."""
    from hft_platform.services.bootstrap import SystemBootstrapper

    monkeypatch.setenv("HFT_RUNTIME_INSTANCE_ID", "myhost:1234")
    monkeypatch.setenv("HFT_REDIS_HOST", "redis")
    monkeypatch.setenv("HFT_REDIS_PORT", "6379")
    monkeypatch.delenv("HFT_REDIS_PASSWORD", raising=False)
    monkeypatch.setenv("HFT_FEED_SESSION_OWNER_TTL_S", "300")
    monkeypatch.setenv("HFT_FEED_SESSION_PREFLIGHT_TIMEOUT_S", "0.5")
    monkeypatch.delenv("HFT_FEED_SESSION_STALE_TAKEOVER_TTL_S", raising=False)
    bs = SystemBootstrapper(settings={})

    with patch("socket.create_connection", side_effect=ConnectionRefusedError("refused")):
        result = bs._check_session_ownership("engine")

    assert result is False


# ---------------------------------------------------------------------------
# _start_lease_refresh_thread
# ---------------------------------------------------------------------------


def test_start_lease_refresh_thread_starts_daemon():
    """_start_lease_refresh_thread sets _lease_refresh_running and starts a thread."""
    from hft_platform.services.bootstrap import SystemBootstrapper

    bs = SystemBootstrapper(settings={})
    bs._lease_refresh_running = False
    bs._lease_refresh_thread = None

    with patch("socket.create_connection", side_effect=ConnectionRefusedError("no redis")):
        bs._start_lease_refresh_thread(
            host="localhost",
            port=6379,
            password="",
            key="feed:session:owner",
            owner_id="test:pid",
            ttl_s=60,
            timeout_s=0.1,
        )

    assert bs._lease_refresh_running is True
    assert bs._lease_refresh_thread is not None
    assert bs._lease_refresh_thread.daemon is True

    # Stop the thread cleanly
    bs._lease_refresh_running = False
    bs._lease_refresh_thread.join(timeout=2.0)


def test_start_lease_refresh_thread_can_be_stopped():
    """_stop_lease_refresh_thread stops the daemon thread within timeout."""
    from hft_platform.services.bootstrap import SystemBootstrapper

    bs = SystemBootstrapper(settings={})
    bs._lease_refresh_running = False
    bs._lease_refresh_thread = None

    with patch("socket.create_connection", side_effect=ConnectionRefusedError("no redis")):
        bs._start_lease_refresh_thread(
            host="localhost",
            port=6379,
            password="",
            key="feed:session:owner",
            owner_id="test:pid",
            ttl_s=60,
            timeout_s=0.1,
        )

    # Now stop it
    bs._stop_lease_refresh_thread()

    assert bs._lease_refresh_running is False
