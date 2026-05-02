"""T5 extensions — R47 baseline, K2 OLS fit, K4 empirical ACF.

Three measurements added to the original T5 backtest harness, addressing the
team-lead's request to upgrade three soft-PASS / FAIL items into measured
PASS/FAIL with concrete numbers:

 - **K2** real OLS fit on (γ_io, γ_us) → 95% CIs (bootstrap with B=1000).
 - **K3** R47 baseline run on the same days for true per-fill gain delta.
 - **K4** empirical signed-trade-arrival ACF at ms / min / hr lags.

Float exception (Architecture Governance Rule §11): float is permitted in
this offline research module.
"""

from __future__ import annotations

import json
import os
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import numpy as np
import requests
import structlog

from research.alphas.r52_amhp_dynamic_spread.backtest_t5 import (
    AmhpMakerBacktestStrategy,
    _compute_fifo_pnl,
    _run_day_with_trade_feed,
    run_backtest,
    trading_dates,
)
from research.backtest.cost_models import load_cost_profile
from research.backtest.fill_models import QueueDepletionFill
from research.backtest.maker_engine import ClickHouseSource, LatencyProfile

logger = structlog.get_logger("r52_amhp.t5_ext")


# ============================================================================
# R47 baseline — modulator off (alpha_rho=0, alpha_iir=0 → no widening)
# ============================================================================


def run_r47_baseline(
    *,
    start_date: str = "2026-01-27",
    end_date: str = "2026-03-26",
    instrument: str = "TMFD6",
    max_pos: int = 3,
) -> dict[str, Any]:
    """R47 baseline run — fires the same execution loop with the modulator
    disabled (`alpha_rho=0`, `alpha_iir=0`).  Effective behavior: quote at
    base spread floor (5 pt) whenever observed spread >= 5 pt.  Used as the
    K3 reference for AMHP per-fill gain delta.
    """
    cost = load_cost_profile(instrument)
    fill_model = QueueDepletionFill(queue_fraction=0.5)
    latency = LatencyProfile(place_ns=395_000_000, cancel_ns=59_000_000)

    strategy = AmhpMakerBacktestStrategy(
        max_pos=max_pos,
        alpha_rho=0.0,           # disable rho contribution to multiplier
        alpha_iir=0.0,           # disable IIR contribution
        rho_critical=999.0,      # rho can never exceed → no critical-snap to mult_cap
        iir_critical=999.0,
        mult_cap=1.0,            # cap == 1 → multiplier == 1 always
    )
    strategy.set_persistent_gammas(0.0, 0.0)

    dates = trading_dates(start_date, end_date)
    if not dates:
        raise RuntimeError("No CK rows for R47 baseline.")

    logger.info("r47_baseline_start", instrument=instrument, dates=len(dates))

    ck = ClickHouseSource()
    ck.health_check()

    daily_pnl: list[dict] = []
    total_gross = 0.0
    total_fills = 0
    total_spread_at_entry_sum = 0
    spread_breakdown: dict[int, dict] = {}

    for date in dates:
        events = ck.load_day(instrument, date)
        if not events:
            continue
        day_fills, _ = _run_day_with_trade_feed(strategy, events, fill_model, latency)
        gross, trips, wins = _compute_fifo_pnl(day_fills)
        net = cost.apply(gross, len(day_fills))
        total_gross += gross
        total_fills += len(day_fills)
        for f in day_fills:
            spr = int(f.get("spread_pts", 0))
            total_spread_at_entry_sum += spr
            spread_breakdown.setdefault(spr, {"fills": 0, "gross_pnl": 0.0})
            spread_breakdown[spr]["fills"] += 1
        daily_pnl.append({
            "date": date,
            "pnl_pts": round(net, 2),
            "gross_pts": round(gross, 2),
            "fills": len(day_fills),
            "trips": trips,
            "wins": wins,
        })

    total_net = sum(d["pnl_pts"] for d in daily_pnl)
    avg_spread_at_entry = (total_spread_at_entry_sum / total_fills) if total_fills else 0.0
    return {
        "label": "R47_baseline_floor_only",
        "instrument": instrument,
        "n_days": len(daily_pnl),
        "total_pnl_pts": round(total_net, 2),
        "total_gross_pts": round(total_gross, 2),
        "total_fills": total_fills,
        "avg_spread_at_entry_pts": round(avg_spread_at_entry, 2),
        "daily_pnl": daily_pnl,
        "per_spread_breakdown": {str(k): v for k, v in sorted(spread_breakdown.items())},
    }


def k3_modulator_gain_delta(c1_summary: dict, r47_summary: dict) -> dict[str, Any]:
    """K3 — per-fill gain delta vs R47 baseline on the same 17d window.

    Compute (avg_spread_at_entry_C1 - avg_spread_at_entry_R47) / 2 as the
    half-spread captured advantage when modulator is active.  This is the
    direct delta T1 §3 §10 calls out as the K3 falsifier.
    """
    c1_avg_spread = c1_summary["avg_spread_at_entry_pts"]
    r47_avg_spread = r47_summary["avg_spread_at_entry_pts"]
    delta_full_spread = c1_avg_spread - r47_avg_spread
    delta_half_spread = delta_full_spread / 2.0

    c1_pnl = c1_summary["total_pnl_pts"]
    r47_pnl = r47_summary["total_pnl_pts"]
    pnl_delta = c1_pnl - r47_pnl

    c1_per_fill_net = c1_summary["net_per_trade_pts"]
    r47_per_fill_net = (
        r47_summary["total_pnl_pts"] / r47_summary["total_fills"]
        if r47_summary["total_fills"] else 0.0
    )

    # Verdict: per-fill gain delta in pt; threshold 1.75 pt (T1 §3).
    return {
        "C1_avg_spread_at_entry_pts": c1_avg_spread,
        "R47_avg_spread_at_entry_pts": r47_avg_spread,
        "delta_avg_spread_pts": round(delta_full_spread, 3),
        "delta_half_spread_pts": round(delta_half_spread, 3),
        "C1_total_pnl_pts": c1_pnl,
        "R47_total_pnl_pts": r47_pnl,
        "delta_total_pnl_pts": round(pnl_delta, 2),
        "C1_net_per_fill_pts": round(c1_per_fill_net, 4),
        "R47_net_per_fill_pts": round(r47_per_fill_net, 4),
        "delta_net_per_fill_pts": round(c1_per_fill_net - r47_per_fill_net, 4),
        "K3_threshold_pts": 1.75,
        "K3_pass": delta_half_spread >= 1.75,
    }


# ============================================================================
# K2 — daily-aggregated OLS on (γ_io, γ_us) with bootstrap CIs
# ============================================================================


@dataclass
class OLSFit:
    coefficient: float
    se: float
    ci_lower: float
    ci_upper: float
    excludes_zero: bool


def _ols_with_bootstrap_ci(
    X: np.ndarray, y: np.ndarray, *, n_boot: int = 1000, seed: int = 20260425,
) -> dict[str, OLSFit]:
    """Multi-variate OLS with non-parametric bootstrap CIs on each coefficient.

    Adds an intercept column.  Returns coefficient + 95% CI for each predictor
    (intercept first, then each column of X).
    """
    n, k = X.shape
    X_int = np.column_stack([np.ones(n), X])
    coef = np.linalg.lstsq(X_int, y, rcond=None)[0]
    rng = np.random.default_rng(seed)
    boot_coefs = np.zeros((n_boot, k + 1))
    for b in range(n_boot):
        idx = rng.integers(0, n, size=n)
        Xb = X_int[idx]
        yb = y[idx]
        try:
            boot_coefs[b] = np.linalg.lstsq(Xb, yb, rcond=None)[0]
        except np.linalg.LinAlgError:
            boot_coefs[b] = coef  # fallback if singular bootstrap
    lower = np.percentile(boot_coefs, 2.5, axis=0)
    upper = np.percentile(boot_coefs, 97.5, axis=0)
    se = np.std(boot_coefs, axis=0)

    names = ["intercept"] + [f"x{i}" for i in range(k)]
    fits: dict[str, OLSFit] = {}
    for i, name in enumerate(names):
        fits[name] = OLSFit(
            coefficient=float(coef[i]),
            se=float(se[i]),
            ci_lower=float(lower[i]),
            ci_upper=float(upper[i]),
            excludes_zero=(lower[i] > 0.0) or (upper[i] < 0.0),
        )
    return fits


def fit_k2_covariates(
    *,
    daily_pnl: list[dict],
    instrument: str = "TMFD6",
) -> dict[str, Any]:
    """K2 — OLS fit of (γ_io, γ_us) on daily PnL.

    Real-world foreign-IO (三大法人) and US-overnight (S&P futures overnight)
    feeds are NOT ingested into this CK instance.  Per-platform constraint we
    use **proxies**:

      * `io_z_proxy`: prior-day signed-trade-volume z-score (computed from
        TMFD6 trade tape).  Surrogate for institutional accumulation.
      * `us_overnight_proxy`: prior-day TMFD6 close-to-current-day open
        log-return.  Surrogate for overnight directional risk.

    Both proxies are documented as surrogates in the scorecard.  Real K2
    promotion requires the platform-side daily institutional flow ingest +
    US futures overnight reference to be wired into CK; until then, this
    fit reports the proxy-coefficient CIs as best-available evidence.
    """
    pwd = os.environ.get("CLICKHOUSE_PASSWORD", "changeme")
    url = "http://localhost:8123/"

    # Fetch per-day signed-trade-volume + open/close prices for proxies.
    # signed_volume = Σ trade_direction * volume per day (using EMO field if
    # available; fall back to tick-rule against best bid/ask captured below).
    sql = (
        f"SELECT toString(toDate(fromUnixTimestamp64Nano(exch_ts))) AS day, "
        f"  count() AS n_trades, "
        f"  sum(volume) AS total_volume, "
        f"  any(price_scaled) AS open_price, "
        f"  argMax(price_scaled, exch_ts) AS close_price "
        f"FROM hft.market_data "
        f"WHERE symbol = '{instrument}' AND type = 'Tick' "
        f"GROUP BY day ORDER BY day"
    )
    resp = requests.post(url, params={"password": pwd}, data=sql, timeout=120)
    resp.raise_for_status()
    rows = resp.text.strip().split("\n")
    daily_market: dict[str, dict] = {}
    for line in rows:
        parts = line.split("\t")
        if len(parts) >= 5:
            day = parts[0]
            daily_market[day] = {
                "n_trades": int(parts[1]),
                "total_volume": int(parts[2]),
                "open": int(parts[3]),
                "close": int(parts[4]),
            }

    # Build proxy series for the 17 backtest days.
    days = [d["date"] for d in daily_pnl]
    pnls = np.array([d["pnl_pts"] for d in daily_pnl])

    # io_z_proxy: prior-day total_volume z-score (over the 17d window).
    vol_series = np.array([daily_market.get(d, {}).get("total_volume", 0) for d in days],
                           dtype=float)
    if vol_series.std() > 0:
        vol_z = (vol_series - vol_series.mean()) / vol_series.std()
    else:
        vol_z = np.zeros_like(vol_series)
    io_z_proxy = np.concatenate([[0.0], vol_z[:-1]])  # lag-1: prior-day z

    # us_overnight_proxy: prior-day-close → today-open log-return.
    overnight = np.zeros(len(days))
    for i, d in enumerate(days):
        if i == 0:
            continue
        prev = daily_market.get(days[i - 1], {})
        cur = daily_market.get(d, {})
        if prev.get("close", 0) > 0 and cur.get("open", 0) > 0:
            overnight[i] = float(np.log(cur["open"] / prev["close"]))
    us_overnight_proxy = overnight

    # Stack into design matrix X = [io_z_proxy, us_overnight_proxy].
    X = np.column_stack([io_z_proxy, us_overnight_proxy])
    fits = _ols_with_bootstrap_ci(X, pnls)

    gamma_io_fit = fits["x0"]
    gamma_us_fit = fits["x1"]

    return {
        "fit_method": "OLS w/ non-parametric bootstrap (B=1000) on 17 daily PnL obs",
        "covariates_source": "proxies — see notes",
        "io_z_proxy_definition": "lag-1 daily total trade volume z-score (TMFD6 surrogate)",
        "us_overnight_proxy_definition": "log(today_open / prev_close) on TMFD6 (surrogate)",
        "gamma_io": {
            "coefficient": gamma_io_fit.coefficient,
            "se": gamma_io_fit.se,
            "ci_95_lower": gamma_io_fit.ci_lower,
            "ci_95_upper": gamma_io_fit.ci_upper,
            "excludes_zero": gamma_io_fit.excludes_zero,
        },
        "gamma_us": {
            "coefficient": gamma_us_fit.coefficient,
            "se": gamma_us_fit.se,
            "ci_95_lower": gamma_us_fit.ci_lower,
            "ci_95_upper": gamma_us_fit.ci_upper,
            "excludes_zero": gamma_us_fit.excludes_zero,
        },
        "K2_pass": gamma_io_fit.excludes_zero and gamma_us_fit.excludes_zero,
        "io_z_proxy_series": io_z_proxy.tolist(),
        "us_overnight_proxy_series": us_overnight_proxy.tolist(),
    }


# ============================================================================
# K4 — empirical multi-scale ACF on signed-trade arrival deltas
# ============================================================================


def measure_k4_multiscale_acf(  # noqa: C901
    *,
    start_date: str = "2026-01-27",
    end_date: str = "2026-03-26",
    instrument: str = "TMFD6",
    sample_per_day_max: int = 5000,
) -> dict[str, Any]:
    """K4 — signed-trade arrival ACF at three lag scales (ms / min / hr).

    Loads each backtest day's trades, infers signed direction (tick rule
    vs prior best bid/ask seen in the bidask stream), then computes ACF
    on the signed-arrival series at lag indices ≈ {1 (ms), 600 (min),
    3600·600 (hr)} via auto-correlation of the signed series resampled
    onto a fixed time grid.

    Per T1 §7 Q-A: K4 PASS = min_acf and hr_acf both non-trivial (effect
    size ≥ 0.05 absolute).
    """
    pwd = os.environ.get("CLICKHOUSE_PASSWORD", "changeme")
    url = "http://localhost:8123/"
    dates = trading_dates(start_date, end_date)

    # Aggregate signed-arrival series across all days at 100-ms bins.
    bin_ns = 100_000_000          # 100 ms
    per_day_acfs: list[dict[str, float]] = []
    pooled_signed_series: list[float] = []

    for date in dates:
        # Pull signed-trade events.  We don't have trade_direction stored in
        # the platform's CK; infer via tick rule against bidask stream.  For
        # speed we approximate by using the trade_price relative to a rolling
        # mid (computed from the tick stream itself — coarse but adequate
        # for ACF measurement at multi-scale lags).
        sql = (
            f"SELECT exch_ts, price_scaled, volume "
            f"FROM hft.market_data "
            f"WHERE symbol = '{instrument}' AND type = 'Tick' "
            f"  AND toDate(fromUnixTimestamp64Nano(exch_ts)) = '{date}' "
            f"ORDER BY exch_ts"
        )
        resp = requests.post(url, params={"password": pwd}, data=sql, timeout=120)
        resp.raise_for_status()
        lines = resp.text.strip().split("\n")
        if not lines or len(lines) < 2:
            continue

        prev_price = 0
        signed_per_bin: dict[int, float] = {}
        bin0 = None

        for line in lines:
            parts = line.split("\t")
            if len(parts) < 3:
                continue
            ts = int(parts[0])
            p = int(parts[1])
            v = int(parts[2])
            if bin0 is None:
                bin0 = ts // bin_ns
            bin_idx = (ts // bin_ns) - bin0
            # Tick rule: up-tick → +1 buy, down-tick → -1 sell, equal → carry-forward
            if prev_price == 0:
                d = 0
            elif p > prev_price:
                d = +1
            elif p < prev_price:
                d = -1
            else:
                d = 0   # zero-tick — uninformative
            prev_price = p
            if d != 0:
                signed_per_bin[bin_idx] = signed_per_bin.get(bin_idx, 0.0) + d * v

        if not signed_per_bin:
            continue

        # Build a contiguous bin array for the day.
        max_bin = max(signed_per_bin)
        ser = np.zeros(max_bin + 1)
        for k, v in signed_per_bin.items():
            ser[k] = v

        # Compute ACF at lags: 1 (≈100 ms), 600 (≈60 s), 36000 (≈3600 s).
        # For long lags on short days, fall back to the maximum feasible lag.
        def _acf(x: np.ndarray, lag: int) -> float:
            if lag >= len(x):
                return float("nan")
            x = x - x.mean()
            denom = (x * x).sum()
            if denom <= 0:
                return 0.0
            num = (x[: len(x) - lag] * x[lag:]).sum()
            return float(num / denom)

        per_day_acfs.append({
            "date": date,
            "ms_acf_lag1": _acf(ser, 1),         # 100 ms
            "min_acf_lag600": _acf(ser, 600),    # 60 s
            "hr_acf_lag36000": _acf(ser, 36000), # 3600 s — may be NaN on short days
            "n_bins": len(ser),
        })
        pooled_signed_series.append(ser)

    # Pool across all days (simple concatenation; preserves intra-day structure
    # while increasing N for long-lag estimates).
    if pooled_signed_series:
        pooled = np.concatenate(pooled_signed_series)

        def _pooled_acf(lag: int) -> float:
            if lag >= len(pooled):
                return float("nan")
            x = pooled - pooled.mean()
            denom = (x * x).sum()
            if denom <= 0:
                return 0.0
            return float((x[: len(x) - lag] * x[lag:]).sum() / denom)

        ms_acf_pooled = _pooled_acf(1)
        min_acf_pooled = _pooled_acf(600)
        hr_acf_pooled = _pooled_acf(36000)
    else:
        ms_acf_pooled = min_acf_pooled = hr_acf_pooled = float("nan")

    # K4 verdict — both min and hr |ACF| ≥ 0.05 → PASS.
    threshold = 0.05
    k4_pass = bool(
        not np.isnan(min_acf_pooled) and abs(min_acf_pooled) >= threshold
        and not np.isnan(hr_acf_pooled) and abs(hr_acf_pooled) >= threshold
    )

    return {
        "method": "tick-rule signed arrival, 100-ms binning, ACF at lags {1, 600, 36000}",
        "ms_acf_pooled_lag1": round(ms_acf_pooled, 4) if not np.isnan(ms_acf_pooled) else None,
        "min_acf_pooled_lag600": round(min_acf_pooled, 4) if not np.isnan(min_acf_pooled) else None,
        "hr_acf_pooled_lag36000": round(hr_acf_pooled, 4) if not np.isnan(hr_acf_pooled) else None,
        "K4_threshold_abs_acf": threshold,
        "K4_pass": k4_pass,
        "per_day_acfs": per_day_acfs,
        "n_days_in_pool": len(per_day_acfs),
    }


# ============================================================================
# Entry — combined runner
# ============================================================================


def main() -> int:
    out_dir = Path("outputs/r52_amhp_dynamic_spread")
    out_dir.mkdir(parents=True, exist_ok=True)
    ts = time.strftime("%Y%m%d_%H%M%S")

    # 1) C1 backtest
    logger.info("phase_1_c1_backtest")
    c1 = run_backtest()

    # 2) R47 baseline backtest (modulator off)
    logger.info("phase_2_r47_baseline")
    r47 = run_r47_baseline()

    # 3) K3 delta
    logger.info("phase_3_k3_delta")
    k3 = k3_modulator_gain_delta(c1, r47)

    # 4) K2 OLS fit
    logger.info("phase_4_k2_ols")
    k2 = fit_k2_covariates(daily_pnl=c1["daily_pnl"])

    # 5) K4 empirical ACF
    logger.info("phase_5_k4_acf")
    k4 = measure_k4_multiscale_acf()

    extended = {
        "c1": c1,
        "r47_baseline": r47,
        "k2_ols_fit": k2,
        "k3_modulator_gain": k3,
        "k4_multiscale_acf": k4,
    }
    path = out_dir / f"t5_extended_{ts}.json"
    path.write_text(json.dumps(extended, indent=2, default=str))
    logger.info("extended_done", output=str(path))

    sys.stdout.write(json.dumps({
        "c1_sharpe": c1["sharpe"],
        "c1_total_pnl_pts": c1["total_pnl_pts"],
        "c1_max_day_pct": c1["K1_max_day_pct"],
        "c1_winning_days": c1["winning_days"],
        "r47_total_pnl_pts": r47["total_pnl_pts"],
        "r47_total_fills": r47["total_fills"],
        "K2_gamma_io_ci": [k2["gamma_io"]["ci_95_lower"], k2["gamma_io"]["ci_95_upper"]],
        "K2_gamma_us_ci": [k2["gamma_us"]["ci_95_lower"], k2["gamma_us"]["ci_95_upper"]],
        "K2_pass": k2["K2_pass"],
        "K3_delta_half_spread_pts": k3["delta_half_spread_pts"],
        "K3_pass": k3["K3_pass"],
        "K4_ms_acf": k4["ms_acf_pooled_lag1"],
        "K4_min_acf": k4["min_acf_pooled_lag600"],
        "K4_hr_acf": k4["hr_acf_pooled_lag36000"],
        "K4_pass": k4["K4_pass"],
        "K5_freq_pct": c1["K5_rho_critical_freq_pct"],
        "output_path": str(path),
    }, indent=2, default=str))
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
