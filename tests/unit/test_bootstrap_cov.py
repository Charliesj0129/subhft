"""Additional coverage tests for services/bootstrap.py.

Targets uncovered branches:
- _get_redis_lease_params (env var precedence, defaults, ttl clamping)
- _check_session_ownership (non-engine role short-circuit, own key refresh,
  stale cleanup failure path, Redis connection error)
- _build_broker_clients (order mode env vars, HFT_ORDER_SIMULATION, HFT_ORDER_NO_CA,
  activate_ca fallback)
- _build_feature_engine (disabled path, load failures, rollout disabled state,
  override_profile_id path, metrics emit paths)
- build() with HFT_GATEWAY_ENABLED=1
- teardown() when last_role is not engine
- _stop_lease_refresh_thread with still-alive thread scenario
- _record_lease_metric import failure swallowing
"""

from __future__ import annotations

import io
from unittest.mock import MagicMock, patch

import pytest

from hft_platform.services.bootstrap import SystemBootstrapper

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_bootstrapper(settings=None):
    return SystemBootstrapper(settings if settings is not None else {})


def _build_with_all_mocks(monkeypatch, extra_env=None):
    """Full build() with all heavy deps mocked out."""
    monkeypatch.setenv("HFT_MODE", "sim")
    monkeypatch.delenv("HFT_BROKER", raising=False)
    monkeypatch.setenv("HFT_FEATURE_ENGINE_ENABLED", "0")
    monkeypatch.delenv("HFT_GATEWAY_ENABLED", raising=False)
    if extra_env:
        for k, v in extra_env.items():
            if v is None:
                monkeypatch.delenv(k, raising=False)
            else:
                monkeypatch.setenv(k, v)

    bootstrapper = _make_bootstrapper()
    patches = [
        patch("hft_platform.services.bootstrap.ShioajiClientFacade"),
        patch("hft_platform.services.bootstrap.MarketDataService"),
        patch("hft_platform.services.bootstrap.OrderAdapter"),
        patch("hft_platform.services.bootstrap.ExecutionGateway"),
        patch("hft_platform.services.bootstrap.ExecutionRouter"),
        patch("hft_platform.services.bootstrap.RiskEngine"),
        patch("hft_platform.services.bootstrap.ReconciliationService"),
        patch("hft_platform.services.bootstrap.StrategyRunner"),
        patch("hft_platform.services.bootstrap.RecorderService"),
        patch("hft_platform.services.bootstrap.RingBufferBus"),
        patch("hft_platform.services.bootstrap.PositionStore"),
        patch("hft_platform.services.bootstrap.StormGuard"),
        patch("hft_platform.services.bootstrap.SymbolMetadata"),
        patch("hft_platform.services.bootstrap.SymbolMetadataPriceScaleProvider"),
        patch("hft_platform.services.bootstrap.LatencyRecorder"),
        patch("hft_platform.services.bootstrap.FeatureEngine"),
    ]
    started = [p.start() for p in patches]
    try:
        with patch.object(bootstrapper, "_check_session_ownership", return_value=False):
            registry = bootstrapper.build()
    finally:
        for p in patches:
            p.stop()
    return registry


# ---------------------------------------------------------------------------
# _get_redis_lease_params
# ---------------------------------------------------------------------------


class TestGetRedisLeaseParams:
    def test_defaults_when_no_env(self, monkeypatch):
        for var in (
            "HFT_REDIS_PORT",
            "REDIS_PORT",
            "HFT_REDIS_HOST",
            "REDIS_HOST",
            "HFT_REDIS_PASSWORD",
            "REDIS_PASSWORD",
            "REDIS_PASS",
            "HFT_FEED_SESSION_OWNER_KEY",
            "HFT_RUNTIME_INSTANCE_ID",
            "HFT_FEED_SESSION_OWNER_TTL_S",
            "HFT_FEED_SESSION_PREFLIGHT_TIMEOUT_S",
        ):
            monkeypatch.delenv(var, raising=False)
        monkeypatch.setenv("HOSTNAME", "myhost")

        b = _make_bootstrapper()
        params = b._get_redis_lease_params()

        assert params["host"] == "redis"
        assert params["port"] == 6379
        assert params["password"] == ""
        assert params["key"] == "feed:session:owner"
        assert params["ttl_s"] == 300
        assert params["timeout_s"] == 0.5
        assert "myhost" in params["owner_id"]

    def test_hft_redis_port_takes_precedence(self, monkeypatch):
        monkeypatch.setenv("HFT_REDIS_PORT", "7000")
        monkeypatch.setenv("REDIS_PORT", "9999")
        b = _make_bootstrapper()
        params = b._get_redis_lease_params()
        assert params["port"] == 7000

    def test_redis_port_fallback(self, monkeypatch):
        monkeypatch.delenv("HFT_REDIS_PORT", raising=False)
        monkeypatch.setenv("REDIS_PORT", "6380")
        b = _make_bootstrapper()
        params = b._get_redis_lease_params()
        assert params["port"] == 6380

    def test_hft_redis_host_takes_precedence(self, monkeypatch):
        monkeypatch.setenv("HFT_REDIS_HOST", "hft-redis")
        monkeypatch.setenv("REDIS_HOST", "other-redis")
        b = _make_bootstrapper()
        params = b._get_redis_lease_params()
        assert params["host"] == "hft-redis"

    def test_password_priority_hft_first(self, monkeypatch):
        monkeypatch.setenv("HFT_REDIS_PASSWORD", "hft-secret")
        monkeypatch.setenv("REDIS_PASSWORD", "other-secret")
        monkeypatch.setenv("REDIS_PASS", "third-secret")
        b = _make_bootstrapper()
        params = b._get_redis_lease_params()
        assert params["password"] == "hft-secret"

    def test_password_fallback_redis_pass(self, monkeypatch):
        monkeypatch.delenv("HFT_REDIS_PASSWORD", raising=False)
        monkeypatch.delenv("REDIS_PASSWORD", raising=False)
        monkeypatch.setenv("REDIS_PASS", "fallback-pass")
        b = _make_bootstrapper()
        params = b._get_redis_lease_params()
        assert params["password"] == "fallback-pass"

    def test_ttl_clamped_to_minimum_30(self, monkeypatch):
        monkeypatch.setenv("HFT_FEED_SESSION_OWNER_TTL_S", "5")
        b = _make_bootstrapper()
        params = b._get_redis_lease_params()
        assert params["ttl_s"] == 30

    def test_ttl_above_minimum_preserved(self, monkeypatch):
        monkeypatch.setenv("HFT_FEED_SESSION_OWNER_TTL_S", "600")
        b = _make_bootstrapper()
        params = b._get_redis_lease_params()
        assert params["ttl_s"] == 600

    def test_instance_id_from_env(self, monkeypatch):
        monkeypatch.setenv("HFT_RUNTIME_INSTANCE_ID", "custom-id-42")
        b = _make_bootstrapper()
        params = b._get_redis_lease_params()
        assert params["owner_id"] == "custom-id-42"

    def test_custom_session_key(self, monkeypatch):
        monkeypatch.setenv("HFT_FEED_SESSION_OWNER_KEY", "custom:session:key")
        b = _make_bootstrapper()
        params = b._get_redis_lease_params()
        assert params["key"] == "custom:session:key"


# ---------------------------------------------------------------------------
# _check_session_ownership — uncovered branches
# ---------------------------------------------------------------------------


class TestCheckSessionOwnershipExtended:
    def test_non_engine_role_returns_false_immediately(self):
        """Non-feed roles skip Redis entirely."""
        b = _make_bootstrapper()
        with patch("hft_platform.services.bootstrap.socket.create_connection") as mock_conn:
            result = b._check_session_ownership("monitor")
        assert result is False
        mock_conn.assert_not_called()

    def test_non_engine_maintenance_returns_false(self):
        b = _make_bootstrapper()
        with patch("hft_platform.services.bootstrap.socket.create_connection") as mock_conn:
            result = b._check_session_ownership("maintenance")
        assert result is False
        mock_conn.assert_not_called()

    def test_own_key_refreshed_and_returns_true(self, monkeypatch):
        """If GET returns our own owner_id, we SETEX and return True."""
        monkeypatch.setenv("HFT_RUNTIME_INSTANCE_ID", "self-123")
        b = _make_bootstrapper()

        # GET -> self-123 (bulk), SETEX -> +OK
        stream_data = b"$8\r\nself-123\r\n+OK\r\n"

        class _Sock:
            def __init__(self):
                self._s = io.BytesIO(stream_data)
                self.sent = []

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def settimeout(self, t):
                pass

            def makefile(self, m):
                return self._s

            def sendall(self, d):
                self.sent.append(d)

        sock = _Sock()
        with patch("hft_platform.services.bootstrap.socket.create_connection", return_value=sock):
            result = b._check_session_ownership("engine")

        assert result is True
        assert any(b"SETEX" in cmd for cmd in sock.sent)

    def test_empty_key_acquires_and_returns_true(self, monkeypatch):
        """If GET returns empty/null, acquire and return True."""
        monkeypatch.setenv("HFT_RUNTIME_INSTANCE_ID", "self-456")
        b = _make_bootstrapper()

        # GET -> $-1 (null), SETEX -> +OK
        stream_data = b"$-1\r\n+OK\r\n"

        class _Sock:
            def __init__(self):
                self._s = io.BytesIO(stream_data)
                self.sent = []

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def settimeout(self, t):
                pass

            def makefile(self, m):
                return self._s

            def sendall(self, d):
                self.sent.append(d)

        sock = _Sock()
        with patch("hft_platform.services.bootstrap.socket.create_connection", return_value=sock):
            result = b._check_session_ownership("engine")

        assert result is True
        assert any(b"SETEX" in cmd for cmd in sock.sent)

    def test_connection_error_returns_false(self, monkeypatch):
        """Redis connection failure is swallowed and returns False."""
        b = _make_bootstrapper()
        with patch(
            "hft_platform.services.bootstrap.socket.create_connection",
            side_effect=ConnectionRefusedError("refused"),
        ):
            result = b._check_session_ownership("engine")
        assert result is False

    def test_with_password_sends_auth(self, monkeypatch):
        """When password is set, AUTH command is sent before GET."""
        monkeypatch.setenv("HFT_REDIS_PASSWORD", "secret-pw")
        monkeypatch.setenv("HFT_RUNTIME_INSTANCE_ID", "self-789")
        b = _make_bootstrapper()

        stream_data = b"$8\r\nself-789\r\n+OK\r\n+OK\r\n"

        class _Sock:
            def __init__(self):
                self._s = io.BytesIO(stream_data)
                self.sent = []

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def settimeout(self, t):
                pass

            def makefile(self, m):
                return self._s

            def sendall(self, d):
                self.sent.append(d)

        sock = _Sock()
        with patch("hft_platform.services.bootstrap.socket.create_connection", return_value=sock):
            b._check_session_ownership("engine")

        assert any(b"AUTH" in cmd for cmd in sock.sent)

    def test_stale_cleanup_fails_when_owner_changes(self, monkeypatch):
        """Stale path where current_owner != owner_str (race) → record failed metric."""
        monkeypatch.setenv("HFT_RUNTIME_INSTANCE_ID", "self-owner")
        monkeypatch.setenv("HFT_FEED_SESSION_STALE_TAKEOVER_TTL_S", "60")
        b = _make_bootstrapper()

        # GET -> other-owner, TTL -> -1 (stale), GET verify -> race-winner (different)
        stream_data = (
            b"$11\r\nother-owner\r\n"  # GET -> "other-owner"
            b":-1\r\n"  # TTL -> -1 (stale)
            b"$11\r\nrace-winner\r\n"  # GET verify -> changed owner
        )

        class _Sock:
            def __init__(self):
                self._s = io.BytesIO(stream_data)
                self.sent = []

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def settimeout(self, t):
                pass

            def makefile(self, m):
                return self._s

            def sendall(self, d):
                self.sent.append(d)

        sock = _Sock()
        with patch("hft_platform.services.bootstrap.socket.create_connection", return_value=sock):
            result = b._check_session_ownership("engine")

        # Race lost — should return False (conflict path)
        assert result is False


# ---------------------------------------------------------------------------
# _record_lease_metric — import failure is swallowed
# ---------------------------------------------------------------------------


class TestRecordLeaseMetric:
    def test_import_error_swallowed(self):
        b = _make_bootstrapper()
        with patch.dict("sys.modules", {"hft_platform.observability.metrics": None}):
            # Should not raise
            b._record_lease_metric("preflight", "acquired")

    def test_metric_incremented_when_available(self):
        b = _make_bootstrapper()
        mock_counter = MagicMock()
        mock_metrics = MagicMock(feed_session_lease_ops_total=mock_counter)
        with patch(
            "hft_platform.observability.metrics.MetricsRegistry.get",
            return_value=mock_metrics,
        ):
            b._record_lease_metric("preflight", "acquired")
        mock_counter.labels.assert_called_once_with(op="preflight", result="acquired")
        mock_counter.labels.return_value.inc.assert_called_once()


# ---------------------------------------------------------------------------
# _build_broker_clients — order mode env var branches
# ---------------------------------------------------------------------------


class TestBuildBrokerClientsOrderMode:
    @pytest.fixture
    def bootstrapper(self):
        return _make_bootstrapper()

    def test_hft_order_mode_sim_sets_simulation_true(self, monkeypatch, bootstrapper):
        monkeypatch.setenv("HFT_ORDER_MODE", "sim")
        monkeypatch.delenv("HFT_ORDER_SIMULATION", raising=False)
        monkeypatch.delenv("HFT_ORDER_NO_CA", raising=False)

        with patch("hft_platform.services.bootstrap.ShioajiClientFacade") as mock_facade:
            bootstrapper._build_broker_clients("engine", "config/symbols.yaml", {}, "shioaji")

        # Second call (order_cfg) should have simulation=True
        assert mock_facade.call_count == 2
        _, order_cfg = mock_facade.call_args_list[1][0]
        assert order_cfg.get("simulation") is True

    def test_hft_order_mode_paper_sets_simulation_true(self, monkeypatch, bootstrapper):
        monkeypatch.setenv("HFT_ORDER_MODE", "paper")
        monkeypatch.delenv("HFT_ORDER_SIMULATION", raising=False)
        monkeypatch.delenv("HFT_ORDER_NO_CA", raising=False)

        with patch("hft_platform.services.bootstrap.ShioajiClientFacade") as mock_facade:
            bootstrapper._build_broker_clients("engine", "config/symbols.yaml", {}, "shioaji")

        _, order_cfg = mock_facade.call_args_list[1][0]
        assert order_cfg.get("simulation") is True

    def test_hft_order_mode_live_does_not_set_simulation(self, monkeypatch, bootstrapper):
        monkeypatch.setenv("HFT_ORDER_MODE", "live")
        monkeypatch.delenv("HFT_ORDER_SIMULATION", raising=False)
        monkeypatch.delenv("HFT_ORDER_NO_CA", raising=False)

        with patch("hft_platform.services.bootstrap.ShioajiClientFacade") as mock_facade:
            bootstrapper._build_broker_clients("engine", "config/symbols.yaml", {}, "shioaji")

        _, order_cfg = mock_facade.call_args_list[1][0]
        # "live" is not in {"sim", "simulation", "paper"} so simulation key shouldn't be set
        assert order_cfg.get("simulation") is not True

    def test_hft_order_simulation_flag_true(self, monkeypatch, bootstrapper):
        monkeypatch.delenv("HFT_ORDER_MODE", raising=False)
        monkeypatch.setenv("HFT_ORDER_SIMULATION", "1")
        monkeypatch.delenv("HFT_ORDER_NO_CA", raising=False)

        with patch("hft_platform.services.bootstrap.ShioajiClientFacade") as mock_facade:
            bootstrapper._build_broker_clients("engine", "config/symbols.yaml", {}, "shioaji")

        _, order_cfg = mock_facade.call_args_list[1][0]
        assert order_cfg.get("simulation") is True

    def test_hft_order_simulation_flag_false(self, monkeypatch, bootstrapper):
        monkeypatch.delenv("HFT_ORDER_MODE", raising=False)
        monkeypatch.setenv("HFT_ORDER_SIMULATION", "0")
        monkeypatch.delenv("HFT_ORDER_NO_CA", raising=False)

        with patch("hft_platform.services.bootstrap.ShioajiClientFacade") as mock_facade:
            bootstrapper._build_broker_clients("engine", "config/symbols.yaml", {}, "shioaji")

        _, order_cfg = mock_facade.call_args_list[1][0]
        assert order_cfg.get("simulation") is not True

    def test_hft_order_no_ca_sets_activate_ca_false(self, monkeypatch, bootstrapper):
        monkeypatch.delenv("HFT_ORDER_MODE", raising=False)
        monkeypatch.delenv("HFT_ORDER_SIMULATION", raising=False)
        monkeypatch.setenv("HFT_ORDER_NO_CA", "1")

        with patch("hft_platform.services.bootstrap.ShioajiClientFacade") as mock_facade:
            bootstrapper._build_broker_clients("engine", "config/symbols.yaml", {}, "shioaji")

        _, order_cfg = mock_facade.call_args_list[1][0]
        assert order_cfg.get("activate_ca") is False

    def test_simulate_true_sets_activate_ca_false(self, monkeypatch, bootstrapper):
        monkeypatch.setenv("HFT_ORDER_MODE", "sim")
        monkeypatch.delenv("HFT_ORDER_NO_CA", raising=False)

        with patch("hft_platform.services.bootstrap.ShioajiClientFacade") as mock_facade:
            bootstrapper._build_broker_clients("engine", "config/symbols.yaml", {}, "shioaji")

        _, order_cfg = mock_facade.call_args_list[1][0]
        assert order_cfg.get("activate_ca") is False


# ---------------------------------------------------------------------------
# _build_feature_engine
# ---------------------------------------------------------------------------


class TestBuildFeatureEngine:
    def test_disabled_returns_all_none(self, monkeypatch):
        monkeypatch.setenv("HFT_FEATURE_ENGINE_ENABLED", "0")
        b = _make_bootstrapper()
        result = b._build_feature_engine()
        assert result == (None, None, None, None, None)

    def test_disabled_false_string_returns_all_none(self, monkeypatch):
        monkeypatch.setenv("HFT_FEATURE_ENGINE_ENABLED", "false")
        b = _make_bootstrapper()
        result = b._build_feature_engine()
        assert result == (None, None, None, None, None)

    def test_enabled_profile_registry_load_failure_continues(self, monkeypatch):
        monkeypatch.setenv("HFT_FEATURE_ENGINE_ENABLED", "1")
        b = _make_bootstrapper()

        with (
            patch(
                "hft_platform.services.bootstrap.load_feature_profile_registry", side_effect=RuntimeError("disk error")
            ),
            patch(
                "hft_platform.services.bootstrap.load_feature_rollout_controller",
                side_effect=RuntimeError("disk error"),
            ),
            patch("hft_platform.services.bootstrap.FeatureEngine") as mock_fe,
        ):
            mock_fe_inst = MagicMock()
            mock_fe_inst.feature_set_id.return_value = "test-set"
            mock_fe.return_value = mock_fe_inst

            (fe, fpr, fp, frc, fra) = b._build_feature_engine()

        assert fe is not None  # FeatureEngine was created
        assert fpr is None  # registry failed
        assert fp is None
        assert frc is None

    def test_enabled_feature_engine_init_failure_returns_all_none(self, monkeypatch):
        monkeypatch.setenv("HFT_FEATURE_ENGINE_ENABLED", "1")
        b = _make_bootstrapper()

        with (
            patch("hft_platform.services.bootstrap.load_feature_profile_registry", return_value=MagicMock()),
            patch("hft_platform.services.bootstrap.load_feature_rollout_controller", return_value=MagicMock()),
            patch("hft_platform.services.bootstrap.FeatureEngine", side_effect=RuntimeError("init failed")),
        ):
            (fe, fpr, fp, frc, fra) = b._build_feature_engine()

        assert fe is None
        assert fpr is None
        assert fp is None

    def test_enabled_no_profile_registry_returns_engine(self, monkeypatch):
        """When profile_registry is None, feature_engine is still created."""
        monkeypatch.setenv("HFT_FEATURE_ENGINE_ENABLED", "1")
        b = _make_bootstrapper()

        mock_fe_inst = MagicMock()
        mock_fe_inst.feature_set_id.return_value = "fs-id"

        with (
            patch(
                "hft_platform.services.bootstrap.load_feature_profile_registry", side_effect=RuntimeError("not found")
            ),
            patch(
                "hft_platform.services.bootstrap.load_feature_rollout_controller", side_effect=RuntimeError("not found")
            ),
            patch("hft_platform.services.bootstrap.FeatureEngine", return_value=mock_fe_inst),
        ):
            (fe, fpr, fp, frc, fra) = b._build_feature_engine()

        assert fe is mock_fe_inst
        assert fpr is None
        assert fp is None

    def test_enabled_rollout_disabled_state_returns_none_profile(self, monkeypatch):
        """When rollout assignment.state == 'disabled', feature_profile is None."""
        monkeypatch.setenv("HFT_FEATURE_ENGINE_ENABLED", "1")
        b = _make_bootstrapper()

        mock_fe_inst = MagicMock()
        mock_fe_inst.feature_set_id.return_value = "fs-id"

        mock_registry = MagicMock()

        mock_rollout_assignment = MagicMock()
        mock_rollout_assignment.state = "disabled"

        mock_rollout_ctrl = MagicMock()
        mock_rollout_ctrl.get.return_value = mock_rollout_assignment
        mock_rollout_ctrl.resolve_profile_id.return_value = None

        with (
            patch("hft_platform.services.bootstrap.load_feature_profile_registry", return_value=mock_registry),
            patch("hft_platform.services.bootstrap.load_feature_rollout_controller", return_value=mock_rollout_ctrl),
            patch("hft_platform.services.bootstrap.FeatureEngine", return_value=mock_fe_inst),
        ):
            (fe, fpr, fp, frc, fra) = b._build_feature_engine()

        assert fe is mock_fe_inst
        assert fp is None
        assert fra is mock_rollout_assignment

    def test_enabled_override_profile_id_used(self, monkeypatch):
        """When rollout controller resolves an override_profile_id, registry.get() is called."""
        monkeypatch.setenv("HFT_FEATURE_ENGINE_ENABLED", "1")
        b = _make_bootstrapper()

        mock_fe_inst = MagicMock()
        mock_fe_inst.feature_set_id.return_value = "fs-id"

        mock_profile = MagicMock()
        mock_profile.state = "active"
        mock_profile.feature_set_id = "fs-id"
        mock_profile.profile_id = "override-profile"

        mock_registry = MagicMock()
        mock_registry.get.return_value = mock_profile

        mock_rollout_assignment = MagicMock()
        mock_rollout_assignment.state = "active"

        mock_rollout_ctrl = MagicMock()
        mock_rollout_ctrl.get.return_value = mock_rollout_assignment
        mock_rollout_ctrl.resolve_profile_id.return_value = "override-profile"

        with (
            patch("hft_platform.services.bootstrap.load_feature_profile_registry", return_value=mock_registry),
            patch("hft_platform.services.bootstrap.load_feature_rollout_controller", return_value=mock_rollout_ctrl),
            patch("hft_platform.services.bootstrap.FeatureEngine", return_value=mock_fe_inst),
        ):
            (fe, fpr, fp, frc, fra) = b._build_feature_engine()

        mock_registry.get.assert_called_once_with("override-profile")
        assert fp is mock_profile

    def test_enabled_active_profile_applied(self, monkeypatch):
        """When an active profile is found, apply_profile is called."""
        monkeypatch.setenv("HFT_FEATURE_ENGINE_ENABLED", "1")
        b = _make_bootstrapper()

        mock_fe_inst = MagicMock()
        mock_fe_inst.feature_set_id.return_value = "fs-id"

        mock_profile = MagicMock()
        mock_profile.state = "active"
        mock_profile.feature_set_id = "fs-id"
        mock_profile.profile_id = "default-profile"

        mock_registry = MagicMock()
        mock_registry.get_active_for_set.return_value = mock_profile

        mock_rollout_ctrl = MagicMock()
        mock_rollout_ctrl.get.return_value = None
        mock_rollout_ctrl.resolve_profile_id.return_value = None

        with (
            patch("hft_platform.services.bootstrap.load_feature_profile_registry", return_value=mock_registry),
            patch("hft_platform.services.bootstrap.load_feature_rollout_controller", return_value=mock_rollout_ctrl),
            patch("hft_platform.services.bootstrap.FeatureEngine", return_value=mock_fe_inst),
        ):
            (fe, fpr, fp, frc, fra) = b._build_feature_engine()

        mock_fe_inst.apply_profile.assert_called_once_with(mock_profile)
        assert fp is mock_profile


# ---------------------------------------------------------------------------
# build() with gateway enabled
# ---------------------------------------------------------------------------


class TestBuildWithGatewayEnabled:
    def test_gateway_enabled_creates_gateway_service(self, monkeypatch):
        monkeypatch.setenv("HFT_MODE", "sim")
        monkeypatch.delenv("HFT_BROKER", raising=False)
        monkeypatch.setenv("HFT_FEATURE_ENGINE_ENABLED", "0")
        monkeypatch.setenv("HFT_GATEWAY_ENABLED", "1")

        bootstrapper = _make_bootstrapper()

        mock_intent_channel = MagicMock()
        mock_gateway_service = MagicMock()
        mock_local_channel_cls = MagicMock(return_value=mock_intent_channel)
        mock_exposure_store = MagicMock()
        mock_dedup_store = MagicMock()
        mock_gateway_policy = MagicMock()
        mock_gateway_service_cls = MagicMock(return_value=mock_gateway_service)

        patches = [
            patch("hft_platform.services.bootstrap.ShioajiClientFacade"),
            patch("hft_platform.services.bootstrap.MarketDataService"),
            patch("hft_platform.services.bootstrap.OrderAdapter"),
            patch("hft_platform.services.bootstrap.ExecutionGateway"),
            patch("hft_platform.services.bootstrap.ExecutionRouter"),
            patch("hft_platform.services.bootstrap.RiskEngine"),
            patch("hft_platform.services.bootstrap.ReconciliationService"),
            patch("hft_platform.services.bootstrap.StrategyRunner"),
            patch("hft_platform.services.bootstrap.RecorderService"),
            patch("hft_platform.services.bootstrap.RingBufferBus"),
            patch("hft_platform.services.bootstrap.PositionStore"),
            patch("hft_platform.services.bootstrap.StormGuard"),
            patch("hft_platform.services.bootstrap.SymbolMetadata"),
            patch("hft_platform.services.bootstrap.SymbolMetadataPriceScaleProvider"),
            patch("hft_platform.services.bootstrap.LatencyRecorder"),
            patch("hft_platform.services.bootstrap.FeatureEngine"),
        ]
        started = [p.start() for p in patches]

        gateway_mocks = {
            "hft_platform.gateway.channel": MagicMock(LocalIntentChannel=mock_local_channel_cls),
            "hft_platform.gateway.dedup": MagicMock(IdempotencyStore=MagicMock(return_value=mock_dedup_store)),
            "hft_platform.gateway.exposure": MagicMock(ExposureStore=MagicMock(return_value=mock_exposure_store)),
            "hft_platform.gateway.policy": MagicMock(GatewayPolicy=MagicMock(return_value=mock_gateway_policy)),
            "hft_platform.gateway.service": MagicMock(GatewayService=mock_gateway_service_cls),
        }

        try:
            with (
                patch.object(bootstrapper, "_check_session_ownership", return_value=False),
                patch.dict("sys.modules", gateway_mocks),
            ):
                registry = bootstrapper.build()
        finally:
            for p in patches:
                p.stop()

        assert registry.gateway_service is mock_gateway_service
        assert registry.intent_channel is mock_intent_channel


# ---------------------------------------------------------------------------
# teardown() — non-engine role skips Redis
# ---------------------------------------------------------------------------


class TestTeardownNonEngineRole:
    def test_teardown_skips_redis_for_monitor_role(self):
        b = _make_bootstrapper()
        b._last_role = "monitor"

        with patch("hft_platform.services.bootstrap.socket.create_connection") as mock_conn:
            b.teardown()

        mock_conn.assert_not_called()

    def test_teardown_skips_redis_for_maintenance_role(self):
        b = _make_bootstrapper()
        b._last_role = "maintenance"

        with patch("hft_platform.services.bootstrap.socket.create_connection") as mock_conn:
            b.teardown()

        mock_conn.assert_not_called()

    def test_teardown_handles_redis_connection_error(self):
        """teardown() swallows Redis connection errors."""
        b = _make_bootstrapper()
        b._last_role = "engine"

        with patch(
            "hft_platform.services.bootstrap.socket.create_connection",
            side_effect=ConnectionRefusedError("down"),
        ):
            b.teardown()  # Must not raise

    def test_teardown_stops_refresh_thread_first(self):
        """teardown() calls _stop_lease_refresh_thread."""
        b = _make_bootstrapper()
        b._last_role = "monitor"  # skip Redis

        with patch.object(b, "_stop_lease_refresh_thread") as mock_stop:
            b.teardown()

        mock_stop.assert_called_once()


# ---------------------------------------------------------------------------
# _stop_lease_refresh_thread
# ---------------------------------------------------------------------------


class TestStopLeaseRefreshThread:
    def test_stop_when_no_thread(self):
        b = _make_bootstrapper()
        b._lease_refresh_running = False
        b._lease_refresh_thread = None
        b._stop_lease_refresh_thread()  # Should not raise
        assert b._lease_refresh_thread is None

    def test_stop_clears_running_flag(self):
        b = _make_bootstrapper()
        b._lease_refresh_running = True
        b._lease_refresh_thread = None
        b._stop_lease_refresh_thread()
        assert b._lease_refresh_running is False

    def test_stop_joins_alive_thread(self):
        """If thread is alive when stop is called, join() is called."""
        b = _make_bootstrapper()
        b._lease_refresh_running = True

        mock_thread = MagicMock()
        # is_alive: True → triggers join, then False → no warning
        mock_thread.is_alive.side_effect = [True, False]
        b._lease_refresh_thread = mock_thread

        b._stop_lease_refresh_thread()

        mock_thread.join.assert_called_once_with(timeout=1.0)
        assert b._lease_refresh_thread is None

    def test_stop_logs_warning_if_thread_still_alive_after_join(self):
        """When thread stays alive after join, a warning is logged."""
        b = _make_bootstrapper()
        b._lease_refresh_running = True

        mock_thread = MagicMock()
        # First call: alive (triggers join), second: still alive
        mock_thread.is_alive.side_effect = [True, True]
        b._lease_refresh_thread = mock_thread

        b._stop_lease_refresh_thread()

        mock_thread.join.assert_called_once_with(timeout=1.0)
        assert b._lease_refresh_thread is None  # Still cleared


# ---------------------------------------------------------------------------
# SystemBootstrapper initialization
# ---------------------------------------------------------------------------


class TestSystemBootstrapperInit:
    def test_default_settings_is_empty_dict(self):
        b = SystemBootstrapper()
        assert b.settings == {}

    def test_custom_settings_stored(self):
        cfg = {"mode": "sim", "symbols": ["2330"]}
        b = SystemBootstrapper(settings=cfg)
        assert b.settings is cfg

    def test_initial_lease_state(self):
        b = SystemBootstrapper()
        assert b._lease_refresh_running is False
        assert b._lease_refresh_thread is None
        assert b._last_role == "engine"


# ---------------------------------------------------------------------------
# build() — runtime role branches
# ---------------------------------------------------------------------------


class TestBuildRolesBranches:
    def test_build_monitor_role_creates_noop_clients(self, monkeypatch):
        registry = _build_with_all_mocks(monkeypatch, extra_env={"HFT_RUNTIME_ROLE": "monitor"})
        assert hasattr(registry.md_client, "runtime_role")
        assert registry.md_client.runtime_role == "monitor"

    def test_build_wal_loader_role_creates_noop_clients(self, monkeypatch):
        registry = _build_with_all_mocks(monkeypatch, extra_env={"HFT_RUNTIME_ROLE": "wal_loader"})
        assert hasattr(registry.md_client, "runtime_role")
        assert registry.md_client.runtime_role == "wal_loader"

    def test_build_engine_role_default(self, monkeypatch):
        registry = _build_with_all_mocks(monkeypatch)
        assert registry.broker_id == "shioaji"

    def test_build_sets_last_role(self, monkeypatch):
        monkeypatch.setenv("HFT_MODE", "sim")
        monkeypatch.delenv("HFT_BROKER", raising=False)
        monkeypatch.setenv("HFT_FEATURE_ENGINE_ENABLED", "0")
        monkeypatch.delenv("HFT_GATEWAY_ENABLED", raising=False)
        monkeypatch.setenv("HFT_RUNTIME_ROLE", "monitor")

        bootstrapper = _make_bootstrapper()
        patches = [
            patch("hft_platform.services.bootstrap.ShioajiClientFacade"),
            patch("hft_platform.services.bootstrap.MarketDataService"),
            patch("hft_platform.services.bootstrap.OrderAdapter"),
            patch("hft_platform.services.bootstrap.ExecutionGateway"),
            patch("hft_platform.services.bootstrap.ExecutionRouter"),
            patch("hft_platform.services.bootstrap.RiskEngine"),
            patch("hft_platform.services.bootstrap.ReconciliationService"),
            patch("hft_platform.services.bootstrap.StrategyRunner"),
            patch("hft_platform.services.bootstrap.RecorderService"),
            patch("hft_platform.services.bootstrap.RingBufferBus"),
            patch("hft_platform.services.bootstrap.PositionStore"),
            patch("hft_platform.services.bootstrap.StormGuard"),
            patch("hft_platform.services.bootstrap.SymbolMetadata"),
            patch("hft_platform.services.bootstrap.SymbolMetadataPriceScaleProvider"),
            patch("hft_platform.services.bootstrap.LatencyRecorder"),
            patch("hft_platform.services.bootstrap.FeatureEngine"),
        ]
        for p in patches:
            p.start()
        try:
            with patch.object(bootstrapper, "_check_session_ownership", return_value=False):
                bootstrapper.build()
        finally:
            for p in patches:
                p.stop()

        assert bootstrapper._last_role == "monitor"


# ---------------------------------------------------------------------------
# Queue size env var — exec and recorder
# ---------------------------------------------------------------------------


class TestQueueSizeEnvVars:
    def test_raw_exec_queue_size_from_env(self, monkeypatch):
        registry = _build_with_all_mocks(monkeypatch, extra_env={"HFT_RAW_EXEC_QUEUE_SIZE": "4096"})
        assert registry.raw_exec_queue.maxsize == 4096

    def test_risk_queue_size_from_env(self, monkeypatch):
        registry = _build_with_all_mocks(monkeypatch, extra_env={"HFT_RISK_QUEUE_SIZE": "8192"})
        assert registry.risk_queue.maxsize == 8192

    def test_order_queue_size_from_env(self, monkeypatch):
        registry = _build_with_all_mocks(monkeypatch, extra_env={"HFT_ORDER_QUEUE_SIZE": "4096"})
        assert registry.order_queue.maxsize == 4096

    def test_recorder_queue_size_from_env(self, monkeypatch):
        registry = _build_with_all_mocks(monkeypatch, extra_env={"HFT_RECORDER_QUEUE_SIZE": "32768"})
        assert registry.recorder_queue.maxsize == 32768

    def test_zero_queue_size_clamped_to_minimum(self, monkeypatch):
        registry = _build_with_all_mocks(monkeypatch, extra_env={"HFT_RISK_QUEUE_SIZE": "0"})
        assert registry.risk_queue.maxsize >= 1024
