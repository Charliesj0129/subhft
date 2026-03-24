#!/usr/bin/env python3
"""Weekly reliability summary — sent Friday 14:00 via Telegram.

Usage:
    source .env && python scripts/weekly_summary.py [--dry-run]
"""
from __future__ import annotations

import argparse
import asyncio
import os
from datetime import date, timedelta

import structlog

logger = structlog.get_logger(__name__)


async def _gather_weekly_data() -> dict:
    """Gather week's data from ClickHouse."""
    try:
        from clickhouse_driver import Client

        host = os.environ.get("HFT_CLICKHOUSE_HOST", "localhost")
        client = Client(host=host)

        today = date.today()
        week_start = today - timedelta(days=today.weekday())

        rows = client.execute(
            "SELECT "
            "  count(DISTINCT event_date) as trading_days, "
            "  sum(realized_pnl) as total_pnl, "
            "  count(*) as total_trades "
            "FROM hft.fills "
            "WHERE event_date >= %(start)s AND event_date <= %(end)s",
            {"start": str(week_start), "end": str(today)},
        )

        daily_rows = client.execute(
            "SELECT event_date, sum(realized_pnl) as day_pnl "
            "FROM hft.fills "
            "WHERE event_date >= %(start)s AND event_date <= %(end)s "
            "GROUP BY event_date ORDER BY day_pnl",
            {"start": str(week_start), "end": str(today)},
        )

        trading_days = rows[0][0] if rows else 0
        total_pnl = rows[0][1] if rows else 0
        total_trades = rows[0][2] if rows else 0

        worst_day = daily_rows[0][1] // 10000 if daily_rows else 0
        best_day = daily_rows[-1][1] // 10000 if daily_rows else 0
        avg_trades = total_trades // max(trading_days, 1)

        recon_rows = client.execute(
            "SELECT countIf(status = 'MATCH'), count() "
            "FROM hft.reconciliation "
            "WHERE event_date >= %(start)s AND event_date <= %(end)s",
            {"start": str(week_start), "end": str(today)},
        )
        match_count = recon_rows[0][0] if recon_rows else 0
        total_count = recon_rows[0][1] if recon_rows else 0
        reconciliation_match = match_count == total_count if total_count > 0 else False

        return {
            "trading_days": trading_days,
            "total_pnl_ntd": total_pnl // 10000,
            "avg_trades": avg_trades,
            "best_day_ntd": best_day,
            "worst_day_ntd": worst_day,
            "reconciliation_match": reconciliation_match,
            "week_start": str(week_start),
            "week_end": str(today),
        }
    except Exception:  # noqa: BLE001
        logger.warning("weekly_data_query_failed", exc_info=True)
        return {
            "trading_days": 0,
            "total_pnl_ntd": 0,
            "avg_trades": 0,
            "best_day_ntd": 0,
            "worst_day_ntd": 0,
            "reconciliation_match": False,
            "week_start": "",
            "week_end": "",
        }


async def run_summary(dry_run: bool = False) -> None:
    """Gather weekly data and send Telegram notification."""
    data = await _gather_weekly_data()
    today = date.today()
    week_num = today.isocalendar()[1]

    logger.info("weekly_summary", **data)

    if not dry_run:
        try:
            from hft_platform.notifications import NotificationDispatcher
            from hft_platform.notifications.telegram import TelegramSender

            sender = TelegramSender(enabled=True)
            dispatcher = NotificationDispatcher(sender=sender)
            await dispatcher.notify_weekly_summary(
                week_label=f"W{week_num}",
                date_range=f"{data['week_start']} ~ {data['week_end']}",
                total_pnl_ntd=data["total_pnl_ntd"],
                trading_days=data["trading_days"],
                avg_trades=float(data["avg_trades"]),
                best_day_ntd=data["best_day_ntd"],
                worst_day_ntd=data["worst_day_ntd"],
                reconciliation_match=data["reconciliation_match"],
                halt_count=0,  # TODO: query from Prometheus
                reconnect_count=0,  # TODO: query from Prometheus
                latency_p95_avg_ms=0.0,
                rss_peak_gb=0.0,
                uptime_pct=100.0,
            )
            try:
                await sender.close()
            except Exception:  # noqa: BLE001
                pass
        except Exception:  # noqa: BLE001
            logger.warning("weekly_summary_dispatch_failed", exc_info=True)


def main() -> None:
    """Parse arguments and run the weekly summary."""
    parser = argparse.ArgumentParser(description="Weekly reliability summary")
    parser.add_argument("--dry-run", action="store_true", help="Gather data only, do not send")
    args = parser.parse_args()
    asyncio.run(run_summary(dry_run=args.dry_run))


if __name__ == "__main__":
    main()
