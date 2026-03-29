"""Report pipeline: date resolution, orchestration, and CLI entry point.

Environment variables:
    HFT_REPORT_ENABLED: Set to "1" to allow pipeline execution in production.
                        Without this (and without --dry-run / --debug), the
                        pipeline exits early with a warning.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import structlog

from hft_platform.reports.models import ComposedReport

__all__ = ["resolve_trading_date", "build_report", "run_pipeline", "main"]

_log = structlog.get_logger(__name__)

_TZ = ZoneInfo("Asia/Taipei")


def resolve_trading_date(session: str, *, now: datetime | None = None) -> str:
    """Return the trading date string (YYYY-MM-DD) for the given session.

    Day session:
        Always returns today's date in Asia/Taipei.

    Night session:
        - now.hour < 15  → yesterday  (session is still running from prior day)
        - now.hour >= 15 → today      (session just opened)

    Args:
        session: "day" or "night".
        now:     Override for the current time (must be timezone-aware or
                 will be treated as Asia/Taipei wall-clock). If None, uses
                 the real current time.

    Returns:
        ISO date string, e.g. "2026-03-27".

    Raises:
        ValueError: If session is not "day" or "night".
    """
    if session not in ("day", "night"):
        raise ValueError(f"Unknown session {session!r}; expected 'day' or 'night'")

    if now is None:
        now = datetime.now(_TZ)
    elif now.tzinfo is None:
        # Treat naive datetime as Asia/Taipei wall-clock
        now = now.replace(tzinfo=_TZ)

    # Convert to Taipei local time so hour comparison is correct
    local_now = now.astimezone(_TZ)

    if session == "day":
        return local_now.strftime("%Y-%m-%d")

    # Night session: opened at 15:00 Taipei time
    if local_now.hour >= 15:
        return local_now.strftime("%Y-%m-%d")
    else:
        yesterday = local_now - timedelta(days=1)
        return yesterday.strftime("%Y-%m-%d")


def build_report(
    session: str,
    date: str,
    symbol: str = "TXFD6",
) -> ComposedReport | None:
    """Run pipeline stages 1-4 (collect → extract → reason → compose).

    Args:
        session: "day" or "night".
        date:    ISO date string, e.g. "2026-03-27".
        symbol:  Instrument symbol to collect data for. Defaults to "TXFD6".

    Returns:
        A :class:`ComposedReport` containing tier-aware message parts.
        Returns None when the session has no tick data.
    """
    from hft_platform.reports.collector import DataCollector
    from hft_platform.reports.composer import ReportComposer
    from hft_platform.reports.facts import extract_all
    from hft_platform.reports.reasoner import reason_all

    _log.info("build_report_start", session=session, date=date, symbol=symbol)

    # Stage 1: collect session data
    collector = DataCollector()
    session_data = collector.collect(session, date, symbol)
    _log.info("stage1_complete", ticks=session_data.tick_count, bars=len(session_data.bars_5m))

    if session_data.tick_count == 0:
        _log.warning("build_report_empty_session", session=session, date=date)
        return None

    # Stage 1b: cross-day data
    prev_days = collector.collect_cross_day(symbol, session, date)

    # Stage 2: extract facts
    fact_report = extract_all(session_data, prev_days=prev_days)
    _log.info("stage2_facts_complete", segments=len(fact_report.segments))

    # Stage 3: reason
    reasoning_report = reason_all(fact_report)
    _log.info(
        "stage3_reasoning_complete",
        bias=reasoning_report.bias.bias,
        confidence=reasoning_report.bias.confidence,
        levels=len(reasoning_report.levels),
    )

    # Stage 4: compose
    composed = ReportComposer().compose(fact_report, reasoning_report)
    _log.info("stage4_compose_complete", parts=len(composed.messages))
    return composed


async def run_pipeline(
    session: str,
    date: str,
    *,
    dry_run: bool = False,
    debug: bool = False,
) -> None:
    """Execute the full report pipeline for the given session and date.

    Args:
        session:  "day" or "night".
        date:     ISO date string, e.g. "2026-03-27".
        dry_run:  If True, skip actual output dispatch.
        debug:    If True, enable verbose logging and print rendered output.
    """
    _log.info("report_pipeline_start", session=session, date=date, dry_run=dry_run)

    composed = build_report(session, date)
    if composed is None:
        return

    if debug:
        print(f"\n{'=' * 40} REPORT {'=' * 40}")
        for i, part in enumerate(composed.messages, 1):
            if part.kind == "text":
                print(f"\n--- Part {i} [{part.min_tier}] ({len(part.content)} chars) ---")
                print(part.content)
            elif part.kind == "image":
                print(f"\n--- Part {i} [{part.min_tier}] IMAGE ({len(part.image or b'')} bytes) ---")
                print(f"  caption: {part.caption}")

    if dry_run:
        _log.info("report_pipeline_dry_run_complete")
        return

    # Stage 5: distribute
    from hft_platform.reports.distributor import Distributor, ReportSender, load_channels

    channels = load_channels()
    sender = ReportSender()
    distributor = Distributor(sender=sender, channels=channels)
    try:
        await distributor.send(composed)
    finally:
        await sender.close()

    _log.info("report_pipeline_complete", session=session, date=date)


def main() -> None:
    """CLI entry point for the report pipeline.

    Usage:
        python -m hft_platform.reports --session day
        python -m hft_platform.reports --session night --dry-run
        python -m hft_platform.reports --session day --date 2026-03-27 --debug
    """
    parser = argparse.ArgumentParser(
        prog="hft-reports",
        description="Generate and distribute HFT market analysis reports.",
    )
    parser.add_argument(
        "--session",
        required=True,
        choices=["day", "night"],
        help="Trading session to report on.",
    )
    parser.add_argument(
        "--date",
        default=None,
        help="Trading date override (YYYY-MM-DD). Auto-resolved if omitted.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        default=False,
        help="Build report but skip actual channel dispatch.",
    )
    parser.add_argument(
        "--debug",
        action="store_true",
        default=False,
        help="Enable verbose debug logging.",
    )

    args = parser.parse_args()

    enabled = os.environ.get("HFT_REPORT_ENABLED", "0") == "1"
    if not enabled and not args.dry_run and not args.debug:
        _log.warning(
            "report_pipeline.disabled",
            reason="HFT_REPORT_ENABLED != '1' and neither --dry-run nor --debug set",
        )
        sys.exit(0)

    date = args.date if args.date else resolve_trading_date(args.session)
    asyncio.run(run_pipeline(args.session, date, dry_run=args.dry_run, debug=args.debug))
