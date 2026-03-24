#!/usr/bin/env python3
"""Post-market shadow trading daily analysis.

Schedule: cron at 14:00 weekdays (after shadow session ends)
Queries hft.shadow_orders, computes simulated PnL, sends Telegram report.

Usage:
    source .env && python scripts/shadow_daily_report.py [--dry-run] [--date 2026-04-22]
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from datetime import date

import structlog

logger = structlog.get_logger(__name__)

# Default point values per symbol (NTD per 1 point)
DEFAULT_POINT_VALUES: dict[str, int] = {"TMF": 10, "MXF": 50}


def compute_simulated_pnl(
    orders: list[dict],
    point_values: dict[str, int],
    slippage_ticks: int = 1,
    tick_size: int = 1,
) -> int:
    """Compute simulated PnL from shadow orders with slippage.

    Uses mid_price +/- slippage as assumed fill price.
    Prices are scaled integers (x10000). Returns PnL in NTD (integer).

    Args:
        orders: List of order dicts with keys: side, mid_price, qty, symbol.
        point_values: Map of symbol -> NTD per 1 point (e.g. TMF=10, MXF=50).
        slippage_ticks: Number of ticks of slippage to apply per fill.
        tick_size: Tick size in scaled-int units (x10000). Default 1 = 0.0001 point.

    Returns:
        Realized PnL in NTD as an integer.
    """
    if not orders:
        return 0

    # Open position stacks per symbol: list of {"price": int, "qty": int}
    positions: dict[str, list[dict]] = {}
    realized_pnl = 0
    slippage = slippage_ticks * tick_size

    for order in orders:
        symbol = order.get("symbol", "")
        side = order.get("side", "")
        mid = order.get("mid_price", 0)
        qty = order.get("qty", 1)
        pv = point_values.get(symbol, 10)

        # Apply slippage: buyer pays more, seller receives less
        if side == "BUY":
            fill_price = mid + slippage
        elif side == "SELL":
            fill_price = mid - slippage
        else:
            continue

        if symbol not in positions:
            positions[symbol] = []

        pos = positions[symbol]
        if side == "BUY":
            pos.append({"price": fill_price, "qty": qty})
        elif side == "SELL" and pos:
            entry = pos.pop(0)
            matched_qty = min(qty, entry["qty"])
            # PnL = (exit_price - entry_price) * qty * point_value / scale_factor
            pnl_scaled = (fill_price - entry["price"]) * matched_qty
            realized_pnl += pnl_scaled * pv // 10000

    return realized_pnl


def count_by_side(orders: list[dict]) -> tuple[int, int]:
    """Count buy and sell orders.

    Args:
        orders: List of order dicts with a "side" key.

    Returns:
        Tuple of (buy_count, sell_count).
    """
    buys = sum(1 for o in orders if o.get("side") == "BUY")
    sells = sum(1 for o in orders if o.get("side") == "SELL")
    return buys, sells


async def query_shadow_orders(target_date: str) -> list[dict]:
    """Query ClickHouse for shadow orders on a given date.

    Args:
        target_date: ISO date string, e.g. "2026-04-22".

    Returns:
        List of order dicts; empty on error.
    """
    try:
        from clickhouse_driver import Client  # type: ignore[import-untyped]

        host = os.environ.get("HFT_CLICKHOUSE_HOST", "localhost")
        client = Client(host=host)
        rows = client.execute(
            "SELECT ts_ns, strategy_id, symbol, side, price, qty, intent_type, intent_id "
            "FROM hft.shadow_orders "
            "WHERE toDate(inserted_at) = %(date)s "
            "ORDER BY ts_ns",
            {"date": target_date},
        )
        return [
            {
                "ts_ns": r[0],
                "strategy_id": r[1],
                "symbol": r[2],
                "side": r[3],
                "price": r[4],
                "qty": r[5],
                "intent_type": r[6],
                "intent_id": r[7],
                # Use order price as mid_price proxy since shadow orders capture intent price
                "mid_price": r[4],
            }
            for r in rows
        ]
    except Exception:
        logger.warning("shadow_orders_query_failed", exc_info=True)
        return []


def save_evidence_pack(target_date: str, data: dict) -> None:
    """Save daily evidence pack to outputs directory.

    Args:
        target_date: ISO date string, e.g. "2026-04-22".
        data: Report data dict to serialize as JSON.
    """
    date_compact = target_date.replace("-", "")
    output_dir = f"outputs/production_rollout/phase2/{date_compact}"
    os.makedirs(output_dir, exist_ok=True)
    path = os.path.join(output_dir, "shadow_daily_report.json")
    with open(path, "w") as f:
        json.dump(data, f, indent=2, default=str)
    logger.info("evidence_pack_saved", path=path)


async def run_analysis(target_date: str, dry_run: bool = False) -> None:
    """Run shadow daily analysis for the given date.

    Queries ClickHouse, computes metrics, optionally saves evidence pack
    and sends a Telegram notification.

    Args:
        target_date: ISO date string, e.g. "2026-04-22".
        dry_run: If True, skip file I/O and Telegram notification.
    """
    logger.info("shadow_daily_report_start", date=target_date, dry_run=dry_run)

    orders = await query_shadow_orders(target_date)
    buys, sells = count_by_side(orders)
    pnl = compute_simulated_pnl(orders, DEFAULT_POINT_VALUES)

    report_data: dict = {
        "date": target_date,
        "intent_count": len(orders),
        "buys": buys,
        "sells": sells,
        "simulated_pnl_ntd": pnl,
    }

    logger.info("shadow_daily_analysis", **report_data)

    if dry_run:
        logger.info("shadow_daily_report_dry_run_complete", **report_data)
        return

    save_evidence_pack(target_date, report_data)

    try:
        from hft_platform.notifications import NotificationDispatcher, TelegramSender

        sender = TelegramSender(enabled=True)
        dispatcher = NotificationDispatcher(sender=sender)
        await dispatcher.notify_shadow_daily_report(
            date_str=target_date,
            intent_count=len(orders),
            buys=buys,
            sells=sells,
            simulated_pnl_ntd=pnl,
            latency_p50_ms=0.0,
            latency_p95_ms=0.0,
            latency_p99_ms=0.0,
            reconnect_count=0,
            queue_peak_pct=0,
            rss_gb=0.0,
            storm_guard_state="NORMAL",
        )
        try:
            await sender.close()
        except Exception:
            pass
    except Exception:
        logger.warning("shadow_daily_report_notification_failed", exc_info=True)


def main() -> None:
    """Entry point for shadow daily analysis CLI."""
    parser = argparse.ArgumentParser(description="Post-market shadow trading daily analysis")
    parser.add_argument("--dry-run", action="store_true", help="Skip file I/O and Telegram")
    parser.add_argument(
        "--date",
        default=str(date.today()),
        help="Analysis date in ISO format (default: today)",
    )
    args = parser.parse_args()

    # Skip on non-trading days unless date is explicitly provided
    if args.date == str(date.today()):
        try:
            from hft_platform.core.market_calendar import get_calendar

            cal = get_calendar()
            if not cal.is_trading_day():
                logger.info("not_trading_day_skipping", date=args.date)
                sys.exit(0)
        except Exception:
            pass  # Market calendar unavailable — proceed anyway

    asyncio.run(run_analysis(target_date=args.date, dry_run=args.dry_run))


if __name__ == "__main__":
    main()
