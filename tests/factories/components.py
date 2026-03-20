"""Factories for shared platform components used across the test suite.

Encapsulates mock/env-var setup patterns for RiskEngine, PositionStore,
StormGuard, and MarketDataNormalizer so tests need not duplicate boilerplate.
"""

from __future__ import annotations

import asyncio
import os
from typing import Any
from unittest.mock import MagicMock, patch

import yaml

from hft_platform.contracts.strategy import StormGuardState


def make_risk_engine(
    tmp_path: Any,
    *,
    storm_state: StormGuardState = StormGuardState.NORMAL,
    config_overrides: dict[str, Any] | None = None,
) -> Any:
    """Create a RiskEngine with mocked MetricsRegistry/LatencyRecorder/audit.

    ``tmp_path`` must be a ``pathlib.Path`` (e.g. from pytest ``tmp_path`` fixture).
    """
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
    if config_overrides:
        for k, v in config_overrides.items():
            if isinstance(v, dict) and k in cfg:
                cfg[k].update(v)
            else:
                cfg[k] = v

    cfg_path = tmp_path / "strategy_limits.yaml"
    cfg_path.write_text(yaml.dump(cfg))

    # Must set env vars BEFORE importing RiskEngine (module-level reads)
    _prev_rust = os.environ.get("HFT_RISK_RUST_VALIDATOR")
    _prev_fg = os.environ.get("HFT_RISK_FAST_GATE")
    os.environ["HFT_RISK_RUST_VALIDATOR"] = "0"
    os.environ["HFT_RISK_FAST_GATE"] = "0"

    try:
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
            engine.storm_guard.state = storm_state
            return engine
    finally:
        if _prev_rust is None:
            os.environ.pop("HFT_RISK_RUST_VALIDATOR", None)
        else:
            os.environ["HFT_RISK_RUST_VALIDATOR"] = _prev_rust
        if _prev_fg is None:
            os.environ.pop("HFT_RISK_FAST_GATE", None)
        else:
            os.environ["HFT_RISK_FAST_GATE"] = _prev_fg


def make_position_store() -> Any:
    """Create a PositionStore with Rust tracker and metrics disabled."""
    _prev = os.environ.get("HFT_RUST_POSITIONS")
    os.environ["HFT_RUST_POSITIONS"] = "0"
    try:
        with patch("hft_platform.execution.positions.MetricsRegistry") as mock_mr:
            mock_mr.get.return_value = None
            from hft_platform.execution.positions import PositionStore

            store = PositionStore()
            store._rust_tracker = None
            store.metrics = None
            return store
    finally:
        if _prev is None:
            os.environ.pop("HFT_RUST_POSITIONS", None)
        else:
            os.environ["HFT_RUST_POSITIONS"] = _prev


def make_storm_guard(
    *,
    state: StormGuardState = StormGuardState.NORMAL,
    on_halt_callback: Any | None = None,
) -> Any:
    """Create a StormGuard with mocked MetricsRegistry and audit writer."""
    with (
        patch("hft_platform.risk.storm_guard.MetricsRegistry") as mock_mr,
        patch("hft_platform.risk.storm_guard.get_audit_writer", return_value=MagicMock()),
    ):
        mock_metrics = MagicMock()
        mock_mr.get.return_value = mock_metrics
        from hft_platform.risk.storm_guard import StormGuard

        guard = StormGuard(on_halt_callback=on_halt_callback)
        guard.state = state
        return guard


def make_normalizer() -> Any:
    """Create a MarketDataNormalizer with Rust acceleration disabled."""
    _prev_accel = os.environ.get("HFT_RUST_ACCEL")
    _prev_fused = os.environ.get("HFT_FUSED_NORMALIZER")
    os.environ["HFT_RUST_ACCEL"] = "0"
    os.environ["HFT_FUSED_NORMALIZER"] = "0"
    try:
        with patch("hft_platform.feed_adapter.normalizer.MetricsRegistry") as mock_mr:
            mock_mr.get.return_value = None
            from hft_platform.feed_adapter.normalizer import MarketDataNormalizer

            norm = MarketDataNormalizer.__new__(MarketDataNormalizer)
            norm.metrics = None
            norm._rust_enabled = False
            return norm
    finally:
        if _prev_accel is None:
            os.environ.pop("HFT_RUST_ACCEL", None)
        else:
            os.environ["HFT_RUST_ACCEL"] = _prev_accel
        if _prev_fused is None:
            os.environ.pop("HFT_FUSED_NORMALIZER", None)
        else:
            os.environ["HFT_FUSED_NORMALIZER"] = _prev_fused
