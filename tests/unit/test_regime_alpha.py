"""Tests for WU9: research/tools/regime_alpha.py — RegimeConditionalAlpha."""

from __future__ import annotations

import types
from typing import Any
from unittest.mock import MagicMock

import pytest

from research.tools.regime_alpha import (
    _WARMUP_TICKS,
    RegimeConditionalAlpha,
)

# ---------------------------------------------------------------------------
# Helpers — minimal AlphaProtocol mock
# ---------------------------------------------------------------------------


def _make_manifest(alpha_id: str = "test_alpha") -> MagicMock:
    manifest = MagicMock()
    manifest.alpha_id = alpha_id
    return manifest


def _make_base_alpha(
    alpha_id: str = "test_alpha",
    *,
    signal_scale: float = 1.0,
    signal_value: float = 0.5,
) -> MagicMock:
    """Return a minimal AlphaProtocol-compatible mock."""
    base = MagicMock()
    base.manifest = _make_manifest(alpha_id)
    base.signal_scale = signal_scale
    base.update.return_value = signal_value
    base.get_signal.return_value = signal_value
    return base


def _make_event(mid_price: float) -> types.SimpleNamespace:
    return types.SimpleNamespace(mid_price=mid_price)


def _make_dict_event(mid_price: float) -> dict[str, float]:
    return {"mid_price": mid_price}


# ---------------------------------------------------------------------------
# Initialisation
# ---------------------------------------------------------------------------


def test_regime_alpha_initializes_mean_reverting() -> None:
    """Initial regime should always be mean_reverting before warmup."""
    base = _make_base_alpha()
    regime_alpha = RegimeConditionalAlpha(base, {})
    assert regime_alpha.current_regime == "mean_reverting"


def test_regime_alpha_tick_count_starts_zero() -> None:
    base = _make_base_alpha()
    ra = RegimeConditionalAlpha(base, {})
    assert ra.tick_count == 0


# ---------------------------------------------------------------------------
# get_signal
# ---------------------------------------------------------------------------


def test_regime_alpha_get_signal_delegates_to_base() -> None:
    """get_signal should simply delegate to base alpha.get_signal()."""
    base = _make_base_alpha(signal_value=0.42)
    ra = RegimeConditionalAlpha(base, {})
    assert ra.get_signal() == pytest.approx(0.42)
    base.get_signal.assert_called_once()


# ---------------------------------------------------------------------------
# update delegation
# ---------------------------------------------------------------------------


def test_regime_alpha_update_delegates_to_base() -> None:
    """update() must call base.update() and return its result."""
    base = _make_base_alpha(signal_value=0.99)
    ra = RegimeConditionalAlpha(base, {})
    event = _make_event(100.0)
    result = ra.update(event)
    assert result == pytest.approx(0.99)
    base.update.assert_called_once_with(event)


def test_regime_alpha_update_with_dict_event() -> None:
    """update() should work when the event is a plain dict."""
    base = _make_base_alpha()
    ra = RegimeConditionalAlpha(base, {})
    result = ra.update({"mid_price": 150.0})
    assert result == base.update.return_value


# ---------------------------------------------------------------------------
# manifest proxy
# ---------------------------------------------------------------------------


def test_regime_alpha_manifest_proxies_base() -> None:
    base = _make_base_alpha(alpha_id="my_alpha")
    ra = RegimeConditionalAlpha(base, {})
    assert ra.manifest.alpha_id == "my_alpha"


# ---------------------------------------------------------------------------
# reset
# ---------------------------------------------------------------------------


def test_regime_alpha_reset_clears_state() -> None:
    """reset() should zero EMA state, reset tick count, and call base.reset()."""
    base = _make_base_alpha()
    ra = RegimeConditionalAlpha(base, {})

    # Advance state.
    for price in range(1, 20):
        ra.update(_make_event(float(price)))

    assert ra.tick_count > 0

    ra.reset()

    assert ra.tick_count == 0
    assert ra.current_regime == "mean_reverting"
    base.reset.assert_called_once()


def test_regime_alpha_reset_restores_initial_conditions() -> None:
    """After reset, properties should match a freshly constructed instance."""
    base = _make_base_alpha()
    ra = RegimeConditionalAlpha(base, {})

    for i in range(10):
        ra.update(_make_event(float(i * 10)))

    ra.reset()

    fresh = RegimeConditionalAlpha(_make_base_alpha(), {})
    assert ra.current_regime == fresh.current_regime
    assert ra.tick_count == fresh.tick_count


# ---------------------------------------------------------------------------
# Regime detection — volatile
# ---------------------------------------------------------------------------


def test_regime_alpha_detects_volatile_after_warmup() -> None:
    """Feeding high-variance returns after warmup should classify regime as volatile."""
    base = _make_base_alpha()
    # Use low VOL_REGIME_MULTIPLIER to make volatile regime easier to trigger.
    ra = RegimeConditionalAlpha(
        base,
        {},
        vol_regime_multiplier=1.05,  # almost any vol spike will trigger
    )

    rng_seed = 0
    import numpy as np

    rng = np.random.default_rng(rng_seed)

    # Phase 1: stable prices for warmup.
    stable_price = 100.0
    for _ in range(_WARMUP_TICKS + 10):
        ra.update(_make_event(stable_price + rng.uniform(-0.01, 0.01)))

    # Phase 2: large jumps to spike vol_ema well above baseline.
    for _ in range(30):
        stable_price += 5.0 * (1 if rng.random() > 0.5 else -1)
        ra.update(_make_event(stable_price))

    assert ra.current_regime == "volatile", f"Expected 'volatile' but got {ra.current_regime!r}"


# ---------------------------------------------------------------------------
# Regime detection — trending
# ---------------------------------------------------------------------------


def test_regime_alpha_detects_trending() -> None:
    """A persistent directional drift past warmup should trigger trending regime."""
    base = _make_base_alpha()
    # Set trend_threshold low enough that a steady drift always exceeds it.
    ra = RegimeConditionalAlpha(
        base,
        {},
        vol_regime_multiplier=100.0,  # never volatile
        trend_threshold=0.001,  # very sensitive to trend
    )

    price = 100.0
    for _ in range(_WARMUP_TICKS + 50):
        price += 1.0  # constant upward drift
        ra.update(_make_event(price))

    assert ra.current_regime == "trending", f"Expected 'trending' but got {ra.current_regime!r}"


# ---------------------------------------------------------------------------
# Config application
# ---------------------------------------------------------------------------


def test_regime_alpha_applies_config_to_base() -> None:
    """_apply_regime_config should setattr on base alpha for matching attributes."""
    base = _make_base_alpha()
    base.signal_scale = 1.0

    regime_configs: dict[str, dict[str, Any]] = {
        "mean_reverting": {"signal_scale": 0.5},
    }
    ra = RegimeConditionalAlpha(base, regime_configs)

    # Directly call private method to test override application in isolation.
    ra._apply_regime_config("mean_reverting")

    assert base.signal_scale == 0.5


def test_regime_alpha_does_not_apply_config_for_unknown_regime() -> None:
    """Config for a regime not currently active should not be applied."""
    base = _make_base_alpha()
    base.signal_scale = 1.0

    regime_configs: dict[str, dict[str, Any]] = {
        "volatile": {"signal_scale": 0.1},
    }
    ra = RegimeConditionalAlpha(base, regime_configs)

    # Apply config for mean_reverting (no entry) — attribute should be unchanged.
    ra._apply_regime_config("mean_reverting")

    assert base.signal_scale == 1.0


def test_regime_alpha_handles_missing_attribute_without_crash(caplog: pytest.LogCaptureFixture) -> None:
    """If regime config references an attribute the base alpha lacks, log warning, don't raise."""
    base = _make_base_alpha()
    # Ensure base does NOT have the target attribute.
    if hasattr(base, "nonexistent_param"):
        delattr(base, "nonexistent_param")

    # Mock hasattr to return False for our fabricated attribute.
    # Use MagicMock spec to restrict attributes strictly.
    base_strict = MagicMock(spec=["manifest", "update", "reset", "get_signal", "signal_scale"])
    base_strict.manifest = _make_manifest()
    base_strict.signal_scale = 1.0

    regime_configs: dict[str, dict[str, Any]] = {
        "mean_reverting": {"nonexistent_param": 99},
    }
    ra = RegimeConditionalAlpha(base_strict, regime_configs)

    # Should not raise — just warn.
    ra._apply_regime_config("mean_reverting")

    # signal_scale should be unchanged since the attr is not defined.
    assert base_strict.signal_scale == 1.0


# ---------------------------------------------------------------------------
# __repr__
# ---------------------------------------------------------------------------


def test_regime_alpha_repr() -> None:
    base = _make_base_alpha(alpha_id="my_alpha")
    ra = RegimeConditionalAlpha(base, {})
    r = repr(ra)
    assert "my_alpha" in r
    assert "mean_reverting" in r


# ---------------------------------------------------------------------------
# Unknown regime config warning
# ---------------------------------------------------------------------------


def test_regime_alpha_warns_on_unknown_regime_keys() -> None:
    """Passing unknown regime names in config should not raise (just logs a warning)."""
    base = _make_base_alpha()
    regime_configs: dict[str, dict[str, Any]] = {
        "unknown_regime_xyz": {"signal_scale": 0.1},
    }
    # Should not raise during construction.
    ra = RegimeConditionalAlpha(base, regime_configs)
    assert ra is not None


# ---------------------------------------------------------------------------
# mid_price extraction variants
# ---------------------------------------------------------------------------


def test_regime_alpha_extracts_mid_price_from_dict() -> None:
    base = _make_base_alpha()
    ra = RegimeConditionalAlpha(base, {})
    extracted = ra._extract_mid_price({"mid_price": 123.0})
    assert extracted == pytest.approx(123.0)


def test_regime_alpha_extracts_mid_price_from_namespace() -> None:
    base = _make_base_alpha()
    ra = RegimeConditionalAlpha(base, {})
    event = types.SimpleNamespace(mid_price=200.0)
    extracted = ra._extract_mid_price(event)
    assert extracted == pytest.approx(200.0)


def test_regime_alpha_extracts_mid_px_fallback() -> None:
    base = _make_base_alpha()
    ra = RegimeConditionalAlpha(base, {})
    event = types.SimpleNamespace(mid_px=300.0)
    extracted = ra._extract_mid_price(event)
    assert extracted == pytest.approx(300.0)


def test_regime_alpha_returns_none_when_no_price_field() -> None:
    base = _make_base_alpha()
    ra = RegimeConditionalAlpha(base, {})
    event = types.SimpleNamespace(other_field=42.0)
    extracted = ra._extract_mid_price(event)
    assert extracted is None


def test_regime_alpha_returns_none_for_none_event() -> None:
    base = _make_base_alpha()
    ra = RegimeConditionalAlpha(base, {})
    extracted = ra._extract_mid_price(None)
    assert extracted is None
