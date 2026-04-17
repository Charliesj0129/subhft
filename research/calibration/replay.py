"""hftbacktest replay bridge for calibration.

Runs a probe strategy through hftbacktest with a given queue model candidate
and extracts DailyFillSummary for scoring.
"""
from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

from research.calibration.probe_strategy import PassiveQuoteProbe
from research.calibration.scoring import DailyFillSummary
from research.calibration.sweep import QueueModelCandidate


def build_probe_replay_fn(
    instrument: str,
    probe_factory: Callable[[], PassiveQuoteProbe],
    l2_data_dir: str | Path,
    latency_us: int,
    tick_size: float,
    lot_size: float,
) -> Callable[[QueueModelCandidate, str], DailyFillSummary]:
    """Build a replay function compatible with sweep_exponent().

    The returned fn takes (candidate, date) and returns DailyFillSummary.
    """
    l2_data_dir = Path(l2_data_dir)

    def replay(candidate: QueueModelCandidate, date: str) -> DailyFillSummary:
        data_path = l2_data_dir / f"{instrument}_{date}_l2.hftbt.npz"
        if not data_path.exists():
            raise FileNotFoundError(f"Missing L2 data: {data_path}")

        from hftbacktest import BacktestAsset, HashMapMarketDepthBacktest

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
        probe = probe_factory()

        n_fills = 0
        n_adverse = 0
        position = 0
        prev_mid: float | None = None
        pnl_points = 0.0

        while hbt.elapse(100_000_000) == 0:
            depth = hbt.depth(0)
            best_bid = depth.best_bid
            best_ask = depth.best_ask
            if best_bid <= 0 or best_ask <= 0 or best_ask < best_bid:
                continue
            mid = (best_bid + best_ask) / 2.0

            new_position = int(hbt.position(0))
            delta = new_position - position
            if delta != 0:
                n_fills += abs(delta)
                if prev_mid is not None:
                    if delta > 0 and mid < prev_mid:
                        n_adverse += abs(delta)
                    elif delta < 0 and mid > prev_mid:
                        n_adverse += abs(delta)
                position = new_position
            prev_mid = mid

            _action = probe.on_tick(
                bid=int(best_bid / tick_size),
                ask=int(best_ask / tick_size),
                mid=mid,
                position=position,
            )
            # Note: simplified version — full hftbacktest order submission not yet wired.
            # hbt.submit_buy_order/submit_sell_order integration is deferred to A7 production run.
            # This adapter currently only observes position changes driven by pre-existing orders.

        hbt.close()

        adverse_pct = n_adverse / max(n_fills, 1)
        return DailyFillSummary(
            date=date, n_fills=n_fills,
            adverse_pct=adverse_pct, pnl=pnl_points,
        )

    return replay
