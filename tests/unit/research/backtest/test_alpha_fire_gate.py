"""Tests for the ``should_fire()`` gate in ``AlphaStrategyBridge.on_stats``.

Codex adversarial-review 2026-05-06 finding 4 (HIGH): when an alpha exposes a
discrete fire-decision API (e.g. c75's 300-tick warmup + spread-regime guard),
the backtest used to score the *raw* composite signal, ignoring the gate. Gate
C therefore validated a strategy that would never trade pre-warmup or during
anomalous spread regimes in live, while the backtest happily simulated those
trades.

The fix is the gated-signal approach in ``alpha_strategy_bridge.py``: after
``signal = alpha.update(**payload)``, if ``alpha`` exposes ``should_fire()``
and it returns 0, the signal is zeroed. Alphas without ``should_fire()`` see
no behaviour change (``hasattr`` guard).

These tests pin three sub-contracts:

1. **Pre-warmup**: ``should_fire() == 0`` -> signal recorded as 0.0 even when
   the raw composite is large.
2. **Anomalous regime**: same gate fires under another reason.
3. **Normal regime**: ``should_fire() != 0`` -> raw signal flows through.

Plus a no-regression contract for alphas without ``should_fire()`` and a
fail-open contract for misbehaving fire gates.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock

import pytest

from research.backtest.alpha_strategy_bridge import AlphaStrategyBridge


# ---------------------------------------------------------------------------
# Synthetic alphas mimicking c75's API surface
# ---------------------------------------------------------------------------


class _GatedAlpha:
    """Mimics c75's contract: update() returns a raw composite, should_fire()
    decides whether to act on it.

    ``should_fire_value`` is a programmable return for the gate so each test
    can drive a specific scenario without spinning up the real alpha.
    """

    def __init__(self, *, raw_signal: float, should_fire_value: int) -> None:
        self._raw_signal = float(raw_signal)
        self._should_fire_value = int(should_fire_value)

    @property
    def manifest(self) -> Any:
        m = MagicMock()
        m.alpha_id = "gated_taker"
        m.data_fields = ("bid_px", "ask_px")
        return m

    def reset(self) -> None:  # pragma: no cover - irrelevant for these tests
        pass

    def update(self, **_kwargs: Any) -> float:
        return self._raw_signal

    def should_fire(self) -> int:
        return self._should_fire_value


class _LegacyAlpha:
    """Alpha that does NOT expose should_fire(). Used to prove the
    ``hasattr`` guard preserves existing semantics for the rest of the
    registry."""

    def __init__(self, *, raw_signal: float) -> None:
        self._raw_signal = float(raw_signal)

    @property
    def manifest(self) -> Any:
        m = MagicMock()
        m.alpha_id = "legacy"
        m.data_fields = ("bid_px", "ask_px")
        return m

    def reset(self) -> None:  # pragma: no cover - irrelevant for these tests
        pass

    def update(self, **_kwargs: Any) -> float:
        return self._raw_signal


class _MisbehavingGatedAlpha:
    """should_fire() raises -- bridge must fail-open, not crash."""

    def __init__(self, *, raw_signal: float) -> None:
        self._raw_signal = float(raw_signal)

    @property
    def manifest(self) -> Any:
        m = MagicMock()
        m.alpha_id = "misbehaving"
        m.data_fields = ("bid_px", "ask_px")
        return m

    def reset(self) -> None:  # pragma: no cover - irrelevant
        pass

    def update(self, **_kwargs: Any) -> float:
        return self._raw_signal

    def should_fire(self) -> int:
        raise RuntimeError("intentional failure for fail-open contract")


# ---------------------------------------------------------------------------
# LOBStatsEvent helper
# ---------------------------------------------------------------------------


def _make_lob_event(
    *,
    symbol: str = "TMFD6",
    ts: int = 1_000_000_000,
    best_bid: int = 999_000,
    best_ask: int = 1_001_000,
    bid_depth: int = 10,
    ask_depth: int = 5,
    imbalance: float = 0.0,
) -> Any:
    from hft_platform.events import LOBStatsEvent

    return LOBStatsEvent(
        symbol=symbol,
        ts=ts,
        imbalance=imbalance,
        best_bid=best_bid,
        best_ask=best_ask,
        bid_depth=bid_depth,
        ask_depth=ask_depth,
    )


# ---------------------------------------------------------------------------
# Sub-contract 1: pre-warmup / spread-regime fail (should_fire == 0)
# ---------------------------------------------------------------------------


def test_should_fire_zero_zeroes_signal_even_when_raw_is_large() -> None:
    """A taker that hasn't reached warmup must not contribute to position
    accumulation, regardless of how large its raw composite is."""
    alpha = _GatedAlpha(raw_signal=5.0, should_fire_value=0)
    bridge = AlphaStrategyBridge(alpha, symbol="TMFD6")
    bridge.on_stats(_make_lob_event())

    assert len(bridge.signal_log) == 1
    _, signal, _ = bridge.signal_log[0]
    assert signal == pytest.approx(0.0), (
        "should_fire() == 0 must zero the recorded signal; otherwise the "
        "position-converter sees the raw composite and trades pre-warmup."
    )


def test_should_fire_zero_in_anomalous_regime_zeroes_signal() -> None:
    """Same gate semantics under a different scenario (e.g. wide spread).
    The bridge cannot tell the gate's reason apart -- it only sees 0/non-0.
    """
    alpha = _GatedAlpha(raw_signal=-3.7, should_fire_value=0)
    bridge = AlphaStrategyBridge(alpha, symbol="TMFD6")
    bridge.on_stats(_make_lob_event())

    _, signal, _ = bridge.signal_log[0]
    assert signal == pytest.approx(0.0)


# ---------------------------------------------------------------------------
# Sub-contract 2: normal regime (should_fire != 0)
# ---------------------------------------------------------------------------


def test_should_fire_nonzero_passes_raw_signal_through_buy() -> None:
    alpha = _GatedAlpha(raw_signal=0.42, should_fire_value=1)
    bridge = AlphaStrategyBridge(alpha, symbol="TMFD6")
    bridge.on_stats(_make_lob_event())

    _, signal, _ = bridge.signal_log[0]
    assert signal == pytest.approx(0.42)


def test_should_fire_nonzero_passes_raw_signal_through_sell() -> None:
    alpha = _GatedAlpha(raw_signal=-0.42, should_fire_value=-1)
    bridge = AlphaStrategyBridge(alpha, symbol="TMFD6")
    bridge.on_stats(_make_lob_event())

    _, signal, _ = bridge.signal_log[0]
    assert signal == pytest.approx(-0.42)


# ---------------------------------------------------------------------------
# Sub-contract 3: legacy alphas without should_fire() are unaffected
# ---------------------------------------------------------------------------


def test_legacy_alpha_without_should_fire_unchanged() -> None:
    """The hasattr guard must preserve raw-signal semantics for alphas that
    don't expose a fire gate. Without this, the gated-signal approach would
    secretly change behaviour for every alpha in the registry."""
    alpha = _LegacyAlpha(raw_signal=0.31)
    bridge = AlphaStrategyBridge(alpha, symbol="TMFD6")
    bridge.on_stats(_make_lob_event())

    _, signal, _ = bridge.signal_log[0]
    assert signal == pytest.approx(0.31)


# ---------------------------------------------------------------------------
# Sub-contract 4: fail-open on misbehaving fire gate
# ---------------------------------------------------------------------------


def test_misbehaving_should_fire_fails_open() -> None:
    """If should_fire() raises, the bridge must NOT crash and NOT zero the
    signal silently -- the threshold gate downstream still filters. Failing
    closed (zeroing) on exceptions would mask alpha bugs as "no-trade"
    sessions, which is harder to debug than a noisy raw signal."""
    alpha = _MisbehavingGatedAlpha(raw_signal=0.42)
    bridge = AlphaStrategyBridge(alpha, symbol="TMFD6")
    bridge.on_stats(_make_lob_event())  # must not raise

    _, signal, _ = bridge.signal_log[0]
    assert signal == pytest.approx(0.42), (
        "Fail-open on should_fire() exception preserves raw-signal "
        "behaviour so a misbehaving fire-gate doesn't silently mask trades."
    )
