"""Unit 8: Tests for FeatureEngine default-on rollout.

Verifies:
- Default (no env var) → FeatureEngine is enabled
- Explicit HFT_FEATURE_ENGINE_ENABLED=0 → FeatureEngine is disabled
- FeatureEngine init failure → graceful degradation (feature_engine=None), no crash
"""
from __future__ import annotations

import asyncio
import os
import unittest
from unittest.mock import MagicMock, patch

from hft_platform.engine.event_bus import RingBufferBus
from hft_platform.services.market_data import MarketDataService


class TestFeatureEngineDefaultOn(unittest.TestCase):
    """Tests for FeatureEngine default-on and graceful degradation."""

    def _make_service(self, env_overrides: dict[str, str] | None = None) -> MarketDataService:
        bus = MagicMock(spec=RingBufferBus)
        raw_queue: asyncio.Queue = asyncio.Queue()
        client = MagicMock()
        env = {k: v for k, v in os.environ.items() if k != "HFT_FEATURE_ENGINE_ENABLED"}
        if env_overrides:
            env.update(env_overrides)
        with patch.dict(os.environ, env, clear=True):
            svc = MarketDataService(bus, raw_queue, client)
        return svc

    def test_default_no_env_var_feature_engine_enabled(self) -> None:
        """When HFT_FEATURE_ENGINE_ENABLED is not set, FeatureEngine should be active."""
        from hft_platform.feature.engine import FeatureEngine

        svc = self._make_service()
        self.assertIsNotNone(
            svc.feature_engine,
            "FeatureEngine should be enabled by default (no env var set)",
        )
        self.assertIsInstance(svc.feature_engine, FeatureEngine)

    def test_explicit_zero_disables_feature_engine(self) -> None:
        """When HFT_FEATURE_ENGINE_ENABLED=0, FeatureEngine should be None."""
        svc = self._make_service({"HFT_FEATURE_ENGINE_ENABLED": "0"})
        self.assertIsNone(
            svc.feature_engine,
            "FeatureEngine should be disabled when HFT_FEATURE_ENGINE_ENABLED=0",
        )

    def test_explicit_one_enables_feature_engine(self) -> None:
        """When HFT_FEATURE_ENGINE_ENABLED=1, FeatureEngine should be active."""
        from hft_platform.feature.engine import FeatureEngine

        svc = self._make_service({"HFT_FEATURE_ENGINE_ENABLED": "1"})
        self.assertIsNotNone(svc.feature_engine)
        self.assertIsInstance(svc.feature_engine, FeatureEngine)

    def test_feature_engine_init_failure_degrades_gracefully(self) -> None:
        """If FeatureEngine() raises, service should degrade to feature_engine=None, not crash."""
        bus = MagicMock(spec=RingBufferBus)
        raw_queue: asyncio.Queue = asyncio.Queue()
        client = MagicMock()

        env = {k: v for k, v in os.environ.items() if k != "HFT_FEATURE_ENGINE_ENABLED"}
        env["HFT_FEATURE_ENGINE_ENABLED"] = "1"

        with patch.dict(os.environ, env, clear=True):
            with patch(
                "hft_platform.services.market_data.FeatureEngine",
                side_effect=RuntimeError("simulated init failure"),
            ):
                svc = MarketDataService(bus, raw_queue, client)

        self.assertIsNone(
            svc.feature_engine,
            "FeatureEngine should be None after init failure (graceful degradation)",
        )

    def test_explicit_feature_engine_arg_takes_precedence(self) -> None:
        """A caller-supplied feature_engine instance overrides env var logic."""
        from hft_platform.feature.engine import FeatureEngine

        bus = MagicMock(spec=RingBufferBus)
        raw_queue: asyncio.Queue = asyncio.Queue()
        client = MagicMock()
        custom_fe = MagicMock(spec=FeatureEngine)

        env = {k: v for k, v in os.environ.items() if k != "HFT_FEATURE_ENGINE_ENABLED"}
        env["HFT_FEATURE_ENGINE_ENABLED"] = "0"  # disabled, but explicit arg wins

        with patch.dict(os.environ, env, clear=True):
            svc = MarketDataService(bus, raw_queue, client, feature_engine=custom_fe)

        self.assertIs(
            svc.feature_engine,
            custom_fe,
            "Caller-supplied feature_engine should be used regardless of env var",
        )


if __name__ == "__main__":
    unittest.main()
