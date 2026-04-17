"""hftbacktest replay bridge for calibration.

STATUS: STUB — order submission NOT wired to hftbacktest yet.

Currently returns DailyFillSummary with always-zero fills because neither
buy nor sell orders are submitted. Before calling `sweep_exponent` with the
returned function on real data, wire:
  - hbt.submit_buy_order / submit_sell_order using ProbeAction outputs
  - Fill event tracking (position delta + average price)
  - Order ID management and cancel-repost logic

Set allow_stub_execution=True to override the guard (e.g., for testing the
pipeline plumbing without expecting real calibration signal).
"""
from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

from research.calibration.probe_strategy import PassiveQuoteProbe
from research.calibration.scoring import DailyFillSummary
from research.calibration.sweep import QueueModelCandidate


class ReplayNotReadyError(NotImplementedError):
    """Raised when the stub replay is invoked without explicit override."""


def build_probe_replay_fn(
    instrument: str,
    probe_factory: Callable[[], PassiveQuoteProbe],
    l2_data_dir: str | Path,
    latency_us: int,
    tick_size: float,
    lot_size: float,
    allow_stub_execution: bool = False,
) -> Callable[[QueueModelCandidate, str], DailyFillSummary]:
    """Build a replay function compatible with sweep_exponent().

    The returned fn takes (candidate, date) and returns DailyFillSummary.

    WARNING: Order submission to hftbacktest is not yet wired. The returned
    function will raise ReplayNotReadyError unless allow_stub_execution=True.
    Even with stub execution enabled, the result will have n_fills=0 and
    pnl=0.0 on every real data file — the scoring will be meaningless.
    """
    # Fail-fast hftbacktest import (moved outside closure so import errors surface at
    # build time, not inside the replay loop on first call).
    from hftbacktest import BacktestAsset, HashMapMarketDepthBacktest

    l2_data_dir = Path(l2_data_dir)

    def replay(candidate: QueueModelCandidate, date: str) -> DailyFillSummary:
        data_path = l2_data_dir / f"{instrument}_{date}_l2.hftbt.npz"
        if not data_path.exists():
            raise FileNotFoundError(f"Missing L2 data: {data_path}")

        if not allow_stub_execution:
            raise ReplayNotReadyError(
                "replay.py is a stub — hftbacktest order submission not wired. "
                "Set allow_stub_execution=True to run the pipeline in stub mode "
                "(all fills will be zero). Real calibration requires wiring "
                "hbt.submit_buy_order/submit_sell_order with probe output."
            )

        asset = BacktestAsset()
        asset.linear_asset(1.0)
        asset.tick_size(tick_size)
        asset.lot_size(lot_size)
        asset.data([str(data_path)])
        asset.constant_order_latency(latency_us * 1000, latency_us * 1000)
        asset.no_partial_fill_exchange()

        if candidate.queue_model == "power_prob":
            asset.power_prob_queue_model(candidate.exponent)
        elif candidate.queue_model == "power_prob2":
            asset.power_prob_queue_model2(candidate.exponent)
        elif candidate.queue_model == "power_prob3":
            asset.power_prob_queue_model3(candidate.exponent)
        elif candidate.queue_model == "log_prob":
            asset.log_prob_queue_model()
        else:
            raise ValueError(f"Unknown queue model: {candidate.queue_model}")

        hbt = HashMapMarketDepthBacktest([asset])
        _probe = probe_factory()

        # STUB: loop through data without submitting orders
        # TODO: wire hbt.submit_buy_order/submit_sell_order with probe.on_tick output
        while hbt.elapse(100_000_000) == 0:
            pass

        hbt.close()

        return DailyFillSummary(date=date, n_fills=0, adverse_pct=0.0, pnl=0.0)

    return replay
