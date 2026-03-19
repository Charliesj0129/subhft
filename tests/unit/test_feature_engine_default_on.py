"""Tests for Feature Engine default-on behavior (Unit 8)."""

from __future__ import annotations

import asyncio
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_market_data_service(**overrides: Any) -> Any:
    """Create a minimal MarketDataService for testing init behavior."""
    from hft_platform.services.market_data import MarketDataService

    mock_bus = MagicMock()
    mock_raw_queue = asyncio.Queue(maxsize=100)
    mock_client = MagicMock()

    kwargs: dict[str, Any] = {
        "bus": mock_bus,
        "raw_queue": mock_raw_queue,
        "client": mock_client,
    }
    kwargs.update(overrides)
    return MarketDataService(**kwargs)


# ---------------------------------------------------------------------------
# Default-on tests
# ---------------------------------------------------------------------------


class TestFeatureEngineDefaultOn:
    def test_default_env_enables_feature_engine(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """With no HFT_FEATURE_ENGINE_ENABLED env var, should default to enabled."""
        monkeypatch.delenv("HFT_FEATURE_ENGINE_ENABLED", raising=False)

        with patch("hft_platform.services.market_data.FeatureEngine") as MockFE:
            MockFE.return_value = MagicMock()
            svc = _make_market_data_service()
        assert svc.feature_engine is not None

    def test_explicit_on_enables_feature_engine(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("HFT_FEATURE_ENGINE_ENABLED", "1")
        with patch("hft_platform.services.market_data.FeatureEngine") as MockFE:
            MockFE.return_value = MagicMock()
            svc = _make_market_data_service()
        assert svc.feature_engine is not None

    def test_explicit_off_disables_feature_engine(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("HFT_FEATURE_ENGINE_ENABLED", "0")
        svc = _make_market_data_service()
        assert svc.feature_engine is None

    def test_false_string_disables_feature_engine(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("HFT_FEATURE_ENGINE_ENABLED", "false")
        svc = _make_market_data_service()
        assert svc.feature_engine is None

    def test_init_failure_graceful_degradation(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """When FeatureEngine() raises, should set feature_engine=None gracefully."""
        monkeypatch.setenv("HFT_FEATURE_ENGINE_ENABLED", "1")
        with patch(
            "hft_platform.services.market_data.FeatureEngine",
            side_effect=RuntimeError("no rust module"),
        ):
            svc = _make_market_data_service()
        assert svc.feature_engine is None

    def test_explicit_feature_engine_arg_takes_precedence(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Explicitly passed feature_engine should be used regardless of env var."""
        monkeypatch.setenv("HFT_FEATURE_ENGINE_ENABLED", "0")
        mock_fe = MagicMock()
        svc = _make_market_data_service(feature_engine=mock_fe)
        assert svc.feature_engine is mock_fe

    def test_explicit_feature_engine_not_replaced_when_enabled(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Explicitly passed feature_engine should not be replaced even when enabled."""
        monkeypatch.setenv("HFT_FEATURE_ENGINE_ENABLED", "1")
        mock_fe = MagicMock()
        with patch("hft_platform.services.market_data.FeatureEngine") as MockFE:
            svc = _make_market_data_service(feature_engine=mock_fe)
        MockFE.assert_not_called()
        assert svc.feature_engine is mock_fe
