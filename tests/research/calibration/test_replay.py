"""Tests for replay.py — hftbacktest order submission bridge."""
from __future__ import annotations

import numpy as np
import pytest

from research.calibration.probe_strategy import PassiveQuoteProbe
from research.calibration.replay import (
    ReplayNotReadyError,
    build_probe_replay_fn,
)
from research.calibration.scoring import DailyFillSummary
from research.calibration.sweep import QueueModelCandidate

# ---------------------------------------------------------------------------
# Basic construction tests
# ---------------------------------------------------------------------------


def test_build_probe_replay_fn_returns_callable():
    fn = build_probe_replay_fn(
        instrument="TMFD6",
        probe_factory=lambda: PassiveQuoteProbe(qty=1, max_pos=3),
        l2_data_dir="/nonexistent",
        latency_us=36000,
        tick_size=1.0,
        lot_size=1.0,
    )
    assert callable(fn)


# ---------------------------------------------------------------------------
# File-not-found: real path, no CH streaming
# ---------------------------------------------------------------------------


def test_build_probe_replay_fn_missing_data_raises():
    """Missing .npz file raises FileNotFoundError (not ReplayNotReadyError)."""
    fn = build_probe_replay_fn(
        instrument="TMFD6",
        probe_factory=lambda: PassiveQuoteProbe(qty=1, max_pos=3),
        l2_data_dir="/nonexistent",
        latency_us=36000,
        tick_size=1.0,
        lot_size=1.0,
        allow_stub_execution=False,
    )
    cand = QueueModelCandidate("power_prob", 1.5)
    with pytest.raises(FileNotFoundError):
        fn(cand, "2026-03-01")


# ---------------------------------------------------------------------------
# Legacy stub path (allow_stub_execution=True)
# ---------------------------------------------------------------------------


def _make_minimal_hftbt_events() -> np.ndarray:
    """Build the smallest valid hftbacktest event array (clear + bid + ask + trade)."""
    DEPTH_EVENT = 1
    TRADE_EVENT = 2
    DEPTH_CLEAR_EVENT = 3
    EXCH_EVENT = 1 << 31
    LOCAL_EVENT = 1 << 30
    BUY_EVENT = 1 << 29
    SELL_EVENT = 1 << 28

    dtype = np.dtype([
        ("ev", "u8"), ("exch_ts", "i8"), ("local_ts", "i8"),
        ("px", "f8"), ("qty", "f8"), ("order_id", "u8"),
        ("ival", "i8"), ("fval", "f8"),
    ])
    rows = []
    # 5 snapshots, each with a trade to give the book some fills
    for i in range(5):
        t = 1_000_000_000 + i * 2_000_000_000
        rows.extend([
            (DEPTH_CLEAR_EVENT | EXCH_EVENT | LOCAL_EVENT, t, t + 1_000_000, 0.0, 0.0, 0, 0, 0.0),
            (DEPTH_EVENT | EXCH_EVENT | LOCAL_EVENT | BUY_EVENT, t, t + 1_000_000, 17000.0, 5.0, 0, 0, 0.0),
            (DEPTH_EVENT | EXCH_EVENT | LOCAL_EVENT | SELL_EVENT, t, t + 1_000_000, 17001.0, 3.0, 0, 0, 0.0),
            (TRADE_EVENT | EXCH_EVENT | LOCAL_EVENT | BUY_EVENT, t + 500_000, t + 1_500_000, 17001.0, 1.0, 0, 0, 0.0),
        ])
    return np.array(rows, dtype=dtype)


def test_allow_stub_execution_returns_zero_fills(tmp_path):
    """Legacy stub path: returns DailyFillSummary with n_fills=0."""
    import numpy as _np

    events = _make_minimal_hftbt_events()
    npz_path = tmp_path / "TMFD6_2026-03-01_l2.hftbt.npz"
    _np.savez(str(npz_path), events=events)

    fn = build_probe_replay_fn(
        instrument="TMFD6",
        probe_factory=lambda: PassiveQuoteProbe(qty=1, max_pos=3),
        l2_data_dir=tmp_path,
        latency_us=100,
        tick_size=1.0,
        lot_size=1.0,
        allow_stub_execution=True,
    )
    cand = QueueModelCandidate("power_prob", 1.5)
    result = fn(cand, "2026-03-01")
    assert isinstance(result, DailyFillSummary)
    assert result.n_fills == 0
    assert result.pnl == 0.0


# ---------------------------------------------------------------------------
# Real execution path — uses in-memory numpy events via ch_data_source mock
# ---------------------------------------------------------------------------


class _MockChDataSource:
    """Minimal mock that returns a pre-built numpy event array for any date."""

    def __init__(self, events: np.ndarray) -> None:
        self._events = events

    def load_day(self, instrument: str, date: str) -> np.ndarray:
        return self._events

    def load_days(self, instrument: str, dates: list[str]) -> list[np.ndarray]:
        return [self._events for _ in dates]


def test_real_execution_produces_nonzero_fills():
    """Real execution path submits orders and returns n_fills > 0 for a
    synthetic event stream with repeated bid/ask snapshots and trade events."""
    DEPTH_EVENT = 1
    TRADE_EVENT = 2
    DEPTH_CLEAR_EVENT = 3
    EXCH_EVENT = 1 << 31
    LOCAL_EVENT = 1 << 30
    BUY_EVENT = 1 << 29
    SELL_EVENT = 1 << 28

    dtype = np.dtype([
        ("ev", "u8"), ("exch_ts", "i8"), ("local_ts", "i8"),
        ("px", "f8"), ("qty", "f8"), ("order_id", "u8"),
        ("ival", "i8"), ("fval", "f8"),
    ])
    # Build enough events to exceed latency and trigger fills
    rows = []
    for i in range(200):
        t = 1_000_000_000 + i * 1_000_000_000  # 1s apart
        rows.extend([
            (DEPTH_CLEAR_EVENT | EXCH_EVENT | LOCAL_EVENT, t, t + 1_000_000, 0.0, 0.0, 0, 0, 0.0),
            (DEPTH_EVENT | EXCH_EVENT | LOCAL_EVENT | BUY_EVENT, t, t + 1_000_000, 17000.0, 5.0, 0, 0, 0.0),
            (DEPTH_EVENT | EXCH_EVENT | LOCAL_EVENT | SELL_EVENT, t, t + 1_000_000, 17001.0, 3.0, 0, 0, 0.0),
            # Trade at ask — fills passive ask orders
            (TRADE_EVENT | EXCH_EVENT | LOCAL_EVENT | BUY_EVENT, t + 100_000, t + 1_100_000, 17001.0, 1.0, 0, 0, 0.0),
            # Trade at bid — fills passive bid orders
            (TRADE_EVENT | EXCH_EVENT | LOCAL_EVENT | SELL_EVENT, t + 200_000, t + 1_200_000, 17000.0, 1.0, 0, 0, 0.0),
        ])
    events = np.array(rows, dtype=dtype)

    mock_src = _MockChDataSource(events)
    fn = build_probe_replay_fn(
        instrument="TMFD6",
        probe_factory=lambda: PassiveQuoteProbe(qty=1, max_pos=3),
        l2_data_dir="/nonexistent",  # irrelevant when CH streaming is used
        latency_us=100,  # 100 µs — well below 1s event spacing
        tick_size=1.0,
        lot_size=1.0,
        allow_stub_execution=False,
        use_ch_streaming=True,
        ch_data_source=mock_src,
    )
    cand = QueueModelCandidate("power_prob", 1.5)
    result = fn(cand, "2026-04-10")

    assert isinstance(result, DailyFillSummary)
    assert result.n_fills > 0, (
        f"Expected real fills but got n_fills={result.n_fills}. "
        "Order submission may not be reaching hftbacktest."
    )
    assert result.date == "2026-04-10"
    assert 0.0 <= result.adverse_pct <= 1.0


def test_real_execution_returns_daily_fill_summary_fields():
    """DailyFillSummary returned by real path has correct field types."""
    events = _make_minimal_hftbt_events()
    mock_src = _MockChDataSource(events)
    fn = build_probe_replay_fn(
        instrument="TMFD6",
        probe_factory=lambda: PassiveQuoteProbe(qty=1, max_pos=3),
        l2_data_dir="/nonexistent",
        latency_us=100,
        tick_size=1.0,
        lot_size=1.0,
        allow_stub_execution=False,
        use_ch_streaming=True,
        ch_data_source=mock_src,
    )
    cand = QueueModelCandidate("power_prob", 1.5)
    result = fn(cand, "2026-03-01")

    assert isinstance(result.n_fills, int)
    assert isinstance(result.adverse_pct, float)
    assert isinstance(result.pnl, float)
    assert result.date == "2026-03-01"


def test_real_execution_missing_file_raises_file_not_found():
    """Real path (no CH streaming) raises FileNotFoundError for missing .npz."""
    fn = build_probe_replay_fn(
        instrument="TMFD6",
        probe_factory=lambda: PassiveQuoteProbe(qty=1, max_pos=3),
        l2_data_dir="/nonexistent",
        latency_us=36000,
        tick_size=1.0,
        lot_size=1.0,
        allow_stub_execution=False,
        use_ch_streaming=False,
    )
    cand = QueueModelCandidate("power_prob", 1.5)
    with pytest.raises(FileNotFoundError):
        fn(cand, "2026-03-01")


def test_real_execution_unknown_queue_model_raises():
    """Unknown queue_model name raises ValueError."""
    events = _make_minimal_hftbt_events()
    mock_src = _MockChDataSource(events)
    fn = build_probe_replay_fn(
        instrument="TMFD6",
        probe_factory=lambda: PassiveQuoteProbe(qty=1, max_pos=3),
        l2_data_dir="/nonexistent",
        latency_us=100,
        tick_size=1.0,
        lot_size=1.0,
        allow_stub_execution=False,
        use_ch_streaming=True,
        ch_data_source=mock_src,
    )
    cand = QueueModelCandidate("nonexistent_model", 1.5)
    with pytest.raises(ValueError, match="Unknown queue model"):
        fn(cand, "2026-03-01")


# ---------------------------------------------------------------------------
# Backward-compat: ReplayNotReadyError is still importable
# ---------------------------------------------------------------------------


def test_replay_not_ready_error_importable():
    """ReplayNotReadyError remains importable for backward compatibility."""
    assert issubclass(ReplayNotReadyError, NotImplementedError)
