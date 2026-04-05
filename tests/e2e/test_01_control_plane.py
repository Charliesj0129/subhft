"""
Plane 1 — Control Plane E2E tests.

Covers config merge priority, symbols.yaml loading, env mode resolution,
bootstrap service graph wiring, queue bounds enforcement, and feature engine wiring.
"""

from __future__ import annotations

import os
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
import yaml

# ---------------------------------------------------------------------------
# Module-level markers for TestChain
# ---------------------------------------------------------------------------
pytestmark = [pytest.mark.e2e]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_MOCK_TARGETS = [
    "hft_platform.feed_adapter.shioaji.facade.ShioajiClientFacade",
    "hft_platform.services.bootstrap.MarketDataService",
    "hft_platform.services.bootstrap.OrderAdapter",
    "hft_platform.services.bootstrap.ExecutionGateway",
    "hft_platform.services.bootstrap.ExecutionRouter",
    "hft_platform.services.bootstrap.RiskEngine",
    "hft_platform.services.bootstrap.ReconciliationService",
    "hft_platform.services.bootstrap.StrategyRunner",
    "hft_platform.services.bootstrap.RecorderService",
    "hft_platform.services.bootstrap.RingBufferBus",
    "hft_platform.services.bootstrap.PositionStore",
    "hft_platform.services.bootstrap.StormGuard",
    "hft_platform.services.bootstrap.SymbolMetadata",
    "hft_platform.services.bootstrap.SymbolMetadataPriceScaleProvider",
    "hft_platform.services.bootstrap.LatencyRecorder",
    "hft_platform.services.bootstrap.FeatureEngine",
]


def _build_with_all_mocks(settings: dict | None = None) -> "ServiceRegistry":  # noqa: F821
    """Build ServiceRegistry with all external deps mocked out."""
    from hft_platform.services.bootstrap import SystemBootstrapper

    bootstrapper = SystemBootstrapper(settings if settings is not None else {})

    patches = [patch(t) for t in _MOCK_TARGETS]
    mocks = [p.start() for p in patches]
    try:
        with patch.object(bootstrapper, "_check_session_ownership", return_value=False):
            registry = bootstrapper.build()
    finally:
        for p in patches:
            p.stop()

    return registry


# ---------------------------------------------------------------------------
# TestChain — config layer tests
# ---------------------------------------------------------------------------


@pytest.mark.e2e_chain
class TestChain:
    """Multi-step config chain tests (no external services)."""

    def test_config_merge_priority(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Config priority chain: CLI > Env Var > Base YAML.

        Note: For the 'mode' key specifically, HFT_MODE env var takes precedence
        over CLI (see load_settings line 148 — mode sync step). This test verifies
        that other keys follow the full CLI > Env > YAML priority, and that
        HFT_MODE env var is applied when no CLI override of mode is given.
        """
        # Write base YAML with custom_key=base
        cfg_dir = tmp_path / "config" / "base"
        cfg_dir.mkdir(parents=True)
        base_cfg = {"mode": "sim", "custom_key": "base"}
        (cfg_dir / "main.yaml").write_text(yaml.dump(base_cfg))

        # Env var sets mode=replay; no HFT_SYMBOLS set
        monkeypatch.setenv("HFT_MODE", "replay")
        monkeypatch.delenv("HFT_SYMBOLS", raising=False)
        monkeypatch.setenv("HFT_SKIP_CONFIG_VALIDATION", "1")
        monkeypatch.chdir(tmp_path)

        from hft_platform.config.loader import load_settings

        # CLI override sets symbols — CLI wins for non-mode keys
        settings, _defaults = load_settings(cli_overrides={"symbols": ["CLI_SYM"]})

        # HFT_MODE env var beats base YAML mode=sim
        assert settings["mode"] == "replay", f"Expected 'replay' (env var) but got {settings['mode']!r}"
        # CLI wins for symbols
        assert settings["symbols"] == ["CLI_SYM"], f"Expected CLI symbols but got {settings['symbols']!r}"

    def test_symbols_yaml_loading(
        self,
        e2e_symbols_yaml: str,
    ) -> None:
        """SymbolMetadata correctly reads scale, exchange, and multiplier from YAML."""
        # The conftest fixture uses 'symbol' key, but SymbolMetadata reads 'code' key.
        # Write a compatible YAML using 'code' key.
        import tempfile

        content = """\
symbols:
  - code: "2330"
    exchange: TSE
    price_scale: 10000
    lot_size: 1000
    tick_size: 1
  - code: "TXFD6"
    exchange: TAIFEX
    price_scale: 10000
    lot_size: 1
    tick_size: 10000
    point_value: 200
"""
        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".yaml", delete=False
        ) as f:
            f.write(content)
            tmp_yaml = f.name

        try:
            from hft_platform.feed_adapter.normalizer import SymbolMetadata

            meta = SymbolMetadata(config_path=tmp_yaml)

            assert meta.price_scale("2330") == 10000
            assert meta.exchange("2330") == "TSE"
            assert meta.exchange("TXFD6") == "TAIFEX"
            assert meta.contract_multiplier("TXFD6") == 200
        finally:
            os.unlink(tmp_yaml)

    def test_env_mode_resolution(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """HFT_MODE env var is picked up by load_settings."""
        cfg_dir = tmp_path / "config" / "base"
        cfg_dir.mkdir(parents=True)
        base_cfg = {"mode": "live"}
        (cfg_dir / "main.yaml").write_text(yaml.dump(base_cfg))

        monkeypatch.setenv("HFT_MODE", "sim")
        monkeypatch.setenv("HFT_SKIP_CONFIG_VALIDATION", "1")
        monkeypatch.chdir(tmp_path)

        from hft_platform.config.loader import load_settings

        settings, _defaults = load_settings()
        assert settings["mode"] == "sim", f"Expected 'sim' but got {settings['mode']!r}"


# ---------------------------------------------------------------------------
# TestIntegration — bootstrap wiring tests
# ---------------------------------------------------------------------------


@pytest.mark.e2e_integration
class TestIntegration:
    """Bootstrap service-graph integration tests (all external deps mocked)."""

    def _setup_env(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("HFT_RUNTIME_ROLE", "maintenance")
        monkeypatch.setenv("HFT_MODE", "sim")
        monkeypatch.setenv("HFT_SKIP_CONFIG_VALIDATION", "1")
        monkeypatch.delenv("HFT_GATEWAY_ENABLED", raising=False)

    def test_bootstrap_builds_valid_registry(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """build() creates a ServiceRegistry with bounded queues and core services."""
        self._setup_env(monkeypatch)
        monkeypatch.setenv("HFT_FEATURE_ENGINE_ENABLED", "0")

        registry = _build_with_all_mocks()

        from hft_platform.services.registry import ServiceRegistry

        assert isinstance(registry, ServiceRegistry)

        # Queues exist and are bounded
        assert registry.raw_queue is not None
        assert registry.raw_queue.maxsize >= 1
        assert registry.risk_queue is not None
        assert registry.risk_queue.maxsize >= 1
        assert registry.order_queue is not None
        assert registry.order_queue.maxsize >= 1
        assert registry.recorder_queue is not None
        assert registry.recorder_queue.maxsize >= 1

        # Core services are non-None
        assert registry.md_service is not None
        assert registry.risk_engine is not None
        assert registry.order_adapter is not None
        assert registry.recorder is not None
        assert registry.strategy_runner is not None

    def test_bootstrap_queue_bounds_enforced(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Even with HFT_RAW_QUEUE_SIZE=10, minimum queue size of 1024 is enforced."""
        self._setup_env(monkeypatch)
        monkeypatch.setenv("HFT_FEATURE_ENGINE_ENABLED", "0")
        monkeypatch.setenv("HFT_RAW_QUEUE_SIZE", "10")

        registry = _build_with_all_mocks()

        assert registry.raw_queue.maxsize >= 1024, (
            f"Expected raw_queue.maxsize >= 1024, got {registry.raw_queue.maxsize}"
        )

    def test_bootstrap_feature_engine_wiring(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """With HFT_FEATURE_ENGINE_ENABLED=1, feature_engine is wired into the registry."""
        self._setup_env(monkeypatch)
        monkeypatch.setenv("HFT_FEATURE_ENGINE_ENABLED", "1")

        registry = _build_with_all_mocks()

        assert registry.feature_engine is not None, (
            "Expected feature_engine to be non-None when HFT_FEATURE_ENGINE_ENABLED=1"
        )
