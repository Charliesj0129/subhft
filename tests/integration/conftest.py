"""Shared fixtures for HFT Platform integration tests.

Provides pre-configured component fixtures (risk engine, position store,
mock broker client) so integration tests avoid duplicating setup boilerplate.
"""

from __future__ import annotations

import asyncio
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
import yaml

# ---------------------------------------------------------------------------
# Environment helpers
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _disable_rust_accel(monkeypatch: pytest.MonkeyPatch) -> None:
    """Disable Rust accelerators by default in integration tests."""
    monkeypatch.setenv("HFT_RISK_RUST_VALIDATOR", "0")
    monkeypatch.setenv("HFT_RISK_FAST_GATE", "0")
    monkeypatch.setenv("HFT_RUST_POSITIONS", "0")
    monkeypatch.setenv("HFT_RUST_ACCEL", "0")
    monkeypatch.setenv("HFT_FUSED_NORMALIZER", "0")


# ---------------------------------------------------------------------------
# Component fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def risk_engine(tmp_path: Any) -> Any:
    """Create a RiskEngine with mocked externals for integration tests."""
    cfg: dict[str, Any] = {
        "global_defaults": {
            "max_price_cap": 5000.0,
            "tick_size": 0.01,
            "price_band_ticks": 200,
            "max_notional": 10_000_000,
            "per_symbol_max_notional": 50_000_000,
            "max_position_lots": 100,
            "daily_loss_limit": 500_000,
        },
        "strategies": {},
    }
    cfg_path = tmp_path / "strategy_limits.yaml"
    cfg_path.write_text(yaml.dump(cfg))

    with (
        patch("hft_platform.risk.engine.MetricsRegistry") as mock_mr,
        patch("hft_platform.risk.engine.LatencyRecorder") as mock_lr,
        patch("hft_platform.risk.engine.get_audit_writer", return_value=MagicMock()),
    ):
        mock_mr.get.return_value = None
        mock_lr.get.return_value = None
        from hft_platform.risk.engine import RiskEngine

        engine = RiskEngine(str(cfg_path), asyncio.Queue(), asyncio.Queue())
        engine.metrics = None
        yield engine


@pytest.fixture()
def position_store() -> Any:
    """Create a PositionStore with metrics disabled for integration tests."""
    with patch("hft_platform.execution.positions.MetricsRegistry") as mock_mr:
        mock_mr.get.return_value = None
        from hft_platform.execution.positions import PositionStore

        store = PositionStore()
        store._rust_tracker = None
        store.metrics = None
        yield store


@pytest.fixture()
def mock_broker_client() -> MagicMock:
    """Create a mock broker client conforming to BrokerClientProtocol."""
    client = MagicMock()
    client.login.return_value = True
    client.place_order.return_value = MagicMock(order_id="ORD-001")
    client.cancel_order.return_value = True
    client.update_order.return_value = True
    client.get_positions.return_value = []
    client.subscribe_basket.return_value = None
    client.set_execution_callbacks.return_value = None
    client.close.return_value = None
    return client


@pytest.fixture()
def storm_guard() -> Any:
    """Create a StormGuard with mocked externals for integration tests."""
    with (
        patch("hft_platform.risk.storm_guard.MetricsRegistry") as mock_mr,
        patch("hft_platform.risk.storm_guard.get_audit_writer", return_value=MagicMock()),
    ):
        mock_metrics = MagicMock()
        mock_mr.get.return_value = mock_metrics
        from hft_platform.risk.storm_guard import StormGuard

        guard = StormGuard()
        yield guard
