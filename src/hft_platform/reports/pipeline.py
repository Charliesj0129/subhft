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

__all__ = ["resolve_trading_date", "run_pipeline", "main"]

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
    from hft_platform.reports.collector import DataCollector
    from hft_platform.reports.distributor import Distributor, ReportSender, load_channels
    from hft_platform.reports.renderer import ReportRenderer
    from hft_platform.reports.scenarios import ScenarioBuilder
    from hft_platform.reports.signals import SignalEngine

    _log.info("report_pipeline_start", session=session, date=date, dry_run=dry_run)

    # Stage 1: collect session data
    collector = DataCollector()
    session_data = collector.collect(session, date)
    _log.info("stage1_complete", ticks=session_data.tick_count, bars=len(session_data.bars_5m))

    if session_data.tick_count == 0:
        _log.warning("report_pipeline_empty_session", session=session, date=date)
        return

    # Stage 2: derive signals
    engine = SignalEngine()
    signal_report = engine.analyze(session_data)
    _log.info(
        "stage2_complete",
        bias=signal_report.bias,
        confidence=signal_report.bias_confidence,
    )

    # Stage 3: build scenarios
    builder = ScenarioBuilder()
    scenario_report = builder.build(signal_report)
    _log.info(
        "stage3_complete",
        direction=scenario_report.direction,
        scenarios=len(scenario_report.scenarios),
    )

    # Stage 4: render messages
    renderer = ReportRenderer()
    rendered = {
        "free": renderer.render(scenario_report, tier="free"),
        "paid": renderer.render(scenario_report, tier="paid"),
    }
    _log.info(
        "stage4_complete",
        free_msgs=len(rendered["free"]),
        paid_msgs=len(rendered["paid"]),
    )

    if debug:
        for tier, msgs in rendered.items():
            print(f"\n{'=' * 40} {tier.upper()} {'=' * 40}")
            for i, m in enumerate(msgs, 1):
                print(f"\n--- Message {i}/{len(msgs)} ({len(m)} chars) ---")
                print(m)

    if dry_run:
        _log.info("report_pipeline_dry_run_complete")
        return

    # Stage 5: distribute
    channels = load_channels()
    sender = ReportSender()
    distributor = Distributor(sender=sender, channels=channels)
    try:
        await distributor.send(rendered)
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
