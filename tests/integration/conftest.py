"""Shared fixtures for HFT Platform integration tests."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any, Generator
from unittest.mock import MagicMock, patch

import pytest
import yaml

from hft_platform.feed_adapter.protocol import BrokerClientProtocol
from tests.factories import make_fill_event, make_order_intent

# ---------------------------------------------------------------------------
# Config helpers
# ---------------------------------------------------------------------------


@pytest.fixture()
def risk_yaml(tmp_path: Path) -> str:
    """Write a minimal strategy_limits.yaml and return its path."""
    data = {
        "global_defaults": {
            "max_price_cap": 5000.0,
            "tick_size": 0.01,
            "price_band_ticks": 20,
            "max_notional": 10_000_000,
            "max_order_size": 1000,
            "position_limit": 100,
            "daily_loss_limit": 500_000,
        },
        "strategies": {},
    }
    p = tmp_path / "strategy_limits.yaml"
    p.write_text(yaml.dump(data))
    return str(p)


# ---------------------------------------------------------------------------
# Factory fixtures (thin wrappers around tests.factories)
# ---------------------------------------------------------------------------


@pytest.fixture()
def make_fill() -> Any:
    """Factory fixture returning FillEvent instances via ``tests.factories``."""
    _counter = 0

    def _factory(**overrides: Any) -> Any:
        nonlocal _counter
        _counter += 1
        overrides.setdefault("fill_id", f"fill_{_counter}")
        overrides.setdefault("ingest_ts_ns", _counter * 1_000_000)
        overrides.setdefault("match_ts_ns", _counter * 1_000_000)
        return make_fill_event(**overrides)

    return _factory


@pytest.fixture()
def make_intent() -> Any:
    """Factory fixture returning OrderIntent instances via ``tests.factories``."""
    _counter = 0

    def _factory(**overrides: Any) -> Any:
        nonlocal _counter
        _counter += 1
        overrides.setdefault("intent_id", _counter)
        overrides.setdefault("timestamp_ns", _counter * 1_000_000)
        return make_order_intent(**overrides)

    return _factory


# ---------------------------------------------------------------------------
# Component fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def position_store(monkeypatch: pytest.MonkeyPatch) -> Generator[Any, None, None]:
    """Create a PositionStore with Rust and metrics disabled."""
    monkeypatch.setenv("HFT_RUST_POSITIONS", "0")
    with patch("hft_platform.execution.positions.MetricsRegistry") as mock_mr:
        mock_mr.get.return_value = None
        from hft_platform.execution.positions import PositionStore

        store = PositionStore()
        store._rust_tracker = None
        store.metrics = None
        yield store


@pytest.fixture()
def risk_engine(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Generator[Any, None, None]:
    """Create a RiskEngine with mocked externals."""
    monkeypatch.setenv("HFT_RISK_RUST_VALIDATOR", "0")
    monkeypatch.setenv("HFT_RISK_FAST_GATE", "0")

    data = {
        "global_defaults": {
            "max_price_cap": 5000.0,
            "tick_size": 0.01,
            "price_band_ticks": 20,
            "max_notional": 10_000_000,
            "max_order_size": 1000,
            "position_limit": 100,
            "daily_loss_limit": 500_000,
        },
        "strategies": {},
    }
    cfg_path = tmp_path / "strategy_limits.yaml"
    cfg_path.write_text(yaml.dump(data))

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
def mock_broker_client() -> MagicMock:
    """Return a MagicMock satisfying BrokerClientProtocol."""
    client: MagicMock = MagicMock(spec=BrokerClientProtocol)
    client.login.return_value = True
    client.place_order.return_value = MagicMock(name="trade_receipt")
    client.cancel_order.return_value = None
    client.get_positions.return_value = []
    client.close.return_value = None
    return client


@pytest.fixture()
def integration_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Set common environment variables for integration tests."""
    monkeypatch.setenv("HFT_MODE", "sim")
    monkeypatch.setenv("HFT_RUST_ACCEL", "0")
    monkeypatch.setenv("HFT_RUST_POSITIONS", "0")
    monkeypatch.setenv("HFT_RISK_RUST_VALIDATOR", "0")
    monkeypatch.setenv("HFT_RISK_FAST_GATE", "0")
