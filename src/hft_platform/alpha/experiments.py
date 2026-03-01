"""Alpha experiment tracking — log runs, compare metrics, load signal/equity arrays.

Each run is stored as a directory under ``base_dir/runs/<run_id>/`` containing:
  - ``meta.json``:             ExperimentRun metadata
  - ``scorecard.json``:        gate-C scorecard payload
  - ``backtest_report.json``:  full backtest report dict
  - ``signals.npy`` (opt):     signal array (float64)
  - ``equity.npy``   (opt):    equity-curve array (float64)
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any
from uuid import uuid4

import numpy as np
from structlog import get_logger

logger = get_logger("alpha.experiments")

DEFAULT_PAPER_SESSION_MINUTES = 60
LEGACY_ZERO_DURATION_FALLBACK_SECONDS = DEFAULT_PAPER_SESSION_MINUTES * 60


@dataclass(frozen=True)
class ExperimentRun:
    run_id: str
    alpha_id: str
    config_hash: str
    timestamp: str
    data_paths: tuple[str, ...]
    metrics: dict[str, float]
    gate_status: dict[str, bool]
    scorecard_path: str
    backtest_report_path: str
    signals_path: str | None = None
    equity_path: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class PaperTradeSession:
    alpha_id: str
    session_id: str
    started_at: str
    ended_at: str
    duration_seconds: int
    trading_day: str
    fills: int
    pnl_bps: float
    drift_alerts: int
    execution_reject_rate: float
    notes: str = ""
    session_duration_minutes: int | None = None  # convenience; None for legacy JSON
    # P95 reject rate over the session window.
    # Per CLAUDE.md latency realism policy, Gate E uses P95 when present.
    # None for legacy sessions that did not record this field.
    reject_rate_p95: float | None = None
    # Market regime observed during this session (e.g. "trending", "mean_reverting",
    # "volatile", "low_vol").  Gate E warns when sessions don't span ≥2 regimes.
    # None for legacy sessions that did not record this field.
    regime: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class ExperimentTracker:
    def __init__(self, base_dir: str | Path = "research/experiments"):
        self.base_dir = Path(base_dir)
        self.runs_dir = self.base_dir / "runs"
        self.comparisons_dir = self.base_dir / "comparisons"
        self.paper_trade_dir = self.base_dir / "paper_trade"

    def log_run(
        self,
        *,
        run_id: str,
        alpha_id: str,
        config_hash: str,
        data_paths: list[str],
        metrics: dict[str, float],
        gate_status: dict[str, bool],
        scorecard_payload: dict[str, Any],
        backtest_report_payload: dict[str, Any],
        signals: np.ndarray | None = None,
        equity: np.ndarray | None = None,
    ) -> Path:
        run_dir = self.runs_dir / run_id
        run_dir.mkdir(parents=True, exist_ok=True)

        scorecard_path = run_dir / "scorecard.json"
        backtest_report_path = run_dir / "backtest_report.json"
        scorecard_path.write_text(json.dumps(scorecard_payload, indent=2, sort_keys=True))
        backtest_report_path.write_text(json.dumps(backtest_report_payload, indent=2, sort_keys=True))

        signals_path: Path | None = None
        equity_path: Path | None = None
        if signals is not None:
            signals_path = run_dir / "signals.npy"
            np.save(signals_path, np.asarray(signals, dtype=np.float64))
        if equity is not None:
            equity_path = run_dir / "equity.npy"
            np.save(equity_path, np.asarray(equity, dtype=np.float64))

        meta = ExperimentRun(
            run_id=run_id,
            alpha_id=alpha_id,
            config_hash=config_hash,
            timestamp=datetime.now(UTC).isoformat(),
            data_paths=tuple(str(p) for p in data_paths),
            metrics=dict(metrics),
            gate_status=dict(gate_status),
            scorecard_path=str(scorecard_path),
            backtest_report_path=str(backtest_report_path),
            signals_path=(str(signals_path) if signals_path else None),
            equity_path=(str(equity_path) if equity_path else None),
        )
        meta_path = run_dir / "meta.json"
        meta_path.write_text(json.dumps(meta.to_dict(), indent=2, sort_keys=True))
        return meta_path

    def list_runs(self, alpha_id: str | None = None) -> list[ExperimentRun]:
        rows: list[ExperimentRun] = []
        for meta_path in sorted(self.runs_dir.glob("*/meta.json")):
            try:
                payload = json.loads(meta_path.read_text())
                row = _from_dict(payload)
            except (OSError, ValueError, KeyError) as exc:
                logger.warning("experiments.list_runs: skipping corrupt meta", path=str(meta_path), error=str(exc))
                continue
            if alpha_id and row.alpha_id != alpha_id:
                continue
            rows.append(row)
        rows.sort(key=lambda item: item.timestamp, reverse=True)
        return rows

    def compare(self, run_ids: list[str]) -> list[dict[str, Any]]:
        target = set(run_ids)
        out: list[dict[str, Any]] = []
        for row in self.list_runs():
            if row.run_id not in target:
                continue
            out.append(
                {
                    "run_id": row.run_id,
                    "alpha_id": row.alpha_id,
                    "config_hash": row.config_hash,
                    "timestamp": row.timestamp,
                    **row.metrics,
                }
            )
        return sorted(out, key=lambda item: run_ids.index(item["run_id"])) if out else []

    def best_by_metric(
        self,
        metric: str,
        n: int = 10,
        alpha_id: str | None = None,
    ) -> list[dict[str, Any]]:
        rows = self.list_runs(alpha_id=alpha_id)
        scored: list[dict[str, Any]] = []
        for row in rows:
            value = row.metrics.get(metric)
            if value is None:
                continue
            scored.append(
                {
                    "run_id": row.run_id,
                    "alpha_id": row.alpha_id,
                    "metric": metric,
                    "value": float(value),
                    "timestamp": row.timestamp,
                    "config_hash": row.config_hash,
                }
            )
        scored.sort(key=lambda item: item["value"], reverse=True)
        return scored[: max(1, n)]

    def latest_signals_by_alpha(self) -> dict[str, np.ndarray]:
        latest: dict[str, ExperimentRun] = {}
        for row in self.list_runs():
            if row.alpha_id in latest:
                continue
            latest[row.alpha_id] = row

        signals: dict[str, np.ndarray] = {}
        for alpha_id, row in latest.items():
            if not row.signals_path:
                continue
            arr = _load_numpy(row.signals_path)
            if arr is None:
                continue
            signals[alpha_id] = np.asarray(arr, dtype=np.float64)
        return signals

    def latest_equity_by_alpha(self) -> dict[str, np.ndarray]:
        latest: dict[str, ExperimentRun] = {}
        for row in self.list_runs():
            if row.alpha_id in latest:
                continue
            latest[row.alpha_id] = row

        equities: dict[str, np.ndarray] = {}
        for alpha_id, row in latest.items():
            if not row.equity_path:
                continue
            arr = _load_numpy(row.equity_path)
            if arr is None:
                continue
            equities[alpha_id] = np.asarray(arr, dtype=np.float64)
        return equities

    def proxy_returns(self) -> np.ndarray | None:
        equities = self.latest_equity_by_alpha()
        if not equities:
            return None

        rows: list[np.ndarray] = []
        for eq in equities.values():
            arr = np.asarray(eq, dtype=np.float64)
            if arr.size < 2:
                continue
            prev = arr[:-1]
            delta = np.diff(arr)
            ret = np.divide(delta, prev, out=np.zeros_like(delta), where=np.abs(prev) > 1e-12)
            rows.append(np.nan_to_num(ret, nan=0.0, posinf=0.0, neginf=0.0))

        if not rows:
            return None

        min_len = min(row.size for row in rows)
        if min_len < 2:
            return None
        data = np.vstack([row[:min_len] for row in rows])
        proxy = np.nanmedian(data, axis=0)
        return np.asarray(proxy, dtype=np.float64)

    def log_paper_trade_session(
        self,
        *,
        alpha_id: str,
        started_at: str | None = None,
        ended_at: str | None = None,
        trading_day: str | None = None,
        fills: int = 0,
        pnl_bps: float = 0.0,
        drift_alerts: int = 0,
        execution_reject_rate: float = 0.0,
        notes: str = "",
        session_id: str | None = None,
        reject_rate_p95: float | None = None,
        regime: str | None = None,
    ) -> Path:
        start_dt, end_dt = _resolve_session_window(
            started_at=started_at,
            ended_at=ended_at,
            default_minutes=DEFAULT_PAPER_SESSION_MINUTES,
        )
        start = start_dt.isoformat()
        end = end_dt.isoformat()
        duration_seconds = _session_duration_seconds(start, end)
        day = str(trading_day or _trading_day_from_iso(start))
        sid = str(session_id or uuid4().hex[:8])
        _dur_s = max(0, int(duration_seconds))
        session = PaperTradeSession(
            alpha_id=str(alpha_id),
            session_id=sid,
            started_at=start,
            ended_at=end,
            duration_seconds=_dur_s,
            trading_day=day,
            fills=max(0, int(fills)),
            pnl_bps=float(pnl_bps),
            drift_alerts=max(0, int(drift_alerts)),
            execution_reject_rate=max(0.0, float(execution_reject_rate)),
            notes=str(notes or ""),
            session_duration_minutes=_dur_s // 60,
            reject_rate_p95=max(0.0, float(reject_rate_p95)) if reject_rate_p95 is not None else None,
            regime=str(regime) if regime else None,
        )

        out_dir = self.paper_trade_dir / str(alpha_id) / "sessions"
        out_dir.mkdir(parents=True, exist_ok=True)
        out_path = out_dir / f"{day}_{sid}.json"
        out_path.write_text(json.dumps(session.to_dict(), indent=2, sort_keys=True))
        return out_path

    def list_paper_trade_sessions(self, alpha_id: str) -> list[PaperTradeSession]:
        base = self.paper_trade_dir / str(alpha_id) / "sessions"
        if not base.exists():
            return []
        rows: list[PaperTradeSession] = []
        for path in sorted(base.glob("*.json")):
            try:
                payload = json.loads(path.read_text())
                row = _paper_session_from_dict(payload, alpha_id=alpha_id)
            except (OSError, ValueError, KeyError) as exc:
                logger.warning(
                    "experiments.list_paper_trade_sessions: skipping corrupt session",
                    path=str(path),
                    error=str(exc),
                )
                continue
            rows.append(row)
        rows.sort(key=lambda item: (item.trading_day, item.started_at))
        return rows

    def summarize_paper_trade(self, alpha_id: str) -> dict[str, Any]:
        sessions = self.list_paper_trade_sessions(alpha_id)
        if not sessions:
            return {
                "alpha_id": str(alpha_id),
                "session_count": 0,
                "distinct_trading_days": 0,
                "calendar_span_days": 0,
                "total_fills": 0,
                "drift_alerts_total": 0,
                "execution_reject_rate_mean": 0.0,
                "execution_reject_rate_p95": None,
                "mean_daily_pnl_bps": 0.0,
                "worst_daily_pnl_bps": 0.0,
                "total_session_duration_seconds": 0,
                "mean_session_duration_seconds": 0.0,
                "min_session_duration_seconds": 0,
                "max_session_duration_seconds": 0,
                "invalid_session_duration_count": 0,
                "first_trading_day": None,
                "last_trading_day": None,
                "regimes_covered": [],
            }

        day_pnl: dict[str, float] = {}
        total_fills = 0
        total_alerts = 0
        reject_rates: list[float] = []
        reject_rates_p95: list[float] = []
        durations: list[int] = []
        invalid_duration_count = 0
        days: set[str] = set()
        regimes_covered: set[str] = set()
        for row in sessions:
            days.add(row.trading_day)
            day_pnl[row.trading_day] = day_pnl.get(row.trading_day, 0.0) + float(row.pnl_bps)
            total_fills += int(row.fills)
            total_alerts += int(row.drift_alerts)
            reject_rates.append(float(row.execution_reject_rate))
            if row.reject_rate_p95 is not None:
                reject_rates_p95.append(float(row.reject_rate_p95))
            duration = max(0, int(row.duration_seconds))
            durations.append(duration)
            if duration <= 0:
                invalid_duration_count += 1
            if row.regime:
                regimes_covered.add(str(row.regime))

        sorted_days = sorted(days)
        day0 = _parse_day(sorted_days[0])
        dayn = _parse_day(sorted_days[-1])
        span_days = 0
        if day0 is not None and dayn is not None:
            span_days = int((dayn - day0).days + 1)

        daily_pnls = np.asarray(list(day_pnl.values()), dtype=np.float64)
        return {
            "alpha_id": str(alpha_id),
            "session_count": int(len(sessions)),
            "distinct_trading_days": int(len(days)),
            "calendar_span_days": int(span_days),
            "total_fills": int(total_fills),
            "drift_alerts_total": int(total_alerts),
            "execution_reject_rate_mean": float(np.mean(reject_rates)) if reject_rates else 0.0,
            "execution_reject_rate_p95": (float(np.percentile(reject_rates_p95, 95)) if reject_rates_p95 else None),
            "mean_daily_pnl_bps": float(np.mean(daily_pnls)) if daily_pnls.size else 0.0,
            "worst_daily_pnl_bps": float(np.min(daily_pnls)) if daily_pnls.size else 0.0,
            "total_session_duration_seconds": int(sum(durations)),
            "mean_session_duration_seconds": float(np.mean(durations)) if durations else 0.0,
            "min_session_duration_seconds": int(min(durations)) if durations else 0,
            "max_session_duration_seconds": int(max(durations)) if durations else 0,
            "invalid_session_duration_count": int(invalid_duration_count),
            "first_trading_day": sorted_days[0],
            "last_trading_day": sorted_days[-1],
            # Regime diversity (Q2 governance): sorted list of distinct regimes observed.
            "regimes_covered": sorted(regimes_covered),
        }


def _from_dict(payload: dict[str, Any]) -> ExperimentRun:
    return ExperimentRun(
        run_id=str(payload["run_id"]),
        alpha_id=str(payload["alpha_id"]),
        config_hash=str(payload.get("config_hash", "")),
        timestamp=str(payload.get("timestamp", "")),
        data_paths=tuple(payload.get("data_paths", ())),
        metrics={str(k): float(v) for k, v in dict(payload.get("metrics", {})).items()},
        gate_status={str(k): bool(v) for k, v in dict(payload.get("gate_status", {})).items()},
        scorecard_path=str(payload.get("scorecard_path", "")),
        backtest_report_path=str(payload.get("backtest_report_path", "")),
        signals_path=str(payload["signals_path"]) if payload.get("signals_path") else None,
        equity_path=str(payload["equity_path"]) if payload.get("equity_path") else None,
    )


def _load_numpy(path_str: str) -> np.ndarray | None:
    path = Path(path_str)
    if not path.exists():
        return None
    try:
        arr = np.load(path, allow_pickle=False)
    except (OSError, ValueError) as exc:
        logger.warning("experiments._load_numpy: failed to load", path=str(path), error=str(exc))
        return None
    return np.asarray(arr, dtype=np.float64)


def _paper_session_from_dict(payload: dict[str, Any], *, alpha_id: str) -> PaperTradeSession:
    alpha = str(payload.get("alpha_id") or alpha_id)
    started_at = str(payload.get("started_at", ""))
    ended_at = str(payload.get("ended_at", ""))
    duration_seconds = payload.get("duration_seconds", None)
    if duration_seconds is None:
        duration = _session_duration_seconds(started_at, ended_at, strict=False)
        if duration <= 0 and _is_legacy_zero_duration_session(payload, started_at=started_at, ended_at=ended_at):
            duration = LEGACY_ZERO_DURATION_FALLBACK_SECONDS
    else:
        try:
            duration = int(duration_seconds)
        except (TypeError, ValueError):
            duration = _session_duration_seconds(started_at, ended_at, strict=False)
    _dur_s = max(0, int(duration))
    raw_minutes = payload.get("session_duration_minutes")
    if raw_minutes is not None:
        try:
            session_duration_minutes: int | None = int(raw_minutes)
        except (TypeError, ValueError):
            session_duration_minutes = _dur_s // 60
    else:
        session_duration_minutes = _dur_s // 60
    return PaperTradeSession(
        alpha_id=alpha,
        session_id=str(payload.get("session_id", "")),
        started_at=started_at,
        ended_at=ended_at,
        duration_seconds=_dur_s,
        trading_day=str(payload.get("trading_day", "")),
        fills=int(payload.get("fills", 0)),
        pnl_bps=float(payload.get("pnl_bps", 0.0)),
        drift_alerts=int(payload.get("drift_alerts", 0)),
        execution_reject_rate=float(payload.get("execution_reject_rate", 0.0)),
        notes=str(payload.get("notes", "")),
        session_duration_minutes=session_duration_minutes,
        reject_rate_p95=(float(payload["reject_rate_p95"]) if payload.get("reject_rate_p95") is not None else None),
        regime=str(payload["regime"]) if payload.get("regime") else None,
    )


def _parse_day(value: str) -> date | None:
    try:
        return date.fromisoformat(value)
    except ValueError:
        return None


def _trading_day_from_iso(ts: str) -> str:
    try:
        return datetime.fromisoformat(ts.replace("Z", "+00:00")).date().isoformat()
    except ValueError:
        return datetime.now(UTC).date().isoformat()


def _coerce_utc(ts: datetime) -> datetime:
    if ts.tzinfo is None:
        return ts.replace(tzinfo=UTC)
    return ts.astimezone(UTC)


def _resolve_session_window(
    *,
    started_at: str | None,
    ended_at: str | None,
    default_minutes: int,
) -> tuple[datetime, datetime]:
    default_delta = timedelta(minutes=max(1, int(default_minutes)))
    now = datetime.now(UTC)

    start = _parse_iso_timestamp(started_at) if started_at else None
    end = _parse_iso_timestamp(ended_at) if ended_at else None
    if started_at and start is None:
        raise ValueError(f"invalid_started_at: {started_at}")
    if ended_at and end is None:
        raise ValueError(f"invalid_ended_at: {ended_at}")
    if start is not None:
        start = _coerce_utc(start)
    if end is not None:
        end = _coerce_utc(end)

    if start is not None and end is not None:
        if end < start:
            raise ValueError(f"invalid_session_window: ended_at ({ended_at}) is earlier than started_at ({started_at})")
        if end == start:
            end = start + default_delta
        return start, end

    if start is not None:
        end = now if now > start else (start + default_delta)
        if end <= start:
            end = start + default_delta
        return start, end

    if end is not None:
        start = end - default_delta
        return start, end

    end = now
    start = end - default_delta
    return start, end


def _is_legacy_zero_duration_session(payload: dict[str, Any], *, started_at: str, ended_at: str) -> bool:
    if payload.get("duration_seconds") is not None:
        return False
    if payload.get("session_duration_minutes") is not None:
        return False
    start = _parse_iso_timestamp(started_at)
    end = _parse_iso_timestamp(ended_at)
    if start is None or end is None:
        return False
    return _coerce_utc(start) == _coerce_utc(end)


def _session_duration_seconds(started_at: str, ended_at: str, *, strict: bool = True) -> int:
    start = _parse_iso_timestamp(started_at)
    end = _parse_iso_timestamp(ended_at)
    if start is None:
        if strict:
            raise ValueError(f"invalid_started_at: {started_at}")
        return 0
    if end is None:
        if strict:
            raise ValueError(f"invalid_ended_at: {ended_at}")
        return 0
    duration = int((end - start).total_seconds())
    if duration < 0:
        if strict:
            raise ValueError(f"invalid_session_window: ended_at ({ended_at}) is earlier than started_at ({started_at})")
        return 0
    return duration


def _parse_iso_timestamp(ts: str) -> datetime | None:
    try:
        return datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
    except ValueError:
        return None
