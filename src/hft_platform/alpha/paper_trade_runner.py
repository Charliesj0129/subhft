"""Paper-trade runner — orchestrates paper trading sessions for alpha governance.

Provides a structured campaign runner that collects PaperTradeSessions from
a SessionRunner implementation and aggregates them into a PaperTradeSummary
suitable for Gate E evaluation.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from typing import Any, Protocol, runtime_checkable

import structlog

from hft_platform.alpha.experiments import PaperTradeSession

logger = structlog.get_logger("alpha.paper_trade_runner")


@dataclass(frozen=True, slots=True)
class PaperTradeRunnerConfig:
    alpha_id: str
    target_sessions: int = 10
    min_session_minutes: int = 60
    max_retries: int = 3
    dry_run: bool = False
    campaign_id: str = ""
    notes: str = ""


@dataclass(frozen=True, slots=True)
class PaperTradeSummary:
    alpha_id: str
    campaign_id: str
    session_count: int
    calendar_span_days: int
    distinct_trading_days: int
    min_session_duration_seconds: int
    invalid_session_duration_count: int
    drift_alerts_total: int
    execution_reject_rate_mean: float
    execution_reject_rate_p95: float | None
    total_fills: int
    mean_pnl_bps: float
    notes: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "alpha_id": self.alpha_id,
            "campaign_id": self.campaign_id,
            "session_count": self.session_count,
            "calendar_span_days": self.calendar_span_days,
            "distinct_trading_days": self.distinct_trading_days,
            "min_session_duration_seconds": self.min_session_duration_seconds,
            "invalid_session_duration_count": self.invalid_session_duration_count,
            "drift_alerts_total": self.drift_alerts_total,
            "execution_reject_rate_mean": self.execution_reject_rate_mean,
            "execution_reject_rate_p95": self.execution_reject_rate_p95,
            "total_fills": self.total_fills,
            "mean_pnl_bps": self.mean_pnl_bps,
            "notes": self.notes,
        }


@runtime_checkable
class SessionRunner(Protocol):
    """Protocol for running a single paper-trade session."""

    def run_one_session(self, config: PaperTradeRunnerConfig) -> PaperTradeSession:
        """Run a single paper-trade session and return the result."""
        ...


def _compute_summary(
    alpha_id: str,
    campaign_id: str,
    sessions: list[PaperTradeSession],
    config: PaperTradeRunnerConfig,
) -> PaperTradeSummary:
    """Aggregate a list of PaperTradeSession into a PaperTradeSummary."""
    if not sessions:
        return PaperTradeSummary(
            alpha_id=alpha_id,
            campaign_id=campaign_id,
            session_count=0,
            calendar_span_days=0,
            distinct_trading_days=0,
            min_session_duration_seconds=0,
            invalid_session_duration_count=0,
            drift_alerts_total=0,
            execution_reject_rate_mean=0.0,
            execution_reject_rate_p95=None,
            total_fills=0,
            mean_pnl_bps=0.0,
            notes=config.notes,
        )

    # Calendar span
    dates = [s.trading_day for s in sessions if s.trading_day]
    distinct_days = len(set(dates))
    if dates:
        try:
            parsed = sorted(dt.date.fromisoformat(d) for d in dates)
            span_days = (parsed[-1] - parsed[0]).days + 1
        except Exception:
            span_days = distinct_days
    else:
        span_days = 0

    min_dur = min(s.duration_seconds for s in sessions) if sessions else 0
    min_threshold_s = config.min_session_minutes * 60
    invalid_count = sum(1 for s in sessions if s.duration_seconds < min_threshold_s)

    drift_total = sum(s.drift_alerts for s in sessions)
    reject_rates = [s.execution_reject_rate for s in sessions]
    reject_mean = sum(reject_rates) / len(reject_rates) if reject_rates else 0.0

    # P95 reject rate if any session recorded reject_rate_p95
    p95_vals = [s.reject_rate_p95 for s in sessions if s.reject_rate_p95 is not None]
    reject_p95 = sorted(p95_vals)[int(len(p95_vals) * 0.95)] if p95_vals else None

    total_fills = sum(s.fills for s in sessions)
    pnl_vals = [s.pnl_bps for s in sessions]
    mean_pnl = sum(pnl_vals) / len(pnl_vals) if pnl_vals else 0.0

    return PaperTradeSummary(
        alpha_id=alpha_id,
        campaign_id=campaign_id,
        session_count=len(sessions),
        calendar_span_days=span_days,
        distinct_trading_days=distinct_days,
        min_session_duration_seconds=min_dur,
        invalid_session_duration_count=invalid_count,
        drift_alerts_total=drift_total,
        execution_reject_rate_mean=reject_mean,
        execution_reject_rate_p95=reject_p95,
        total_fills=total_fills,
        mean_pnl_bps=mean_pnl,
        notes=config.notes,
    )


class PaperTradeRunner:
    """Orchestrate paper trading sessions for an alpha strategy."""

    def __init__(self, session_runner: SessionRunner) -> None:
        self._runner = session_runner

    def run_session(self, config: PaperTradeRunnerConfig) -> PaperTradeSession:
        """Run a single paper-trade session with retry on error.

        Returns the session result. In dry_run mode returns a stub session.
        """
        if config.dry_run:
            logger.info(
                "paper_trade_runner.dry_run",
                alpha_id=config.alpha_id,
                campaign_id=config.campaign_id,
            )
            return PaperTradeSession(
                alpha_id=config.alpha_id,
                session_id=f"dry_run_{config.campaign_id}_0",
                started_at=dt.datetime.now(dt.UTC).isoformat(),
                ended_at=dt.datetime.now(dt.UTC).isoformat(),
                duration_seconds=config.min_session_minutes * 60,
                trading_day=dt.date.today().isoformat(),
                fills=0,
                pnl_bps=0.0,
                drift_alerts=0,
                execution_reject_rate=0.0,
                notes="dry_run",
            )

        last_exc: Exception | None = None
        for attempt in range(1, config.max_retries + 1):
            try:
                session = self._runner.run_one_session(config)
                logger.info(
                    "paper_trade_runner.session_complete",
                    alpha_id=config.alpha_id,
                    session_id=session.session_id,
                    fills=session.fills,
                    pnl_bps=session.pnl_bps,
                    attempt=attempt,
                )
                return session
            except Exception as exc:
                last_exc = exc
                logger.warning(
                    "paper_trade_runner.session_error",
                    alpha_id=config.alpha_id,
                    attempt=attempt,
                    max_retries=config.max_retries,
                    error=str(exc),
                )
        raise RuntimeError(
            f"run_session failed after {config.max_retries} attempts for alpha '{config.alpha_id}'"
        ) from last_exc

    def run_campaign(self, config: PaperTradeRunnerConfig) -> PaperTradeSummary:
        """Run a full campaign of sessions and return aggregated summary."""
        sessions: list[PaperTradeSession] = []
        for idx in range(config.target_sessions):
            logger.info(
                "paper_trade_runner.campaign_progress",
                alpha_id=config.alpha_id,
                session_index=idx + 1,
                target=config.target_sessions,
            )
            session = self.run_session(config)
            sessions.append(session)

        summary = _compute_summary(
            alpha_id=config.alpha_id,
            campaign_id=config.campaign_id,
            sessions=sessions,
            config=config,
        )
        logger.info(
            "paper_trade_runner.campaign_complete",
            alpha_id=config.alpha_id,
            sessions=summary.session_count,
            distinct_days=summary.distinct_trading_days,
            drift_alerts=summary.drift_alerts_total,
        )
        return summary
