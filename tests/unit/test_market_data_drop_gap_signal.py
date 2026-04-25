"""Regression tests for D-Q1: ``_enqueue_raw`` GAP signaling on persistent drops.

Bug pathology
-------------
The original implementation in ``services/market_data.py`` used:

    if fe is not None and self._raw_consecutive_drops == self._raw_drop_degrade_threshold:
        fe.mark_gap_all()

The ``==`` (exact equality) means ``mark_gap_all`` fires only on the EXACT
N-th consecutive drop (where N == threshold). Drops past that boundary
silently corrupt rolling features (OFI EMA, autocovariance, depth momentum)
without GAP propagating to downstream consumers. The same block at lines
1703/1708 of the file uses ``>=`` for StormGuard escalation — semantic
asymmetry that masks persistent-drop signal.

Defense-in-depth context: ``TickDispatcher.set_on_drop_callback`` (H12 wiring
at ``market_data.py:280``) provides a redundant per-drop GAP signal via the
Shioaji path. But for fubon / dispatcher-less configurations, ``_enqueue_raw``
is the only line of defense — and the ``==`` typo silenced it.

Tests below verify ``mark_gap_all`` fires on EVERY drop at-or-above threshold,
not just on the threshold-equality boundary, and resets correctly on success.
"""

from __future__ import annotations

import asyncio
import os
import tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest


def _make_symbols_config() -> tuple[tempfile.TemporaryDirectory, Path]:
    tmp = tempfile.TemporaryDirectory()
    cfg = Path(tmp.name) / "symbols.yaml"
    cfg.write_text("symbols:\n  - code: '2330'\n    exchange: 'TSE'\n    price_scale: 10000\n")
    return tmp, cfg


def _make_service():
    """Create a MarketDataService with feature engine disabled and mocked deps."""
    from hft_platform.engine.event_bus import RingBufferBus
    from hft_platform.services.market_data import MarketDataService

    tmp, cfg = _make_symbols_config()
    env = {
        "HFT_FEATURE_ENGINE_ENABLED": "0",
        "SYMBOLS_CONFIG": str(cfg),
        "HFT_MONITOR_LIVE_ENABLED": "0",
    }

    bus = MagicMock(spec=RingBufferBus)
    raw_queue = asyncio.Queue(maxsize=100)
    client = MagicMock()
    client.login = MagicMock(return_value=None)
    client.validate_symbols = MagicMock(return_value=None)
    client.fetch_snapshots = MagicMock(return_value=[])
    client.subscribe_basket = MagicMock(return_value=None)

    with patch.dict(os.environ, env):
        svc = MarketDataService(bus, raw_queue, client)
    svc._tmp = tmp  # keep TemporaryDirectory alive
    return svc


def _force_drop_state(svc) -> None:
    """Configure svc so that calls to ``_enqueue_raw`` always hit ``QueueFull``."""
    svc.raw_queue = asyncio.Queue(maxsize=1)
    svc.raw_queue.put_nowait(("seed", "seed"))  # block any further put_nowait
    svc._storm_guard = None
    # Push StormGuard thresholds far above what the test will reach so they
    # don't interfere with mark_gap_all observation.
    svc._raw_drop_halt_threshold = 10_000
    svc._raw_drop_window_threshold = 10_000
    svc._raw_drop_window_count = 0.0
    svc._raw_drop_window_last_ns = 0


def test_mark_gap_all_fires_on_every_drop_past_threshold():
    """GAP must fire on the threshold-th drop AND every subsequent drop.

    With the ``==`` bug this would fire only once (drop #3); with ``>=``
    it fires on drops 3, 4, and 5 — three calls total.
    """
    svc = _make_service()
    _force_drop_state(svc)

    fe = MagicMock()
    svc.feature_engine = fe
    svc._raw_drop_degrade_threshold = 3
    svc._raw_consecutive_drops = 0

    # Simulate 5 consecutive drops.
    for _ in range(5):
        svc._enqueue_raw("TSE", {"code": "2330"})

    # Drops 1, 2 are below threshold (no fire).
    # Drops 3, 4, 5 are >= threshold (must fire each time).
    assert fe.mark_gap_all.call_count == 3, (
        f"Expected mark_gap_all to fire on every drop >= threshold "
        f"(drops 3, 4, 5 => 3 calls); got {fe.mark_gap_all.call_count}. "
        "If this is 1, the `==` bug regression has returned."
    )


def test_mark_gap_all_not_fired_below_threshold():
    """Drops 1 and 2 with threshold=3 must NOT trigger ``mark_gap_all``."""
    svc = _make_service()
    _force_drop_state(svc)

    fe = MagicMock()
    svc.feature_engine = fe
    svc._raw_drop_degrade_threshold = 3
    svc._raw_consecutive_drops = 0

    svc._enqueue_raw("TSE", {"code": "2330"})  # drop #1
    svc._enqueue_raw("TSE", {"code": "2330"})  # drop #2

    assert fe.mark_gap_all.call_count == 0, (
        "mark_gap_all must not fire below threshold "
        f"(threshold=3, drops=2); got {fe.mark_gap_all.call_count}"
    )
    assert svc._raw_consecutive_drops == 2


def test_mark_gap_all_resets_on_success():
    """After a successful enqueue, ``_raw_consecutive_drops`` resets to 0,
    and the next pass-through-threshold fires GAP again.
    """
    svc = _make_service()
    _force_drop_state(svc)

    fe = MagicMock()
    svc.feature_engine = fe
    svc._raw_drop_degrade_threshold = 2
    svc._raw_consecutive_drops = 0

    # First burst: 3 drops -> mark_gap_all fires twice (drops 2 and 3).
    svc._enqueue_raw("TSE", {"code": "2330"})  # drop #1
    svc._enqueue_raw("TSE", {"code": "2330"})  # drop #2 -> fire
    svc._enqueue_raw("TSE", {"code": "2330"})  # drop #3 -> fire
    assert fe.mark_gap_all.call_count == 2
    assert svc._raw_consecutive_drops == 3

    # Drain the queue and successfully enqueue -> resets counter.
    svc.raw_queue.get_nowait()
    svc._enqueue_raw("TSE", {"code": "2330"})  # success
    assert svc._raw_consecutive_drops == 0

    # Re-saturate the queue and run another burst.
    # raw_queue still has the new item we just put. Drops will resume.
    svc._enqueue_raw("TSE", {"code": "2330"})  # drop #1 (post-reset)
    assert fe.mark_gap_all.call_count == 2  # no new fire yet
    svc._enqueue_raw("TSE", {"code": "2330"})  # drop #2 -> fire
    assert fe.mark_gap_all.call_count == 3, (
        "After reset and re-crossing threshold, mark_gap_all must fire again. "
        f"Expected 3 total calls (2 pre-reset + 1 post-reset); got {fe.mark_gap_all.call_count}"
    )
