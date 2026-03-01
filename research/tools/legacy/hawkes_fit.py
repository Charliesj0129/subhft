import argparse
import json
import os
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

import clickhouse_connect
import numpy as np


@dataclass
class FitResult:
    mu: float
    alpha: float
    beta: float
    loglik: float
    branching_ratio: float
    n_events: int
    horizon_s: float
    base_rate: float


def _parse_time(value: str | None, unit: str) -> int | None:
    if value is None:
        return None
    if value.isdigit():
        n = int(value)
        if unit == "auto":
            if n > 10**14:  # ns
                return n
            if n > 10**11:  # ms
                return n * 1_000_000
            return n * 1_000_000_000
        if unit == "ns":
            return n
        if unit == "ms":
            return n * 1_000_000
        if unit == "s":
            return n * 1_000_000_000
    dt = datetime.fromisoformat(value)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return int(dt.timestamp() * 1_000_000_000)


def _fetch_event_times(
    host: str,
    port: int,
    user: str,
    password: str,
    database: str,
    symbol: str,
    event_type: str,
    ts_field: str,
    start_ts: int,
    end_ts: int,
    limit: int,
) -> np.ndarray:
    client = clickhouse_connect.get_client(
        host=host,
        port=port,
        username=user,
        password=password,
        database=database,
    )
    query = f"""
        SELECT {ts_field}
        FROM {database}.market_data
        WHERE symbol = %(symbol)s
          AND type = %(event_type)s
          AND {ts_field} >= %(start_ts)s
          AND {ts_field} <= %(end_ts)s
        ORDER BY {ts_field}
        LIMIT %(limit)s
    """
    result = client.query(
        query,
        parameters={
            "symbol": symbol,
            "event_type": event_type,
            "start_ts": start_ts,
            "end_ts": end_ts,
            "limit": limit,
        },
    )
    rows = result.result_rows
    if not rows:
        return np.array([], dtype=np.int64)
    return np.array([int(r[0]) for r in rows], dtype=np.int64)


def _hawkes_loglik(times: np.ndarray, mu: float, alpha: float, beta: float, h_cache: np.ndarray) -> float:
    intensity = mu + alpha * h_cache
    if np.any(intensity <= 0):
        return -np.inf
    horizon = times[-1]
    tail_terms = 1.0 - np.exp(-beta * (horizon - times))
    integral = mu * horizon + (alpha / beta) * np.sum(tail_terms)
    return np.sum(np.log(intensity)) - integral


def _compute_h_cache(times: np.ndarray, beta: float) -> np.ndarray:
    h = np.zeros_like(times)
    for i in range(1, len(times)):
        decay = np.exp(-beta * (times[i] - times[i - 1]))
        h[i] = decay * (1.0 + h[i - 1])
    return h


def _grid_fit(times: np.ndarray, beta_grid: np.ndarray, eta_grid: np.ndarray, mu_grid: np.ndarray) -> FitResult:
    horizon = times[-1]
    base_rate = len(times) / horizon
    best = FitResult(
        mu=0.0,
        alpha=0.0,
        beta=0.0,
        loglik=-np.inf,
        branching_ratio=0.0,
        n_events=len(times),
        horizon_s=horizon,
        base_rate=base_rate,
    )
    for beta in beta_grid:
        h_cache = _compute_h_cache(times, beta)
        for eta in eta_grid:
            alpha = eta * beta
            for mu in mu_grid:
                loglik = _hawkes_loglik(times, mu, alpha, beta, h_cache)
                if loglik > best.loglik:
                    best = FitResult(
                        mu=mu,
                        alpha=alpha,
                        beta=beta,
                        loglik=loglik,
                        branching_ratio=eta,
                        n_events=len(times),
                        horizon_s=horizon,
                        base_rate=base_rate,
                    )
    return best


def _build_grids(times: np.ndarray, beta_count: int, eta_count: int, mu_count: int) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    deltas = np.diff(times)
    median_dt = float(np.median(deltas[deltas > 0])) if np.any(deltas > 0) else 1.0
    beta_min = max(1e-6, 0.1 / median_dt)
    beta_max = max(beta_min * 10, 10.0 / median_dt)
    beta_grid = np.logspace(np.log10(beta_min), np.log10(beta_max), beta_count)
    eta_grid = np.linspace(0.05, 0.95, eta_count)
    base_rate = len(times) / times[-1]
    mu_base = base_rate * (1.0 - np.median(eta_grid))
    mu_grid = mu_base * np.linspace(0.2, 2.0, mu_count)
    mu_grid = np.clip(mu_grid, 1e-8, None)
    return beta_grid, eta_grid, mu_grid


def _refine_grids(best: FitResult, beta_count: int, eta_count: int, mu_count: int) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    beta_grid = np.logspace(np.log10(best.beta / 2), np.log10(best.beta * 2), beta_count)
    eta_grid = np.linspace(max(0.01, best.branching_ratio * 0.7), min(0.99, best.branching_ratio * 1.3), eta_count)
    mu_grid = np.linspace(max(1e-8, best.mu * 0.7), best.mu * 1.3, mu_count)
    return beta_grid, eta_grid, mu_grid


def fit_hawkes(
    times_ns: np.ndarray,
    beta_count: int,
    eta_count: int,
    mu_count: int,
    refine: bool,
) -> FitResult:
    if len(times_ns) < 100:
        raise ValueError("need at least 100 events for a stable fit")
    times = (times_ns - times_ns[0]) / 1_000_000_000.0
    if times[-1] <= 0:
        raise ValueError("invalid time range for events")
    beta_grid, eta_grid, mu_grid = _build_grids(times, beta_count, eta_count, mu_count)
    best = _grid_fit(times, beta_grid, eta_grid, mu_grid)
    if refine:
        beta_grid, eta_grid, mu_grid = _refine_grids(best, beta_count, eta_count, mu_count)
        best = _grid_fit(times, beta_grid, eta_grid, mu_grid)
    return best


def main() -> None:
    parser = argparse.ArgumentParser(description="Fit a univariate Hawkes process (exponential kernel).")
    parser.add_argument("--symbol", required=True)
    parser.add_argument("--event-type", default="BidAsk")
    parser.add_argument("--ts-field", default="exch_ts")
    parser.add_argument("--start")
    parser.add_argument("--end")
    parser.add_argument("--time-unit", choices=["auto", "ns", "ms", "s"], default="auto")
    parser.add_argument("--lookback-seconds", type=int, default=3600)
    parser.add_argument("--limit", type=int, default=200000)
    parser.add_argument("--beta-grid", type=int, default=20)
    parser.add_argument("--eta-grid", type=int, default=20)
    parser.add_argument("--mu-grid", type=int, default=20)
    parser.add_argument("--refine", action="store_true")
    parser.add_argument("--output", default="")
    parser.add_argument("--host", default=os.getenv("HFT_CLICKHOUSE_HOST") or os.getenv("CLICKHOUSE_HOST") or "localhost")
    parser.add_argument("--port", type=int, default=int(os.getenv("HFT_CLICKHOUSE_PORT") or os.getenv("CLICKHOUSE_PORT") or 8123))
    parser.add_argument("--user", default=os.getenv("HFT_CLICKHOUSE_USER") or os.getenv("CLICKHOUSE_USER") or "default")
    parser.add_argument("--password", default=os.getenv("HFT_CLICKHOUSE_PASSWORD") or os.getenv("CLICKHOUSE_PASSWORD") or "")
    parser.add_argument("--database", default=os.getenv("HFT_CLICKHOUSE_DB") or "hft")
    parser.add_argument("--downsample", type=int, default=1)
    args = parser.parse_args()

    now = datetime.now(timezone.utc)
    end_ts = _parse_time(args.end, args.time_unit)
    if end_ts is None:
        end_ts = int(now.timestamp() * 1_000_000_000)
    start_ts = _parse_time(args.start, args.time_unit)
    if start_ts is None:
        start_ts = int((now - timedelta(seconds=args.lookback_seconds)).timestamp() * 1_000_000_000)

    times = _fetch_event_times(
        host=args.host,
        port=args.port,
        user=args.user,
        password=args.password,
        database=args.database,
        symbol=args.symbol,
        event_type=args.event_type,
        ts_field=args.ts_field,
        start_ts=start_ts,
        end_ts=end_ts,
        limit=args.limit,
    )
    if len(times) == 0:
        raise SystemExit("no events found for the specified query")
    if args.downsample > 1:
        times = times[:: args.downsample]

    result = fit_hawkes(times, args.beta_grid, args.eta_grid, args.mu_grid, args.refine)
    payload = {
        "symbol": args.symbol,
        "event_type": args.event_type,
        "ts_field": args.ts_field,
        "start_ts": start_ts,
        "end_ts": end_ts,
        "mu": result.mu,
        "alpha": result.alpha,
        "beta": result.beta,
        "branching_ratio": result.branching_ratio,
        "loglik": result.loglik,
        "n_events": result.n_events,
        "horizon_s": result.horizon_s,
        "base_rate": result.base_rate,
    }
    output = json.dumps(payload, indent=2)
    if args.output:
        with open(args.output, "w", encoding="utf-8") as f:
            f.write(output + "\n")
    else:
        print(output)


if __name__ == "__main__":
    main()
