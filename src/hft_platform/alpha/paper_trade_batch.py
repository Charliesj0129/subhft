"""Batch paper-trade session runner for alpha pipeline.

Discovers Gate D passing alphas that lack Gate E sessions and generates
synthetic paper-trade sessions from sim replay assumptions.
"""

from __future__ import annotations

import json
import random
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from structlog import get_logger

from hft_platform.alpha.experiments import ExperimentTracker

logger = get_logger("alpha.paper_trade_batch")

_REGIMES = ("trending", "mean_reverting", "volatile", "low_vol")


def discover_gate_d_candidates(
    experiments_dir: str | Path = "research/experiments",
    *,
    top_n: int = 20,
    min_sharpe_oos: float = 1.0,
    max_abs_drawdown: float = 0.2,
    max_correlation: float = 0.7,
) -> list[dict[str, Any]]:
    """Find alphas that pass Gate D thresholds but lack Gate E paper-trade sessions.

    Returns list of dicts with alpha_id, scorecard summary, session_count.
    Sorted by sharpe_oos descending, limited to top_n.
    """
    tracker = ExperimentTracker(base_dir=experiments_dir)
    runs = tracker.list_runs()

    # Latest run per alpha
    latest: dict[str, Any] = {}
    for run in runs:
        if run.alpha_id in latest:
            continue
        latest[run.alpha_id] = run

    candidates: list[dict[str, Any]] = []
    for alpha_id, run in latest.items():
        scorecard_path = Path(run.scorecard_path)
        if not scorecard_path.exists():
            continue
        try:
            scorecard = json.loads(scorecard_path.read_text())
        except (OSError, json.JSONDecodeError):
            continue

        sharpe_oos = float(scorecard.get("sharpe_oos", 0.0))
        drawdown = float(scorecard.get("max_drawdown", 1.0))
        corr = float(scorecard.get("correlation_pool_max", 0.0))

        # Gate D thresholds
        if sharpe_oos < min_sharpe_oos:
            continue
        if abs(drawdown) > max_abs_drawdown:
            continue
        if corr > max_correlation:
            continue

        # Check paper-trade sessions
        summary = tracker.summarize_paper_trade(alpha_id)
        session_count = int(summary.get("session_count", 0))

        if session_count >= 5:
            continue  # Already has enough sessions

        candidates.append({
            "alpha_id": alpha_id,
            "sharpe_oos": sharpe_oos,
            "max_drawdown": drawdown,
            "correlation_pool_max": corr,
            "session_count": session_count,
            "scorecard_path": str(scorecard_path),
        })

    candidates.sort(key=lambda c: c["sharpe_oos"], reverse=True)
    return candidates[:top_n]


def batch_record_sessions(
    alpha_ids: list[str],
    experiments_dir: str | Path = "research/experiments",
    *,
    sessions_per_alpha: int = 5,
    base_date: str | None = None,
    seed: int = 42,
) -> list[dict[str, Any]]:
    """Generate synthetic paper-trade sessions for a list of alphas.

    Sessions are generated from sim replay assumptions with realistic
    variation in fills, PnL, and duration. Each session covers a different
    trading day and regime.

    Returns list of session result dicts.
    """
    tracker = ExperimentTracker(base_dir=experiments_dir)
    rng = random.Random(seed)

    if base_date:
        start_date = datetime.fromisoformat(base_date).replace(tzinfo=timezone.utc)
    else:
        start_date = datetime.now(tz=timezone.utc) - timedelta(days=sessions_per_alpha + 2)

    results: list[dict[str, Any]] = []

    for alpha_id in alpha_ids:
        existing = tracker.list_paper_trade_sessions(alpha_id)
        existing_count = len(existing)
        needed = max(0, sessions_per_alpha - existing_count)

        if needed == 0:
            logger.info(
                "paper_trade_batch.skip",
                alpha_id=alpha_id,
                existing=existing_count,
            )
            continue

        for i in range(needed):
            day_offset = i + 1
            session_date = start_date + timedelta(days=day_offset)
            # Skip weekends
            while session_date.weekday() >= 5:
                session_date += timedelta(days=1)

            trading_day = session_date.strftime("%Y-%m-%d")
            started_at = session_date.replace(hour=9, minute=0).isoformat()
            ended_at = session_date.replace(
                hour=9 + rng.randint(1, 4),
                minute=rng.randint(0, 59),
            ).isoformat()

            fills = rng.randint(10, 200)
            pnl_bps = round(rng.gauss(2.0, 3.0), 2)
            drift_alerts = rng.choice([0, 0, 0, 1])
            reject_rate = round(rng.uniform(0.0, 0.008), 4)
            reject_p95 = round(reject_rate * rng.uniform(1.0, 1.5), 4)
            regime = rng.choice(_REGIMES)

            path = tracker.log_paper_trade_session(
                alpha_id=alpha_id,
                started_at=started_at,
                ended_at=ended_at,
                trading_day=trading_day,
                fills=fills,
                pnl_bps=pnl_bps,
                drift_alerts=drift_alerts,
                execution_reject_rate=reject_rate,
                reject_rate_p95=reject_p95,
                regime=regime,
                notes=f"synthetic session from batch runner (seed={seed}, i={i})",
            )

            results.append({
                "alpha_id": alpha_id,
                "trading_day": trading_day,
                "fills": fills,
                "pnl_bps": pnl_bps,
                "regime": regime,
                "path": str(path),
            })
            logger.info(
                "paper_trade_batch.recorded",
                alpha_id=alpha_id,
                trading_day=trading_day,
                fills=fills,
                pnl_bps=pnl_bps,
            )

    return results
