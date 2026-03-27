"""CLI command: hft feasibility report — aggregated feasibility scorecard."""
from __future__ import annotations

import argparse
import datetime as _dt
import os
import sys


def cmd_feasibility_report(args: argparse.Namespace) -> None:
    try:
        from clickhouse_driver import Client as CHClient
    except ImportError:
        print("clickhouse-driver not installed. Run: pip install clickhouse-driver")
        sys.exit(1)

    from hft_platform.analytics.queries import (
        query_daily_pnl,
        query_fill_quality,
        query_liquidity_gate_stats,
        query_slippage_distribution,
    )

    date_str: str = getattr(args, "date", None) or _dt.date.today().isoformat()

    ch_host = os.getenv("HFT_CLICKHOUSE_HOST", "localhost")
    ch_port = int(os.getenv("HFT_CLICKHOUSE_PORT", "9000"))

    try:
        ch = CHClient(host=ch_host, port=ch_port)
    except Exception as exc:
        print(f"Failed to connect to ClickHouse: {exc}")
        sys.exit(1)

    print(f"=== Feasibility Report: {date_str} ===\n")

    pnl = query_daily_pnl(ch, date_str)
    print("--- Daily PnL ---")
    if pnl:
        for row in pnl:
            print(f"  {row['strategy']:20s} {row['symbol']:10s} "
                  f"fills={row['fill_count']} qty={row['total_qty']} "
                  f"cost={row['total_cost_ntd']:.0f} NTD")
    else:
        print("  No fills found.")

    slip = query_slippage_distribution(ch, date_str)
    print("\n--- Slippage Distribution ---")
    if slip:
        for row in slip:
            print(f"  {row['symbol']:10s} n={row['count']} "
                  f"avg={row['avg_ticks']:.1f} ticks P95={row['p95_ticks']:.1f} ticks")
    else:
        print("  No slippage records found.")

    fq = query_fill_quality(ch, date_str)
    print("\n--- Fill Quality ---")
    if fq:
        for row in fq:
            print(f"  {row['strategy']:20s} {row['symbol']:10s} "
                  f"n={row['count']} avg={row['avg_latency_ms']:.1f}ms "
                  f"P95={row['p95_latency_ms']:.1f}ms")
    else:
        print("  No fill quality data.")

    lg = query_liquidity_gate_stats(ch, date_str)
    print("\n--- Liquidity Gate ---")
    if lg:
        for row in lg:
            reject_pct = (row['rejected'] / row['total'] * 100) if row['total'] > 0 else 0
            print(f"  {row['symbol']:10s} "
                  f"passed={row['passed']} rejected={row['rejected']} "
                  f"({reject_pct:.1f}% rejected)")
    else:
        print("  No gate events.")

    print("\n=== End Report ===")
