"""CLI commands for Transaction Cost Analysis."""

from __future__ import annotations

import argparse
import datetime as _dt
import os
import sys


def cmd_tca_daily(args: argparse.Namespace) -> None:
    """Query ClickHouse hft.fills and print daily TCA report table."""
    try:
        from clickhouse_driver import Client as CHClient
    except ImportError:
        print("clickhouse-driver not installed. Run: pip install clickhouse-driver")
        sys.exit(1)

    from hft_platform.tca.analyzer import TCAAnalyzer

    date_str: str = getattr(args, "date", None) or _dt.date.today().isoformat()

    ch_host = os.getenv("HFT_CLICKHOUSE_HOST", "localhost")
    ch_port = int(os.getenv("HFT_CLICKHOUSE_PORT", "9000"))

    try:
        ch_client = CHClient(host=ch_host, port=ch_port)
    except Exception as exc:
        print(f"Failed to connect to ClickHouse at {ch_host}:{ch_port}: {exc}")
        sys.exit(1)

    analyzer = TCAAnalyzer(ch_client=ch_client)
    reports = analyzer.daily_report(date_str)

    if not reports:
        print(f"No fills found for {date_str}.")
        return

    # Print table
    cols = [f"{'Strategy':<20}", f"{'Symbol':<10}", f"{'Trades':>7}", f"{'Volume':>8}"]
    cols += [f"{'Comm bps':>10}", f"{'Tax bps':>9}", f"{'Total bps':>10}"]
    header = " ".join(cols)
    print(f"\nTCA Daily Report — {date_str}")
    print("=" * len(header))
    print(header)
    print("-" * len(header))
    for r in reports:
        print(
            f"{r.strategy:<20} {r.symbol:<10} {r.trade_count:>7} {r.volume:>8} "
            f"{r.commission_bps_mean:>10.2f} {r.tax_bps_mean:>9.2f} {r.total_cost_bps_mean:>10.2f}"
        )
    print("-" * len(header))
    print(f"Total groups: {len(reports)}")
