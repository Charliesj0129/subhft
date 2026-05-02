"""Verify queue-model exponent sensitivity after the DEPTH_CLEAR fix.

Runs the PassiveQuoteProbe on one day of real TMFD6 data across 7 queue
model candidates and prints fill counts + PnL. If the DEPTH_CLEAR fix is
working, exponent should meaningfully change fill counts.
"""
from __future__ import annotations

import sys

from hft_platform.backtest.ch_data_source import ChDataSource
from research.calibration.probe_strategy import PassiveQuoteProbe
from research.calibration.replay import build_probe_replay_fn
from research.calibration.sweep import QueueModelCandidate


def main() -> int:
    instrument = "TMFD6"
    date = "2026-04-10"

    ch = ChDataSource(ch_host="localhost", ch_port=8123)
    try:
        events = ch.load_day(instrument, date)
    except Exception as exc:  # noqa: BLE001
        print(f"ClickHouse load failed: {exc}")
        return 1
    print(f"Loaded {len(events):,} events for {instrument} {date}")

    replay = build_probe_replay_fn(
        instrument=instrument,
        probe_factory=lambda: PassiveQuoteProbe(qty=1, max_pos=3),
        l2_data_dir="/tmp/unused_when_ch_streaming",
        latency_us=50,
        tick_size=1.0,
        lot_size=1.0,
        allow_stub_execution=False,
        use_ch_streaming=True,
        ch_data_source=ch,
    )

    candidates = [
        QueueModelCandidate("power_prob", 0.5),
        QueueModelCandidate("power_prob", 1.0),
        QueueModelCandidate("power_prob", 1.5),
        QueueModelCandidate("power_prob", 2.0),
        QueueModelCandidate("power_prob", 2.5),
        QueueModelCandidate("power_prob", 3.0),
        QueueModelCandidate("log_prob", None),
    ]

    print(f"\n{'queue_model':<20} {'sim_fills':>10} {'pnl_pts':>12}")
    print("-" * 50)
    for cand in candidates:
        summary = replay(cand, date)
        print(f"{cand.label():<20} {summary.n_fills:>10,} {summary.pnl:>12.1f}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
