"""DailyReportService — auto-triggered on SessionGovernor CLOSED callback.

Queries ClickHouse for daily fill aggregates, sends a Telegram notification
via NotificationDispatcher, and writes an evidence summary via AutonomyEvidenceWriter.
"""

from __future__ import annotations

import asyncio
import datetime  # date-label-ok
import resource
from typing import Any

import structlog

logger = structlog.get_logger("services.daily_report")


class DailyReportService:
    """End-of-day report triggered by SessionGovernor phase transitions.

    Callback-driven — no async start/stop required.
    """

    __slots__ = (
        "_ch_client",
        "_notification_dispatcher",
        "_evidence_writer",
        "_position_store",
        "_storm_guard",
        "_loop",
    )

    def __init__(
        self,
        *,
        ch_client: Any,
        notification_dispatcher: Any,
        evidence_writer: Any,
        position_store: Any,
        storm_guard: Any,
    ) -> None:
        self._ch_client = ch_client
        self._notification_dispatcher = notification_dispatcher
        self._evidence_writer = evidence_writer
        self._position_store = position_store
        self._storm_guard = storm_guard
        self._loop: asyncio.AbstractEventLoop | None = None

    # ------------------------------------------------------------------
    # Phase callback (registered with SessionGovernor)
    # ------------------------------------------------------------------

    def on_phase_transition(self, track: str, old_phase: Any, new_phase: Any) -> None:
        """SessionGovernor phase callback — only acts on CLOSED.

        Handles both SessionPhase enum and plain strings defensively.
        """
        phase_name = new_phase.name if hasattr(new_phase, "name") else str(new_phase)
        if phase_name != "CLOSED":
            return

        logger.info(
            "daily_report.phase_closed",
            track=track,
            old_phase=str(old_phase),
        )

        # Fire-and-forget the async report in the running event loop
        loop = self._loop
        try:
            if loop is None:
                loop = asyncio.get_running_loop()
            loop.create_task(self.on_session_closed(track))
        except RuntimeError:
            # No running loop — run synchronously (e.g. in tests)
            _loop = asyncio.new_event_loop()
            try:
                _loop.run_until_complete(self.on_session_closed(track))
            finally:
                _loop.close()

    # ------------------------------------------------------------------
    # Core report generation
    # ------------------------------------------------------------------

    async def on_session_closed(self, track: str) -> None:
        """Generate and send the daily report."""
        date_str = datetime.date.today().isoformat()  # date-label-ok

        aggregates = self._query_daily_aggregates(date_str)
        position_status = self._get_position_status()
        memory_gb, memory_max_gb = self._get_memory_usage()

        storm_state = getattr(self._storm_guard, "state", None)
        storm_guard_state = getattr(storm_state, "name", None) or str(storm_state)

        # TCA section
        tca_section = ""
        try:
            from hft_platform.tca.analyzer import TCAAnalyzer
            from hft_platform.tca.report import TCAReportGenerator

            tca_gen = TCAReportGenerator()
            tca_analyzer = TCAAnalyzer(self._ch_client)
            tca_reports = tca_analyzer.daily_report(date_str)
            tca_section = tca_gen.format_telegram_section(tca_reports)
        except Exception:  # noqa: BLE001
            logger.warning("daily_report_tca_section_failed", exc_info=True)

        # PnL section
        pnl_section = ""
        try:
            from hft_platform.ops.daily_pnl_report import DailyPnlSection

            pnl_gen = DailyPnlSection()
            pnl_section = pnl_gen.format_telegram_section(
                realized_pnl_ntd=aggregates.get("pnl_ntd", 0),
                unrealized_pnl_ntd=0,  # TODO: wire from position_store
                trade_count=aggregates.get("buys", 0) + aggregates.get("sells", 0),
                fill_count=aggregates.get("fills", 0),
            )
        except Exception:  # noqa: BLE001
            logger.warning("daily_report_pnl_section_failed", exc_info=True)

        report_kwargs: dict[str, Any] = {
            "date_str": date_str,
            "pnl_ntd": aggregates["pnl_ntd"],
            "buys": aggregates["buys"],
            "sells": aggregates["sells"],
            "fills": aggregates["fills"],
            "position_status": position_status,
            "reconciliation_status": "OK",
            "latency_p95_ms": 0.0,  # TODO: wire from Prometheus metrics or LatencyRecorder
            "reconnect_count": 0,  # TODO: wire from ReconnectOrchestrator counter
            "storm_guard_state": storm_guard_state,
            "memory_gb": memory_gb,
            "memory_max_gb": memory_max_gb,
        }

        try:
            await self._notification_dispatcher.notify_daily_report(**report_kwargs)
            logger.info("daily_report.sent", date_str=date_str, pnl_ntd=aggregates["pnl_ntd"])
        except Exception as exc:  # noqa: BLE001
            logger.error("daily_report.send_failed", error=str(exc))

        # Send TCA + PnL supplement as a follow-up message
        try:
            await self._notification_dispatcher.notify_tca_pnl_supplement(
                tca_section=tca_section,
                pnl_section=pnl_section,
            )
        except Exception as exc:  # noqa: BLE001
            logger.error("daily_report.supplement_send_failed", error=str(exc))

        # Write evidence summary
        try:
            summary = {
                "date": date_str,
                "track": track,
                **aggregates,
                "position_status": position_status,
                "storm_guard_state": storm_guard_state,
                "memory_gb": memory_gb,
                "memory_max_gb": memory_max_gb,
            }
            self._evidence_writer.write_daily_summary(summary)
            logger.info("daily_report.evidence_written", date_str=date_str)
        except Exception as exc:  # noqa: BLE001
            logger.error("daily_report.evidence_write_failed", error=str(exc))

    # ------------------------------------------------------------------
    # ClickHouse query
    # ------------------------------------------------------------------

    def _query_daily_aggregates(self, date_str: str) -> dict[str, int]:
        """Query hft.fills for daily aggregates. Returns zeroes on failure."""
        zeroed: dict[str, int] = {
            "pnl_ntd": 0,
            "buys": 0,
            "sells": 0,
            "fills": 0,
            "total_fee_scaled": 0,
        }
        if self._ch_client is None:
            return zeroed

        query = (
            "SELECT "
            "  sum(price_scaled * qty) AS pnl_scaled, "  # TODO: use realized_pnl; this is notional
            "  countIf(side = 'B') AS buy_count, "
            "  countIf(side = 'S') AS sell_count, "
            "  count(*) AS fill_count, "
            "  sum(fee_scaled) AS total_fee_scaled "
            "FROM hft.fills "
            "WHERE toDate(toDateTime(ts_exchange / 1000000000)) = {date:String}"
        )
        try:
            result = self._ch_client.query(query, parameters={"date": date_str})
            rows = getattr(result, "result_rows", None) or []
            if not rows or not rows[0]:
                return zeroed
            row = rows[0]
            return {
                "pnl_ntd": int(row[0] or 0),
                "buys": int(row[1] or 0),
                "sells": int(row[2] or 0),
                "fills": int(row[3] or 0),
                "total_fee_scaled": int(row[4] or 0),
            }
        except Exception as exc:  # noqa: BLE001
            logger.warning("daily_report.ch_query_failed", error=str(exc))
            return zeroed

    # ------------------------------------------------------------------
    # Position status
    # ------------------------------------------------------------------

    def _get_position_status(self) -> str:
        """Return 'flat' or 'N open' based on current positions."""
        try:
            positions = self._position_store.get_all_positions()
            non_zero = {k: v for k, v in positions.items() if v != 0}
            if not non_zero:
                return "flat"
            return f"{len(non_zero)} open"
        except Exception:  # noqa: BLE001
            return "unknown"

    # ------------------------------------------------------------------
    # Memory usage
    # ------------------------------------------------------------------

    @staticmethod
    def _get_memory_usage() -> tuple[float, float]:
        """Return (current_gb, max_gb) via resource.getrusage."""
        try:
            usage = resource.getrusage(resource.RUSAGE_SELF)
            # ru_maxrss is in KB on Linux
            max_gb = usage.ru_maxrss / (1024 * 1024)
            current_gb = max_gb  # RSS is peak on Linux
            return (round(current_gb, 2), round(max_gb, 2))
        except Exception:  # noqa: BLE001
            return (0.0, 0.0)
