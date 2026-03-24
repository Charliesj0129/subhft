#!/usr/bin/env python3
"""Daily post-market 3-way PnL/position reconciliation.

Compares broker (Shioaji API), platform (ClickHouse pnl_snapshots), and
ClickHouse (orders table) views of end-of-day positions and PnL.

Usage:
    uv run python scripts/daily_reconcile.py [--dry-run] [--date YYYY-MM-DD]
"""
from __future__ import annotations

import argparse
import asyncio
import datetime as dt
import json
import os
import sys
from typing import Any

import structlog

# Must add src to path when running as a script
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from hft_platform.core import timebase
from hft_platform.core.market_calendar import get_calendar

logger = structlog.get_logger("reconcile.daily")

# Tolerance in scaled-int units (x10000): 100_000 = 10 NTD
_PNL_TOLERANCE_SCALED: int = 100_000

# ClickHouse connection defaults (override via env)
_CH_HOST = os.environ.get("HFT_CLICKHOUSE_HOST", "localhost")
_CH_PORT = int(os.environ.get("HFT_CLICKHOUSE_PORT", "8123"))
_CH_USER = os.environ.get("HFT_CLICKHOUSE_USER", "default")
_CH_PASSWORD = os.environ.get("HFT_CLICKHOUSE_PASSWORD", "")


# ---------------------------------------------------------------------------
# Pure comparison logic (unit-testable, no I/O)
# ---------------------------------------------------------------------------


def compare_positions(
    broker: dict[str, dict[str, Any]],
    platform: dict[str, dict[str, Any]],
    ch: dict[str, dict[str, Any]],
    tolerance: int = _PNL_TOLERANCE_SCALED,
) -> tuple[bool, list[dict[str, Any]]]:
    """Compare three views of position data and return match status plus mismatches.

    Each source is a dict keyed by symbol with values containing at minimum:
        - ``qty``: int — net quantity (negative = short)
        - ``pnl``: int — realized PnL in scaled-int units (x10000)

    Args:
        broker: Positions from broker API.
        platform: Positions from platform ClickHouse pnl_snapshots.
        ch: Positions derived from ClickHouse order/fill records.
        tolerance: Absolute tolerance in scaled-int units for PnL comparison.

    Returns:
        Tuple of (is_match, mismatches).  ``mismatches`` is an empty list when
        ``is_match`` is True.
    """
    all_symbols: set[str] = set(broker) | set(platform) | set(ch)

    # Empty positions on all sides is an explicit match
    if not all_symbols:
        return True, []

    mismatches: list[dict[str, Any]] = []

    for symbol in sorted(all_symbols):
        b = broker.get(symbol, {"qty": 0, "pnl": 0})
        p = platform.get(symbol, {"qty": 0, "pnl": 0})
        c = ch.get(symbol, {"qty": 0, "pnl": 0})

        b_qty: int = b.get("qty", 0)
        p_qty: int = p.get("qty", 0)
        c_qty: int = c.get("qty", 0)

        b_pnl: int = b.get("pnl", 0)
        p_pnl: int = p.get("pnl", 0)
        c_pnl: int = c.get("pnl", 0)

        qty_ok = (b_qty == p_qty == c_qty)
        pnl_ok = (
            abs(b_pnl - p_pnl) <= tolerance
            and abs(b_pnl - c_pnl) <= tolerance
            and abs(p_pnl - c_pnl) <= tolerance
        )

        if not qty_ok or not pnl_ok:
            mismatches.append(
                {
                    "symbol": symbol,
                    "broker_qty": b_qty,
                    "platform_qty": p_qty,
                    "ch_qty": c_qty,
                    "broker_pnl": b_pnl,
                    "platform_pnl": p_pnl,
                    "ch_pnl": c_pnl,
                    "qty_match": qty_ok,
                    "pnl_match": pnl_ok,
                }
            )

    return len(mismatches) == 0, mismatches


# ---------------------------------------------------------------------------
# ClickHouse helpers
# ---------------------------------------------------------------------------


def _get_ch_client() -> Any:
    """Return a connected clickhouse_connect client."""
    import clickhouse_connect  # type: ignore[import]

    return clickhouse_connect.get_client(
        host=_CH_HOST,
        port=_CH_PORT,
        username=_CH_USER,
        password=_CH_PASSWORD,
    )


def _query_platform_positions(
    client: Any,
    date: dt.date,
) -> dict[str, dict[str, Any]]:
    """Query latest pnl_snapshots for each symbol on *date*.

    Returns a dict keyed by symbol with ``qty`` and ``pnl`` (scaled int).
    """
    date_ts_start_ns = int(
        dt.datetime(date.year, date.month, date.day, tzinfo=dt.timezone.utc).timestamp() * 1_000_000_000
    )
    date_ts_end_ns = date_ts_start_ns + 86_400 * 1_000_000_000

    sql = """
        SELECT
            symbol,
            argMax(net_qty, snapshot_ts)           AS qty,
            argMax(realized_pnl_scaled, snapshot_ts) AS pnl
        FROM hft.pnl_snapshots
        WHERE snapshot_ts >= {ts_start:Int64}
          AND snapshot_ts < {ts_end:Int64}
        GROUP BY symbol
    """
    try:
        result = client.query(
            sql,
            parameters={"ts_start": date_ts_start_ns, "ts_end": date_ts_end_ns},
        )
        positions: dict[str, dict[str, Any]] = {}
        for row in result.result_rows:
            symbol, qty, pnl = row
            positions[str(symbol)] = {"qty": int(qty), "pnl": int(pnl)}
        logger.info("ch_platform_positions_queried", symbol_count=len(positions))
        return positions
    except Exception as exc:
        logger.warning("ch_platform_positions_query_failed", error=str(exc))
        return {}


def _query_ch_order_positions(
    client: Any,
    date: dt.date,
) -> dict[str, dict[str, Any]]:
    """Derive positions from ClickHouse orders table for *date*.

    Returns a dict keyed by symbol with ``qty`` (net signed) and ``pnl`` = 0
    (order table does not carry realized PnL; callers should treat this as
    a qty-only cross-check).
    """
    date_str = date.isoformat()
    sql = """
        SELECT
            symbol,
            sumIf(qty,  side = 'BUY')  - sumIf(qty, side = 'SELL') AS net_qty
        FROM hft.orders
        WHERE toDate(ts_ns / 1000000000) = {date:String}
          AND status = 'FILLED'
        GROUP BY symbol
    """
    try:
        result = client.query(sql, parameters={"date": date_str})
        positions: dict[str, dict[str, Any]] = {}
        for row in result.result_rows:
            symbol, net_qty = row
            positions[str(symbol)] = {"qty": int(net_qty), "pnl": 0}
        logger.info("ch_order_positions_queried", symbol_count=len(positions))
        return positions
    except Exception as exc:
        logger.warning("ch_order_positions_query_failed", error=str(exc))
        return {}


def _write_reconciliation_result(
    client: Any,
    date: dt.date,
    is_match: bool,
    broker_pnl: int,
    platform_pnl: int,
    ch_pnl: int,
    details: str,
) -> None:
    """Insert a single row into hft.reconciliation."""
    status = "MATCH" if is_match else "MISMATCH"
    ts_ns = timebase.now_ns()

    data = [[date.isoformat(), status, broker_pnl, platform_pnl, ch_pnl, details, ts_ns]]
    try:
        client.insert(
            "hft.reconciliation",
            data,
            column_names=["event_date", "status", "broker_pnl", "platform_pnl", "ch_pnl", "details", "timestamp_ns"],
        )
        logger.info(
            "reconciliation_written",
            date=date.isoformat(),
            status=status,
            broker_pnl=broker_pnl,
            platform_pnl=platform_pnl,
            ch_pnl=ch_pnl,
        )
    except Exception as exc:
        logger.error("reconciliation_write_failed", error=str(exc))


# ---------------------------------------------------------------------------
# Broker query
# ---------------------------------------------------------------------------


def _query_broker_positions() -> dict[str, dict[str, Any]]:
    """Query live broker positions via Shioaji API.

    Returns a dict keyed by symbol with ``qty`` and ``pnl`` fields.
    Falls back to empty dict on any error — broker may be offline post-market.
    """
    try:
        from hft_platform.feed_adapter.broker_registry import get_broker_factory  # type: ignore[import]

        factory = get_broker_factory(os.environ.get("HFT_BROKER", "shioaji"))
        client = factory()
        client.login()

        positions_raw = client.account_gateway.get_positions()
        positions: dict[str, dict[str, Any]] = {}

        for pos in positions_raw:
            # Shioaji Position object has: code, quantity, price (avg), pnl
            symbol = str(getattr(pos, "code", "") or getattr(pos, "symbol", ""))
            if not symbol:
                continue
            qty: int = int(getattr(pos, "quantity", 0))
            # pnl from broker is typically in NTD; scale to x10000 for comparison
            broker_pnl_raw = getattr(pos, "pnl", 0) or 0
            pnl_scaled: int = int(float(broker_pnl_raw) * 10_000)
            positions[symbol] = {"qty": qty, "pnl": pnl_scaled}

        logger.info("broker_positions_fetched", count=len(positions))
        client.logout()
        return positions

    except ImportError as exc:
        logger.warning("broker_sdk_unavailable", error=str(exc))
        return {}
    except Exception as exc:
        logger.warning("broker_positions_fetch_failed", error=str(exc))
        return {}


# ---------------------------------------------------------------------------
# Main async entrypoint
# ---------------------------------------------------------------------------


async def run_reconciliation(
    date: dt.date,
    dry_run: bool,
) -> int:
    """Run the 3-way reconciliation for *date*.

    Returns exit code: 0 = success/match, 1 = mismatch, 2 = error.
    """
    log = logger.bind(date=date.isoformat(), dry_run=dry_run)
    log.info("reconciliation_start")

    # --- Fetch data ---
    broker_positions = _query_broker_positions()

    ch_client: Any = None
    try:
        ch_client = _get_ch_client()
    except Exception as exc:
        log.error("ch_connect_failed", error=str(exc))
        return 2

    platform_positions = _query_platform_positions(ch_client, date)
    ch_order_positions = _query_ch_order_positions(ch_client, date)

    # --- Aggregate scalar PnL for notification ---
    broker_pnl_total: int = sum(v["pnl"] for v in broker_positions.values())
    platform_pnl_total: int = sum(v["pnl"] for v in platform_positions.values())
    ch_pnl_total: int = sum(v["pnl"] for v in ch_order_positions.values())

    # --- Compare (ch_order_positions is qty-only; use platform for PnL) ---
    # Build unified ch view: qty from orders, pnl from platform snapshot
    ch_unified: dict[str, dict[str, Any]] = {}
    all_syms = set(platform_positions) | set(ch_order_positions)
    for sym in all_syms:
        ch_unified[sym] = {
            "qty": ch_order_positions.get(sym, {}).get("qty", 0),
            "pnl": platform_positions.get(sym, {}).get("pnl", 0),
        }

    is_match, mismatches = compare_positions(
        broker=broker_positions,
        platform=platform_positions,
        ch=ch_unified,
    )

    details_str = json.dumps(mismatches) if mismatches else ""

    log.info(
        "reconciliation_result",
        is_match=is_match,
        mismatch_count=len(mismatches),
        broker_pnl=broker_pnl_total,
        platform_pnl=platform_pnl_total,
        ch_pnl=ch_pnl_total,
    )

    # --- Persist result ---
    if not dry_run:
        _write_reconciliation_result(
            client=ch_client,
            date=date,
            is_match=is_match,
            broker_pnl=broker_pnl_total,
            platform_pnl=platform_pnl_total,
            ch_pnl=ch_pnl_total,
            details=details_str,
        )

    # --- Notifications ---
    telegram_enabled = bool(
        os.environ.get("HFT_TELEGRAM_BOT_TOKEN")
        and os.environ.get("HFT_TELEGRAM_CHAT_ID")
    )

    if not dry_run and telegram_enabled:
        from hft_platform.notifications.dispatcher import NotificationDispatcher
        from hft_platform.notifications.telegram import TelegramSender

        sender = TelegramSender(enabled=True)
        dispatcher = NotificationDispatcher(sender=sender)

        try:
            if not is_match:
                # Report each mismatch individually so operator sees all symbols
                for mm in mismatches:
                    await dispatcher.notify_reconciliation_mismatch(
                        platform_pnl=mm["platform_pnl"],
                        broker_pnl=mm["broker_pnl"],
                        ch_pnl=mm["ch_pnl"],
                    )
                    log.warning(
                        "mismatch_notified",
                        symbol=mm["symbol"],
                        broker_qty=mm["broker_qty"],
                        platform_qty=mm["platform_qty"],
                        ch_qty=mm["ch_qty"],
                    )
            else:
                await dispatcher.notify_daily_report(
                    date_str=date.isoformat(),
                    pnl_ntd=platform_pnl_total // 10_000,
                    buys=0,
                    sells=0,
                    fills=0,
                    position_status="FLAT" if not platform_positions else "OPEN",
                    reconciliation_status="OK",
                    latency_p95_ms=0.0,
                    reconnect_count=0,
                    storm_guard_state="NORMAL",
                    memory_gb=0.0,
                    memory_max_gb=0.0,
                )
        finally:
            await sender.close()

    elif dry_run:
        log.info("dry_run_skip_notifications")

    return 0 if is_match else 1


def main() -> None:
    """CLI entrypoint."""
    parser = argparse.ArgumentParser(description="Daily post-market reconciliation")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Skip DB writes and Telegram notifications (compare only)",
    )
    parser.add_argument(
        "--date",
        default=None,
        help="Date to reconcile (YYYY-MM-DD). Defaults to today.",
    )
    args = parser.parse_args()

    if args.date:
        target_date = dt.date.fromisoformat(args.date)
    else:
        # Use timebase for consistency; convert ns epoch to date
        ts_ns = timebase.now_ns()
        ts_s = ts_ns / 1_000_000_000
        target_date = dt.datetime.fromtimestamp(ts_s, tz=dt.timezone.utc).date()

    calendar = get_calendar()
    if not calendar.is_trading_day(target_date):
        logger.info(
            "non_trading_day_skip",
            date=target_date.isoformat(),
        )
        sys.exit(0)

    exit_code = asyncio.run(run_reconciliation(date=target_date, dry_run=args.dry_run))
    sys.exit(exit_code)


if __name__ == "__main__":
    main()
