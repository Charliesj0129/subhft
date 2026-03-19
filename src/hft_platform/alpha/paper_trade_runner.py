"""Paper-trade session runner — orchestrates single and campaign paper-trade sessions.

Provides:
- ``PaperTradeRunnerConfig``: configuration for a paper-trade campaign.
- ``PaperTradeSummary``: aggregated statistics across all sessions in a campaign.
- ``SessionRunner``: protocol for objects that can execute a single paper-trade session.
- ``PaperTradeRunner``: orchestrator that drives session execution and optional persistence.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import date
from typing import TYPE_CHECKING, Protocol

import structlog

from hft_platform.alpha.experiments import ExperimentTracker, PaperTradeSession

if TYPE_CHECKING:
    pass

logger = structlog.get_logger("alpha.paper_trade_runner")


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class PaperTradeRunnerConfig:
    """Configuration for a paper-trade campaign or a single session run."""

    alpha_id: str
    session_duration_minutes: int = 240
    max_sessions: int = 10
    regime_hint: str | None = None
    project_root: str = "."
    experiments_dir: str = "research/experiments"


# ---------------------------------------------------------------------------
# Summary
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class PaperTradeSummary:
    """Aggregated statistics computed from all sessions in a campaign."""

    alpha_id: str
    sessions: tuple[PaperTradeSession, ...]
    session_count: int
    calendar_span_days: int
    distinct_trading_days: int
    min_session_duration_seconds: int
    drift_alerts_total: int
    execution_reject_rate_mean: float
    pnl_bps_mean: float

    def to_dict(self) -> dict:  # type: ignore[type-arg]
        """Return a JSON-serialisable representation."""
        return {
            "alpha_id": self.alpha_id,
            "session_count": self.session_count,
            "calendar_span_days": self.calendar_span_days,
            "distinct_trading_days": self.distinct_trading_days,
            "min_session_duration_seconds": self.min_session_duration_seconds,
            "drift_alerts_total": self.drift_alerts_total,
            "execution_reject_rate_mean": self.execution_reject_rate_mean,
            "pnl_bps_mean": self.pnl_bps_mean,
            "sessions": [s.to_dict() for s in self.sessions],
        }


# ---------------------------------------------------------------------------
# Protocol
# ---------------------------------------------------------------------------


class SessionRunner(Protocol):
    """Protocol for objects that can execute a single paper-trade session."""

    def run(
        self,
        alpha_id: str,
        duration_minutes: int,
        regime_hint: str | None = None,
    ) -> PaperTradeSession:
        """Run a single paper-trade session and return the result."""
        ...


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------


class PaperTradeRunner:
    """Orchestrates paper-trade sessions using a ``SessionRunner`` implementation.

    Parameters
    ----------
    runner:
        The ``SessionRunner`` that executes individual sessions.
    tracker:
        Optional ``ExperimentTracker`` for persisting session records and
        writing the campaign summary JSON.  When ``None``, sessions are still
        executed but nothing is persisted.
    """

    def __init__(
        self,
        runner: SessionRunner,
        tracker: ExperimentTracker | None = None,
    ) -> None:
        self._runner = runner
        self._tracker = tracker

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def run_session(self, config: PaperTradeRunnerConfig) -> PaperTradeSession:
        """Execute a single paper-trade session.

        Parameters
        ----------
        config:
            Runner configuration.  Only ``alpha_id``, ``session_duration_minutes``,
            and ``regime_hint`` are used for a single-session run.

        Returns
        -------
        PaperTradeSession
            The completed session record.
        """
        log = logger.bind(alpha_id=config.alpha_id, duration_minutes=config.session_duration_minutes)
        log.info("paper_trade_runner.run_session.start")

        session = self._runner.run(
            alpha_id=config.alpha_id,
            duration_minutes=config.session_duration_minutes,
            regime_hint=config.regime_hint,
        )

        log.info(
            "paper_trade_runner.run_session.complete",
            session_id=session.session_id,
            pnl_bps=session.pnl_bps,
            fills=session.fills,
        )
        return session

    def run_campaign(self, config: PaperTradeRunnerConfig) -> PaperTradeSummary:
        """Orchestrate a full paper-trade campaign of ``max_sessions`` sessions.

        Each session is executed sequentially via the ``SessionRunner``.
        If a ``tracker`` was provided at construction time, each session is
        persisted via ``tracker.log_paper_trade_session()``, and the final
        campaign summary is written to
        ``<paper_trade_dir>/<alpha_id>/paper_trade_summary.json``.

        Parameters
        ----------
        config:
            Campaign configuration.

        Returns
        -------
        PaperTradeSummary
            Aggregated statistics across all completed sessions.
        """
        log = logger.bind(alpha_id=config.alpha_id, max_sessions=config.max_sessions)
        log.info("paper_trade_runner.run_campaign.start")

        sessions: list[PaperTradeSession] = []
        for i in range(config.max_sessions):
            log.debug("paper_trade_runner.run_campaign.session", index=i + 1, total=config.max_sessions)
            try:
                session = self._runner.run(
                    alpha_id=config.alpha_id,
                    duration_minutes=config.session_duration_minutes,
                    regime_hint=config.regime_hint,
                )
            except Exception as exc:  # noqa: BLE001
                log.error(
                    "paper_trade_runner.run_campaign.session_error",
                    index=i + 1,
                    error=str(exc),
                )
                continue

            sessions.append(session)

            if self._tracker is not None:
                try:
                    self._tracker.log_paper_trade_session(
                        alpha_id=session.alpha_id,
                        started_at=session.started_at,
                        ended_at=session.ended_at,
                        trading_day=session.trading_day,
                        fills=session.fills,
                        pnl_bps=session.pnl_bps,
                        drift_alerts=session.drift_alerts,
                        execution_reject_rate=session.execution_reject_rate,
                        notes=session.notes,
                        session_id=session.session_id,
                        reject_rate_p95=session.reject_rate_p95,
                        regime=session.regime,
                    )
                except Exception as exc:  # noqa: BLE001
                    log.warning(
                        "paper_trade_runner.run_campaign.persist_error",
                        session_id=session.session_id,
                        error=str(exc),
                    )

        summary = _build_summary(alpha_id=config.alpha_id, sessions=sessions)

        if self._tracker is not None:
            self._persist_summary(summary)

        log.info(
            "paper_trade_runner.run_campaign.complete",
            session_count=summary.session_count,
            pnl_bps_mean=summary.pnl_bps_mean,
            drift_alerts_total=summary.drift_alerts_total,
        )
        return summary

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    def _persist_summary(self, summary: PaperTradeSummary) -> None:
        """Write ``paper_trade_summary.json`` under the tracker's paper_trade_dir."""
        assert self._tracker is not None  # guarded by caller
        out_dir = self._tracker.paper_trade_dir / summary.alpha_id
        try:
            out_dir.mkdir(parents=True, exist_ok=True)
            out_path = out_dir / "paper_trade_summary.json"
            payload = summary.to_dict()
            # Remove the full session list to keep the summary file lean;
            # individual sessions are already stored per-session by the tracker.
            payload.pop("sessions", None)
            out_path.write_text(json.dumps(payload, indent=2, sort_keys=True))
            logger.info(
                "paper_trade_runner.summary_persisted",
                alpha_id=summary.alpha_id,
                path=str(out_path),
            )
        except OSError as exc:
            logger.error(
                "paper_trade_runner.summary_persist_error",
                alpha_id=summary.alpha_id,
                error=str(exc),
            )


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _build_summary(
    *,
    alpha_id: str,
    sessions: list[PaperTradeSession],
) -> PaperTradeSummary:
    """Compute aggregate statistics from a list of sessions."""
    if not sessions:
        return PaperTradeSummary(
            alpha_id=alpha_id,
            sessions=(),
            session_count=0,
            calendar_span_days=0,
            distinct_trading_days=0,
            min_session_duration_seconds=0,
            drift_alerts_total=0,
            execution_reject_rate_mean=0.0,
            pnl_bps_mean=0.0,
        )

    trading_days: set[str] = set()
    drift_alerts_total = 0
    reject_rates: list[float] = []
    pnl_bps_values: list[float] = []
    durations: list[int] = []

    for s in sessions:
        trading_days.add(s.trading_day)
        drift_alerts_total += s.drift_alerts
        reject_rates.append(s.execution_reject_rate)
        pnl_bps_values.append(s.pnl_bps)
        durations.append(max(0, s.duration_seconds))

    sorted_days = sorted(trading_days)
    calendar_span_days = _calendar_span(sorted_days)

    execution_reject_rate_mean = sum(reject_rates) / len(reject_rates) if reject_rates else 0.0
    pnl_bps_mean = sum(pnl_bps_values) / len(pnl_bps_values) if pnl_bps_values else 0.0
    min_session_duration_seconds = min(durations) if durations else 0

    return PaperTradeSummary(
        alpha_id=alpha_id,
        sessions=tuple(sessions),
        session_count=len(sessions),
        calendar_span_days=calendar_span_days,
        distinct_trading_days=len(trading_days),
        min_session_duration_seconds=min_session_duration_seconds,
        drift_alerts_total=drift_alerts_total,
        execution_reject_rate_mean=execution_reject_rate_mean,
        pnl_bps_mean=pnl_bps_mean,
    )


def _calendar_span(sorted_days: list[str]) -> int:
    """Return inclusive calendar span in days between first and last trading day."""
    if len(sorted_days) < 2:
        return 1 if sorted_days else 0
    try:
        day0 = date.fromisoformat(sorted_days[0])
        dayn = date.fromisoformat(sorted_days[-1])
        return int((dayn - day0).days) + 1
    except ValueError:
        return 0
