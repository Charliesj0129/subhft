"""Tests for SystemBootstrapper.build() — service graph, queue wiring, broker injection."""

from __future__ import annotations

import asyncio
from unittest.mock import MagicMock, patch

import pytest

from hft_platform.services.bootstrap import SystemBootstrapper

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def _sim_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Set minimal env for sim mode bootstrap."""
    monkeypatch.setenv("HFT_MODE", "sim")
    monkeypatch.delenv("HFT_BROKER", raising=False)
    monkeypatch.delenv("HFT_GATEWAY_ENABLED", raising=False)
    monkeypatch.delenv("HFT_FEATURE_ENGINE_ENABLED", raising=False)
    monkeypatch.setenv("HFT_FEATURE_ENGINE_ENABLED", "0")


@pytest.fixture()
def _mock_services():
    """Patch heavy service constructors so build() never touches real infra."""
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
    mocks = {}
    started = []
    for p in patches:
        m = p.start()
        started.append(p)
        # Extract short name from target, e.g. "hft_platform.services.bootstrap.RingBufferBus" -> "RingBufferBus"
        name = p.attribute
        mocks[name] = m
    yield mocks
    for p in started:
        p.stop()


def _build_with_mocks(
    settings: dict | None = None,
    monkeypatch: pytest.MonkeyPatch | None = None,
) -> "ServiceRegistry":  # noqa: F821
    """Build a ServiceRegistry with all external deps mocked."""
    bootstrapper = SystemBootstrapper(settings if settings is not None else {})
    # Bypass Redis session ownership check
    with patch.object(bootstrapper, "_check_session_ownership", return_value=False):
        return bootstrapper.build()


# ---------------------------------------------------------------------------
# Build flow — service graph completeness
# ---------------------------------------------------------------------------


class TestBuildServiceGraph:
    """Verify build() produces a ServiceRegistry with all expected services."""

    @pytest.mark.usefixtures("_sim_env", "_mock_services")
    def test_build_sim_returns_service_registry(self) -> None:
        registry = _build_with_mocks()
        from hft_platform.services.registry import ServiceRegistry

        assert isinstance(registry, ServiceRegistry)

    @pytest.mark.usefixtures("_sim_env", "_mock_services")
    def test_build_runs_order_mode_safety_guard(self) -> None:
        with patch("hft_platform.services.bootstrap.validate_order_mode_safety") as guard:
            _build_with_mocks()

        guard.assert_called_once_with()

    @pytest.mark.usefixtures("_sim_env", "_mock_services")
    def test_build_sim_has_all_required_services(self) -> None:
        registry = _build_with_mocks()
        required_attrs = [
            "bus",
            "raw_queue",
            "raw_exec_queue",
            "risk_queue",
            "order_queue",
            "recorder_queue",
            "position_store",
            "order_id_map",
            "storm_guard",
            "symbol_metadata",
            "price_scale_provider",
            "broker_id",
            "md_client",
            "order_client",
            "md_service",
            "order_adapter",
            "execution_gateway",
            "exec_service",
            "risk_engine",
            "recon_service",
            "strategy_runner",
            "recorder",
        ]
        for attr in required_attrs:
            assert hasattr(registry, attr), f"ServiceRegistry missing attribute: {attr}"
            assert getattr(registry, attr) is not None, f"ServiceRegistry.{attr} is None"

    @pytest.mark.usefixtures("_sim_env", "_mock_services")
    def test_build_sim_default_broker_id_is_shioaji(self) -> None:
        registry = _build_with_mocks()
        assert registry.broker_id == "shioaji"

    @pytest.mark.usefixtures("_sim_env", "_mock_services")
    def test_build_wires_platform_degrade_thresholds_from_env(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("HFT_PLATFORM_REDUCE_ONLY_FEED_GAP_S", "15")
        monkeypatch.setenv("HFT_PLATFORM_REDUCE_ONLY_RECONNECT_PENDING_S", "25")
        monkeypatch.setenv("HFT_PLATFORM_REDUCE_ONLY_RECONNECT_FLAP_BUDGET", "9")
        monkeypatch.setenv("HFT_PLATFORM_REDUCE_ONLY_QUEUE_DEPTH", "777")
        monkeypatch.setenv("HFT_PLATFORM_REDUCE_ONLY_RSS_MB", "321")
        monkeypatch.setenv("HFT_PLATFORM_REDUCE_ONLY_WAL_BACKLOG_FILES", "45")

        registry = _build_with_mocks()
        inputs = registry.platform_degrade_inputs

        assert inputs is not None
        assert inputs.feed_gap_threshold_s == 15.0
        assert inputs.reconnect_pending_threshold_s == 25.0
        assert inputs.reconnect_flap_budget == 9
        assert inputs.queue_depth_threshold == 777
        assert inputs.rss_threshold_mb == 321
        assert inputs.wal_backlog_files_threshold == 45

    @pytest.mark.usefixtures("_sim_env", "_mock_services")
    def test_build_sim_gateway_disabled_by_default(self) -> None:
        registry = _build_with_mocks()
        assert registry.gateway_service is None
        assert registry.intent_channel is None

    @pytest.mark.usefixtures("_sim_env", "_mock_services")
    def test_build_sets_runtime_role_in_settings(self) -> None:
        settings: dict = {}
        registry = _build_with_mocks(settings=settings)
        assert settings.get("runtime_role") == "engine"


# ---------------------------------------------------------------------------
# Queue wiring
# ---------------------------------------------------------------------------


class TestQueueWiring:
    """Verify queues are correctly wired to services."""

    @pytest.mark.usefixtures("_sim_env", "_mock_services")
    def test_all_queues_are_bounded(self) -> None:
        registry = _build_with_mocks()
        for name in ("raw_queue", "raw_exec_queue", "risk_queue", "order_queue", "recorder_queue"):
            q = getattr(registry, name)
            assert isinstance(q, asyncio.Queue), f"{name} is not an asyncio.Queue"
            assert q.maxsize > 0, f"{name} is unbounded (maxsize={q.maxsize})"

    @pytest.mark.usefixtures("_sim_env", "_mock_services")
    def test_queue_minimum_size_enforced(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Setting queue size below minimum (1024) must be clamped."""
        monkeypatch.setenv("HFT_RAW_QUEUE_SIZE", "100")
        registry = _build_with_mocks()
        assert registry.raw_queue.maxsize >= 1024

    @pytest.mark.usefixtures("_sim_env", "_mock_services")
    def test_queue_custom_size_from_env(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("HFT_RAW_QUEUE_SIZE", "131072")
        registry = _build_with_mocks()
        assert registry.raw_queue.maxsize == 131072

    @pytest.mark.usefixtures("_sim_env")
    def test_raw_queue_passed_to_market_data_service(self, _mock_services: dict) -> None:
        registry = _build_with_mocks()
        md_cls = _mock_services["MarketDataService"]
        # MarketDataService(bus, raw_queue, md_client, ...) — raw_queue is 2nd positional arg
        call_args = md_cls.call_args
        assert call_args is not None
        assert call_args[0][1] is registry.raw_queue

    @pytest.mark.usefixtures("_sim_env")
    def test_risk_queue_passed_to_risk_engine(self, _mock_services: dict) -> None:
        registry = _build_with_mocks()
        risk_cls = _mock_services["RiskEngine"]
        call_args = risk_cls.call_args
        assert call_args is not None
        # RiskEngine(risk_path, risk_queue, order_queue, price_scale_provider)
        assert call_args[0][1] is registry.risk_queue

    @pytest.mark.usefixtures("_sim_env")
    def test_order_queue_passed_to_risk_engine_and_order_adapter(self, _mock_services: dict) -> None:
        registry = _build_with_mocks()
        risk_cls = _mock_services["RiskEngine"]
        order_cls = _mock_services["OrderAdapter"]
        # RiskEngine 3rd positional = order_queue
        assert risk_cls.call_args[0][2] is registry.order_queue
        # OrderAdapter(adapter_path, order_queue, order_client, order_id_map)
        assert order_cls.call_args[0][1] is registry.order_queue

    @pytest.mark.usefixtures("_sim_env")
    def test_recorder_queue_passed_to_recorder_service(self, _mock_services: dict) -> None:
        registry = _build_with_mocks()
        rec_cls = _mock_services["RecorderService"]
        assert rec_cls.call_args[0][0] is registry.recorder_queue


# ---------------------------------------------------------------------------
# Broker client injection
# ---------------------------------------------------------------------------


class TestBrokerInjection:
    """Verify broker client selection based on HFT_BROKER env var."""

    @pytest.mark.usefixtures("_mock_services")
    def test_shioaji_broker_creates_shioaji_facade(self, monkeypatch: pytest.MonkeyPatch, _mock_services: dict) -> None:
        monkeypatch.setenv("HFT_MODE", "sim")
        monkeypatch.setenv("HFT_BROKER", "shioaji")
        monkeypatch.setenv("HFT_FEATURE_ENGINE_ENABLED", "0")
        registry = _build_with_mocks()
        facade_cls = _mock_services["ShioajiClientFacade"]
        # engine role: ShioajiClientFacade called twice (md + order)
        assert facade_cls.call_count == 2
        assert registry.broker_id == "shioaji"

    @pytest.mark.usefixtures("_mock_services")
    def test_fubon_broker_creates_fubon_facade(self, monkeypatch: pytest.MonkeyPatch, _mock_services: dict) -> None:
        monkeypatch.setenv("HFT_MODE", "sim")
        monkeypatch.setenv("HFT_BROKER", "fubon")
        monkeypatch.setenv("HFT_FEATURE_ENGINE_ENABLED", "0")

        mock_fubon_facade_cls = MagicMock()
        mock_fubon_facade_cls.return_value = MagicMock()
        mock_fubon_order_codec_cls = MagicMock()
        fake_codec_module = MagicMock()
        fake_codec_module.FubonOrderCodec = mock_fubon_order_codec_cls

        fake_module = MagicMock()
        fake_module.FubonClientFacade = mock_fubon_facade_cls

        with patch.dict(
            "sys.modules",
            {
                "hft_platform.feed_adapter.fubon": MagicMock(),
                "hft_platform.feed_adapter.fubon.facade": fake_module,
                "hft_platform.feed_adapter.fubon.order_codec": fake_codec_module,
            },
        ):
            registry = _build_with_mocks()

        assert registry.broker_id == "fubon"
        assert mock_fubon_facade_cls.call_count == 2
        assert mock_fubon_order_codec_cls.call_count == 1

    @pytest.mark.usefixtures("_mock_services")
    def test_invalid_broker_raises_value_error(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("HFT_MODE", "sim")
        monkeypatch.setenv("HFT_BROKER", "unknown_broker")
        monkeypatch.setenv("HFT_FEATURE_ENGINE_ENABLED", "0")
        with pytest.raises(ValueError, match="Unknown HFT_BROKER"):
            _build_with_mocks()

    @pytest.mark.usefixtures("_mock_services")
    def test_maintenance_role_gets_noop_clients(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("HFT_MODE", "sim")
        monkeypatch.setenv("HFT_RUNTIME_ROLE", "maintenance")
        monkeypatch.setenv("HFT_FEATURE_ENGINE_ENABLED", "0")
        monkeypatch.delenv("HFT_BROKER", raising=False)
        registry = _build_with_mocks()
        assert hasattr(registry.md_client, "runtime_role")
        assert registry.md_client.runtime_role == "maintenance"
        assert registry.md_client.login() is False


# ---------------------------------------------------------------------------
# Health server lifecycle
# ---------------------------------------------------------------------------


class TestHealthServerLifecycle:
    """Verify HealthServer start/stop via HFTSystem (lightweight check)."""

    def test_health_server_instantiation(self) -> None:
        """HealthServer can be instantiated with system=None."""
        from hft_platform.observability.health import HealthServer

        hs = HealthServer(system=None)
        assert hs is not None

    def test_health_server_stop_before_start(self) -> None:
        """Calling stop() before run() must not raise."""
        from hft_platform.observability.health import HealthServer

        hs = HealthServer(system=None)
        hs.stop()  # Should not raise
        assert hs._server is None


# ---------------------------------------------------------------------------
# Error cases
# ---------------------------------------------------------------------------


class TestBuildErrorCases:
    """Error paths during build()."""

    @pytest.mark.usefixtures("_sim_env", "_mock_services")
    def test_build_with_broker_sdk_import_failure(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """When fubon facade import fails, build() propagates ImportError."""
        monkeypatch.setenv("HFT_BROKER", "fubon")

        bootstrapper = SystemBootstrapper({})
        with (
            patch.object(bootstrapper, "_check_session_ownership", return_value=False),
            patch.dict("sys.modules", {"hft_platform.feed_adapter.fubon.facade": None}),
        ):
            with pytest.raises((ImportError, ModuleNotFoundError)):
                bootstrapper.build()

    @pytest.mark.usefixtures("_sim_env", "_mock_services")
    def test_build_empty_broker_env_raises(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Empty HFT_BROKER string raises ValueError."""
        monkeypatch.setenv("HFT_BROKER", "")
        with pytest.raises(ValueError, match="Unknown HFT_BROKER"):
            _build_with_mocks()


# ---------------------------------------------------------------------------
# Config loading chain verification
# ---------------------------------------------------------------------------


class TestConfigLoadingChain:
    """Verify config priority chain: base YAML -> env YAML -> settings.py -> env vars -> CLI."""

    def test_load_settings_returns_tuple(self) -> None:
        from hft_platform.config.loader import load_settings

        result = load_settings(cli_overrides={"skip_config_validation": True})
        assert isinstance(result, tuple)
        assert len(result) == 2
        settings, defaults = result
        assert isinstance(settings, dict)
        assert isinstance(defaults, dict)

    def test_env_var_overrides_base_mode(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from hft_platform.config.loader import load_settings

        monkeypatch.setenv("HFT_MODE", "live")
        settings, _ = load_settings(cli_overrides={"skip_config_validation": True})
        assert settings.get("mode") == "live"

    def test_cli_overrides_take_highest_priority(self) -> None:
        from hft_platform.config.loader import load_settings

        settings, _ = load_settings(
            cli_overrides={
                "symbols": ["9999"],
                "skip_config_validation": True,
            }
        )
        assert settings["symbols"] == ["9999"]

    def test_default_settings_used_when_no_yaml(self, monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
        """When base YAML is missing, DEFAULT_SETTINGS provides fallback."""
        from hft_platform.config import loader
        from hft_platform.config.loader import load_settings

        monkeypatch.setattr(loader, "DEFAULT_YAML_PATH", str(tmp_path / "nonexistent.yaml"))
        settings, _ = load_settings(cli_overrides={"skip_config_validation": True})
        assert "mode" in settings
        assert "symbols" in settings

    def test_hft_symbols_env_splits_correctly(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from hft_platform.config.loader import _env_overrides

        monkeypatch.setenv("HFT_SYMBOLS", "2330,2317,2454")
        overrides = _env_overrides()
        assert overrides["symbols"] == ["2330", "2317", "2454"]


# ---------------------------------------------------------------------------
# Queue default sizes
# ---------------------------------------------------------------------------


class TestQueueDefaults:
    """Verify default queue size constants."""

    def test_default_raw_queue_size(self) -> None:
        assert SystemBootstrapper.DEFAULT_RAW_QUEUE_SIZE == 65536

    def test_default_risk_queue_size(self) -> None:
        assert SystemBootstrapper.DEFAULT_RISK_QUEUE_SIZE == 4096

    def test_default_order_queue_size(self) -> None:
        assert SystemBootstrapper.DEFAULT_ORDER_QUEUE_SIZE == 2048

    def test_default_recorder_queue_size(self) -> None:
        assert SystemBootstrapper.DEFAULT_RECORDER_QUEUE_SIZE == 16384

    def test_default_raw_exec_queue_size(self) -> None:
        assert SystemBootstrapper.DEFAULT_RAW_EXEC_QUEUE_SIZE == 8192
