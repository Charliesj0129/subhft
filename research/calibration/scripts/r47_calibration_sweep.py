"""R47-driven calibration sweep.

Replaces PassiveQuoteProbe with C17 (R47-structural, MakerEngine-protocol
variant for TMFD6). Runs hftbacktest with each queue-model candidate and
reports sim-fill vs live-fill metrics.
"""
from __future__ import annotations

import argparse
import os
import sys

import clickhouse_connect

from hft_platform.backtest.ch_data_source import ChDataSource
from research.alphas.c17_tmf_frontmonth_native_maker.impl import (
    C17Params,
    TmfFrontMonthMaker,
)
from research.calibration.replay import build_maker_strategy_replay_fn
from research.calibration.sweep import QueueModelCandidate


def fetch_live_fills(
    instrument: str,
    date: str,
    ch_host: str = "localhost",
    ch_port: int = 8123,
    ch_password: str = "",
) -> int:
    """Count live R47 fills for the instrument/date from hft.fills."""
    client = clickhouse_connect.get_client(
        host=ch_host,
        port=ch_port,
        username="default",
        password=ch_password or os.environ.get("CLICKHOUSE_PASSWORD", ""),
    )
    row = client.query(
        """
        SELECT count() FROM hft.fills
        WHERE symbol = {sym:String}
          AND toDate(toDateTime64(ts_exchange/1e9, 3)) = {date:Date}
        """,
        parameters={"sym": instrument, "date": date},
    ).result_rows
    return int(row[0][0]) if row else 0


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--instrument", default="TMFD6")
    parser.add_argument("--date", default="2026-04-10")
    parser.add_argument("--tick-size", type=float, default=1.0)
    parser.add_argument("--lot-size", type=float, default=1.0)
    parser.add_argument("--latency-us", type=int, default=50)
    parser.add_argument("--spread-threshold", type=int, default=5)
    parser.add_argument("--max-pos", type=int, default=1)
    args = parser.parse_args()

    ch = ChDataSource(ch_host="localhost", ch_port=8123)
    live_fills = fetch_live_fills(
        args.instrument, args.date,
        ch_password=ch.ch_password,
    )
    print(
        f"Live fills for {args.instrument} on {args.date}: {live_fills:,}"
    )

    params = C17Params(
        spread_threshold_pts=args.spread_threshold,
        max_pos=args.max_pos,
    )

    def factory() -> TmfFrontMonthMaker:
        strat = TmfFrontMonthMaker(params=params, active_symbol=args.instrument)
        return strat

    replay = build_maker_strategy_replay_fn(
        instrument=args.instrument,
        strategy_factory=factory,
        latency_us=args.latency_us,
        tick_size=args.tick_size,
        lot_size=args.lot_size,
        ch_data_source=ch,
        price_scale=1_000_000,
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

    print(
        f"\n{'queue_model':<20} {'sim_fills':>10} {'abs_err':>10} "
        f"{'pnl_pts':>12}"
    )
    print("-" * 60)
    best = None
    for cand in candidates:
        summary = replay(cand, args.date)
        err = abs(summary.n_fills - live_fills)
        print(
            f"{cand.label():<20} {summary.n_fills:>10,} {err:>10,} "
            f"{summary.pnl:>12.1f}"
        )
        if best is None or err < best[1]:
            best = (cand, err, summary)

    if best is not None:
        cand, err, summary = best
        print(
            f"\nBest: {cand.label()} (|sim-live|={err:,}, "
            f"sim_fills={summary.n_fills:,}, pnl={summary.pnl:.1f})"
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
