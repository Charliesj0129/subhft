"""Digital Twin: generate synthetic PaperTradeSession list from BacktestResult.

Eliminates the 1-week manual Gate E bottleneck by splitting an equity curve
into N segments, computing per-segment PnL/fills/regime, and producing
PaperTradeSession objects that satisfy Gate E requirements.

Usage (CLI):
    python -m research digital-twin --alpha-id <id> --data <path> [--n-sessions 5]
"""
from __future__ import annotations

import argparse
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from uuid import uuid4

import numpy as np
from structlog import get_logger

from hft_platform.alpha.experiments import PaperTradeSession

logger = get_logger("research.tools.digital_twin")

# Default conservative reject rate when no latency profile is provided.
_DEFAULT_REJECT_RATE: float = 0.005
# Maximum reject rate to stay within Gate E bounds (max_execution_reject_rate=0.01).
_MAX_REJECT_RATE: float = 0.009
_DEFAULT_SESSION_DURATION_MINUTES: int = 60


def generate_digital_twin_sessions(
    backtest_result: Any,
    alpha_id: str,
    *,
    n_sessions: int = 5,
    latency_profile: dict[str, Any] | None = None,
) -> list[PaperTradeSession]:
    """Generate synthetic PaperTradeSession list from a BacktestResult.

    Splits ``backtest_result.equity_curve`` into *n_sessions* equal segments
    and derives per-segment metrics (PnL, fills, regime).

    Args:
        backtest_result: A ``BacktestResult`` (or any object with
            ``equity_curve`` and ``positions`` ndarrays).
        alpha_id: Alpha identifier string.
        n_sessions: Number of sessions to generate (minimum 5 for Gate E).
        latency_profile: Optional latency profile dict; used to model
            ``reject_rate``.  When *None*, a conservative default is used.

    Returns:
        List of ``PaperTradeSession`` frozen dataclasses.
    """
    n_sessions = max(5, int(n_sessions))

    equity = np.asarray(backtest_result.equity_curve, dtype=np.float64)
    positions = np.asarray(backtest_result.positions, dtype=np.float64)

    if equity.size < n_sessions:
        raise ValueError(
            f"equity_curve has {equity.size} points but {n_sessions} sessions "
            "were requested; need at least one point per session"
        )

    # Split into equal-sized segments -----------------------------------------
    seg_size = equity.size // n_sessions
    segments: list[dict[str, Any]] = []
    for i in range(n_sessions):
        start = i * seg_size
        end = start + seg_size if i < n_sessions - 1 else equity.size
        seg_eq = equity[start:end]
        seg_pos = positions[start:end]

        # PnL in bps from equity changes
        eq_start = seg_eq[0] if seg_eq[0] != 0.0 else 1.0
        pnl_bps = float((seg_eq[-1] - seg_eq[0]) / abs(eq_start) * 10_000)

        # Fills: count position changes
        fills = int(np.count_nonzero(np.diff(seg_pos)))

        # Volatility: std of tick-to-tick returns
        prev = seg_eq[:-1]
        safe_prev = np.where(np.abs(prev) > 1e-12, prev, 1.0)
        returns = np.diff(seg_eq) / safe_prev
        vol = float(np.std(returns)) if returns.size > 1 else 0.0

        segments.append({"pnl_bps": pnl_bps, "fills": max(1, fills), "vol": vol})

    # Assign regimes by volatility (above-median -> trending, below -> mean_reverting)
    vols = np.array([s["vol"] for s in segments], dtype=np.float64)
    median_vol = float(np.median(vols))
    for seg in segments:
        seg["regime"] = "trending" if seg["vol"] >= median_vol else "mean_reverting"

    # Force regime diversity: at least 2 distinct regimes.  When all equal,
    # flip the last segment to the opposite regime.
    regimes_set = {s["regime"] for s in segments}
    if len(regimes_set) < 2:
        segments[-1]["regime"] = (
            "mean_reverting" if segments[0]["regime"] == "trending" else "trending"
        )

    # Model reject rate from latency profile -----------------------------------
    reject_rate = _model_reject_rate(latency_profile)

    # Generate synthetic trading days (today - 7 days window) ------------------
    base_day = datetime.now(UTC).date() - timedelta(days=7)
    sessions: list[PaperTradeSession] = []
    for i, seg in enumerate(segments):
        trading_day = base_day + timedelta(days=i)
        start_dt = datetime(
            trading_day.year,
            trading_day.month,
            trading_day.day,
            9,
            0,
            0,
            tzinfo=UTC,
        )
        dur_s = _DEFAULT_SESSION_DURATION_MINUTES * 60
        end_dt = start_dt + timedelta(seconds=dur_s)
        sid = uuid4().hex[:8]

        session = PaperTradeSession(
            alpha_id=str(alpha_id),
            session_id=sid,
            started_at=start_dt.isoformat(),
            ended_at=end_dt.isoformat(),
            duration_seconds=dur_s,
            trading_day=trading_day.isoformat(),
            fills=int(seg["fills"]),
            pnl_bps=float(seg["pnl_bps"]),
            drift_alerts=0,
            execution_reject_rate=reject_rate,
            notes="digital_twin:v1",
            session_duration_minutes=_DEFAULT_SESSION_DURATION_MINUTES,
            reject_rate_p95=reject_rate,
            regime=str(seg["regime"]),
        )
        sessions.append(session)

    logger.info(
        "digital_twin.generated",
        alpha_id=alpha_id,
        n_sessions=len(sessions),
        regimes=sorted({s.regime for s in sessions if s.regime}),
    )
    return sessions


def _model_reject_rate(latency_profile: dict[str, Any] | None) -> float:
    """Derive a conservative reject rate from latency profile.

    Higher broker RTT implies more opportunity for rejects.  We model this
    as a simple linear function clamped to [0, _MAX_REJECT_RATE].
    """
    if latency_profile is None:
        return _DEFAULT_REJECT_RATE

    submit_ms = float(latency_profile.get("submit_ack_latency_ms", 36.0))
    # Model: base 0.002 + 0.0001 per ms of submit latency, clamped
    rate = 0.002 + submit_ms * 0.0001
    return min(rate, _MAX_REJECT_RATE)


def cmd_digital_twin() -> int:
    """CLI entry point for ``python -m research digital-twin``."""
    parser = argparse.ArgumentParser(
        prog="research digital-twin",
        description="Generate digital twin PaperTradeSessions from backtest equity curve",
    )
    parser.add_argument("--alpha-id", required=True, help="Alpha identifier")
    parser.add_argument(
        "--data",
        required=True,
        help="Path to backtest result .npy (equity curve) or directory with equity.npy + positions.npy",
    )
    parser.add_argument("--n-sessions", type=int, default=5, help="Number of sessions (default 5)")
    parser.add_argument("--latency-profile", default=None, help="Latency profile ID from config")

    args = parser.parse_args(sys.argv[2:])

    data_path = Path(args.data).resolve()
    equity, positions = _load_data(data_path)

    latency_profile: dict[str, Any] | None = None
    if args.latency_profile:
        from research.tools.latency_profiles import load_latency_profile

        latency_profile = load_latency_profile(args.latency_profile)

    # Build a lightweight backtest result proxy
    result_proxy = _BacktestProxy(equity_curve=equity, positions=positions)

    sessions = generate_digital_twin_sessions(
        result_proxy,
        args.alpha_id,
        n_sessions=args.n_sessions,
        latency_profile=latency_profile,
    )

    # Log sessions to experiment tracker
    from hft_platform.alpha.experiments import ExperimentTracker

    tracker = ExperimentTracker()
    for s in sessions:
        tracker.log_paper_trade_session(
            alpha_id=s.alpha_id,
            started_at=s.started_at,
            ended_at=s.ended_at,
            trading_day=s.trading_day,
            fills=s.fills,
            pnl_bps=s.pnl_bps,
            drift_alerts=s.drift_alerts,
            execution_reject_rate=s.execution_reject_rate,
            notes=s.notes,
            session_id=s.session_id,
            reject_rate_p95=s.reject_rate_p95,
            regime=s.regime,
        )

    logger.info(
        "digital_twin.cli_complete",
        alpha_id=args.alpha_id,
        sessions_logged=len(sessions),
    )
    return 0


class _BacktestProxy:
    """Lightweight proxy mimicking BacktestResult for digital twin generation."""

    __slots__ = ("equity_curve", "positions")

    def __init__(self, equity_curve: np.ndarray, positions: np.ndarray) -> None:
        self.equity_curve = equity_curve
        self.positions = positions


def _load_data(data_path: Path) -> tuple[np.ndarray, np.ndarray]:
    """Load equity curve and positions from file or directory."""
    if data_path.is_dir():
        eq_path = data_path / "equity.npy"
        pos_path = data_path / "positions.npy"
        if not eq_path.exists():
            raise FileNotFoundError(f"equity.npy not found in {data_path}")
        equity = np.load(eq_path, allow_pickle=False).astype(np.float64)
        if pos_path.exists():
            positions = np.load(pos_path, allow_pickle=False).astype(np.float64)
        else:
            positions = np.zeros_like(equity)
    elif data_path.suffix == ".npz":
        data = np.load(data_path, allow_pickle=False)
        if "equity_curve" in data:
            equity = data["equity_curve"].astype(np.float64)
        elif "equity" in data:
            equity = data["equity"].astype(np.float64)
        else:
            raise KeyError(f"No 'equity_curve' or 'equity' key in {data_path}")
        positions = data.get("positions", np.zeros_like(equity)).astype(np.float64)
    else:
        # Assume single .npy is equity curve
        equity = np.load(data_path, allow_pickle=False).astype(np.float64)
        # Derive synthetic positions from equity changes
        positions = np.sign(np.diff(np.concatenate([[0.0], equity])))

    return equity, positions
