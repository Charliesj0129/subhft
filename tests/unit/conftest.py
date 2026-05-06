"""Shared fixtures for HFT Platform unit tests."""

from __future__ import annotations

import asyncio
from unittest.mock import MagicMock, patch

import pytest
import yaml

from hft_platform.contracts.execution import FillEvent, Side
from hft_platform.contracts.strategy import TIF, IntentType, OrderIntent

# ---------------------------------------------------------------------------
# Config helpers
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _disable_live_monitor_publisher(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep unit tests from starting real Redis-backed monitor publisher threads by default."""
    monkeypatch.setenv("HFT_MONITOR_LIVE_ENABLED", "0")


@pytest.fixture(autouse=True)
def _disable_clickhouse_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep unit tests from attempting real ClickHouse connections unless they opt in explicitly."""
    monkeypatch.setenv("HFT_CLICKHOUSE_ENABLED", "0")


@pytest.fixture(autouse=True)
def _scrub_operator_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Strip developer .env / shell pollution before each unit test.

    Makefile uses ``-include .env`` (see :file:`Makefile:4`), so values like
    ``HFT_MODE=real`` and ``HFT_QUOTE_CONNECTIONS=2`` reach pytest under
    ``make ci`` but not under a bare ``uv run pytest`` shell. This made unit
    tests pass standalone but fail under CI for any test that:

      * loads :class:`HftConfig` via the L1 strict validator (rejects
        ``mode='real'``);
      * exercises the single-conn broker facade path (multi-conn pool
        replaces the facade, breaking ``call_count`` assertions).

    Tests that need these vars must set them explicitly via their own
    ``monkeypatch.setenv`` — the autouse fixture only clears the *defaults*.
    """
    for var in (
        "HFT_MODE",
        "HFT_ENV",
        "HFT_QUOTE_CONNECTIONS",
        "HFT_LOOP_ID",
        "HFT_LOOP",
        "HFT_ORDER_MODE",
        "HFT_LIVE_CONFIRM",
    ):
        monkeypatch.delenv(var, raising=False)


@pytest.fixture()
def symbols_yaml(tmp_path):
    """Write a minimal symbols.yaml and return its path."""
    data = {
        "symbols": {
            "2330": {
                "name": "TSMC",
                "exchange": "TWSE",
                "price_scale": 10000,
                "tick_size": 0.01,
            }
        }
    }
    p = tmp_path / "symbols.yaml"
    p.write_text(yaml.dump(data))
    return str(p)


@pytest.fixture()
def risk_yaml(tmp_path):
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
# Factory fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def make_fill():
    """Factory that returns FillEvent instances with sensible defaults."""
    _counter = 0

    def _factory(**overrides):
        nonlocal _counter
        _counter += 1
        defaults = dict(
            fill_id=f"fill_{_counter}",
            account_id="acc_test",
            order_id=f"ord_{_counter}",
            strategy_id="test_strategy",
            symbol="2330",
            side=Side.BUY,
            qty=1,
            price=1_000_000,  # 100.0 scaled x10000
            fee=0,
            tax=0,
            ingest_ts_ns=_counter * 1_000_000,
            match_ts_ns=_counter * 1_000_000,
        )
        defaults.update(overrides)
        return FillEvent(**defaults)

    return _factory


@pytest.fixture()
def make_intent():
    """Factory that returns OrderIntent instances with sensible defaults."""
    _counter = 0

    def _factory(**overrides):
        nonlocal _counter
        _counter += 1
        defaults = dict(
            intent_id=_counter,
            strategy_id="test_strategy",
            symbol="2330",
            intent_type=IntentType.NEW,
            side=Side.BUY,
            price=1_000_000,  # 100.0 scaled x10000
            qty=1,
            tif=TIF.LIMIT,
            timestamp_ns=_counter * 1_000_000,
        )
        defaults.update(overrides)
        return OrderIntent(**defaults)

    return _factory


# ---------------------------------------------------------------------------
# Component fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def risk_engine(tmp_path, monkeypatch):
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
        patch("hft_platform.recorder.audit.get_audit_writer", return_value=MagicMock()),
    ):
        mock_mr.get.return_value = None
        mock_lr.get.return_value = None
        from hft_platform.risk.engine import RiskEngine

        engine = RiskEngine(str(cfg_path), asyncio.Queue(), asyncio.Queue())
        engine.metrics = None
        yield engine


@pytest.fixture()
def position_store(monkeypatch):
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
def normalizer(monkeypatch):
    """Create a MarketDataNormalizer with Rust acceleration disabled."""
    monkeypatch.setenv("HFT_RUST_ACCEL", "0")
    monkeypatch.setenv("HFT_FUSED_NORMALIZER", "0")
    with patch("hft_platform.feed_adapter.normalizer.MetricsRegistry") as mock_mr:
        mock_mr.get.return_value = None
        from hft_platform.feed_adapter.normalizer import MarketDataNormalizer

        norm = MarketDataNormalizer.__new__(MarketDataNormalizer)
        norm.metrics = None
        norm._rust_enabled = False
        yield norm
