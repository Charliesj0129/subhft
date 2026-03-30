"""Diagnostic 1: Regime classifier backtest validation (Direction C, R24).

Replays the RegimeClassifier against L1 research data and measures:
1. Regime distribution (% FAVORABLE / NEUTRAL / ADVERSE per day)
2. Forward price movement magnitude by regime (FAVORABLE vs ADVERSE)
3. Transition frequency (transitions/hour)
4. Kill gates:
   - FAVORABLE vs ADVERSE separation in |forward_30s| must be > 1 bps
   - Trade frequency reduction from ADVERSE gating must be < 50%
   - Transitions must be < 20/hour

Usage:
    python -m research.experiments.validations.r24_diagnostics.diagnostic_1_regime_backtest
"""

from __future__ import annotations

import os
from pathlib import Path

import numpy as np

from hft_platform.execution.regime_classifier import Regime, RegimeClassifier

# -------------------------------------------------------------------
# Configuration
# -------------------------------------------------------------------
DATA_DIR = Path("research/data/raw")
SYMBOLS = ["txfd6", "tmfd6"]
FORWARD_WINDOW_NS = 30_000_000_000  # 30s
RET_AUTOCOV_WINDOW = 40
EMA_ALPHA_50 = 2.0 / 51.0
SPREAD_EMA300S_ALPHA = 2.0 / 2401.0


def compute_feature_tuples(data: np.ndarray) -> list[tuple[int, ...] | None]:
    """Compute feature tuples for each tick matching FeatureEngine v3 layout.

    Returns list of 27-element tuples (or None during warmup).
    Only computes the indices used by RegimeClassifier:
      [17] ret_autocov_5s_x1e6
      [18] tob_survival_ms
      [21] toxicity_ema50_x1000
      [26] spread_ema300s
    Other indices are filled with 0.
    """
    n = len(data)
    mid = data["mid_price"]
    bid = data["bid_px"]
    ask = data["ask_px"]
    ts = data["local_ts"]
    spread = ask - bid

    result: list[tuple[int, ...] | None] = [None] * n
    warmup = max(RET_AUTOCOV_WINDOW + 2, 2400)

    # State for ret_autocov
    ret_buf = np.zeros(RET_AUTOCOV_WINDOW, dtype=np.float64)
    buf_pos = 0
    buf_count = 0
    prev_mid = mid[0]

    # State for tob_survival
    prev_bid = bid[0]
    prev_ask = ask[0]
    last_change_ns = ts[0]

    # State for spread_ema300s
    s_ema = float(spread[0])

    for i in range(n):
        # ret_autocov
        delta = mid[i] - prev_mid
        ret_buf[buf_pos] = delta
        buf_pos = (buf_pos + 1) % RET_AUTOCOV_WINDOW
        buf_count = min(buf_count + 1, RET_AUTOCOV_WINDOW)
        prev_mid = mid[i]

        autocov_val = 0
        if buf_count >= RET_AUTOCOV_WINDOW:
            rets = np.empty(buf_count, dtype=np.float64)
            for j in range(buf_count):
                rets[j] = ret_buf[(buf_pos - buf_count + j) % RET_AUTOCOV_WINDOW]
            mean_r = rets.mean()
            if len(rets) > 1:
                autocov_val = int(np.mean((rets[1:] - mean_r) * (rets[:-1] - mean_r)) * 1e6)

        # tob_survival
        if bid[i] != prev_bid or ask[i] != prev_ask:
            last_change_ns = ts[i]
            prev_bid = bid[i]
            prev_ask = ask[i]
        tob_ms = int((ts[i] - last_change_ns) / 1_000_000)

        # spread_ema300s
        s_ema = SPREAD_EMA300S_ALPHA * float(spread[i]) + (1.0 - SPREAD_EMA300S_ALPHA) * s_ema
        spread_val = int(s_ema * 10000)  # scale x10000

        # toxicity: set to 0 (no trade data in BidAsk files)
        toxicity_val = 0

        if i >= warmup:
            vals = [0] * 27
            vals[17] = autocov_val
            vals[18] = tob_ms
            vals[21] = toxicity_val
            vals[26] = spread_val
            result[i] = tuple(vals)

    return result


def run_backtest() -> dict:
    """Run regime backtest across all available data."""
    all_results: dict[str, list] = {}

    rc = RegimeClassifier(
        tob_survival_adverse_ms=50,
        tob_survival_favorable_ms=500,
        ret_autocov_calm_threshold=500,
        toxicity_adverse_threshold=400,
        spread_wide_threshold=0,
        holdoff_ns=5_000_000_000,  # 5s holdoff to suppress rapid transitions
    )

    for symbol_dir in SYMBOLS:
        sym_path = DATA_DIR / symbol_dir
        if not sym_path.exists():
            continue

        npy_files = sorted(sym_path.glob(f"{symbol_dir.upper()}_*_l1.npy"))
        npy_files = [f for f in npy_files if "_all_" not in f.name and "_march_" not in f.name]

        for fpath in npy_files:
            date_str = fpath.stem.split("_")[1]
            print(f"Processing {symbol_dir.upper()} {date_str}...")

            data = np.load(str(fpath), allow_pickle=True)
            if len(data) < 3000:
                continue

            n = len(data)
            mid = data["mid_price"]
            ts = data["local_ts"]

            # Compute feature tuples
            feature_tuples = compute_feature_tuples(data)

            # Compute forward returns
            fwd_ret = np.full(n, np.nan)
            j = 0
            for i in range(n):
                target_ts = ts[i] + FORWARD_WINDOW_NS
                while j < n and ts[j] < target_ts:
                    j += 1
                if j < n:
                    fwd_ret[i] = mid[j] - mid[i]
                j = max(j - 1, i + 1)

            # Classify each tick (with holdoff debouncing)
            rc.reset()
            regimes = np.full(n, -99, dtype=np.int8)
            for i in range(n):
                ft = feature_tuples[i]
                regime = rc.classify(ft, ts_ns=int(ts[i]))
                regimes[i] = int(regime)

            # Compute stats per regime
            valid = ~np.isnan(fwd_ret) & (regimes != -99)
            abs_fwd = np.abs(fwd_ret)

            day_result = {
                "symbol": symbol_dir.upper(),
                "date": date_str,
                "n_ticks": n,
                "transitions": rc.transition_count,
            }

            # Session duration for transitions/hour
            duration_ns = int(ts[-1]) - int(ts[0])
            duration_hours = duration_ns / 3.6e12
            day_result["transitions_per_hour"] = (
                rc.transition_count / duration_hours if duration_hours > 0 else 0
            )

            for regime_val, regime_name in [
                (Regime.FAVORABLE, "favorable"),
                (Regime.NEUTRAL, "neutral"),
                (Regime.ADVERSE, "adverse"),
            ]:
                mask = valid & (regimes == int(regime_val))
                count = int(mask.sum())
                day_result[f"{regime_name}_count"] = count
                day_result[f"{regime_name}_pct"] = 100.0 * count / valid.sum() if valid.sum() > 0 else 0
                if count > 0:
                    day_result[f"{regime_name}_mean_abs_fwd"] = float(np.mean(abs_fwd[mask]))
                    day_result[f"{regime_name}_median_abs_fwd"] = float(np.median(abs_fwd[mask]))
                else:
                    day_result[f"{regime_name}_mean_abs_fwd"] = 0.0
                    day_result[f"{regime_name}_median_abs_fwd"] = 0.0

            if symbol_dir not in all_results:
                all_results[symbol_dir] = []
            all_results[symbol_dir].append(day_result)

    return all_results


def format_results(all_results: dict) -> str:
    """Format as markdown report."""
    lines = [
        "# Diagnostic 1: Regime Classifier Backtest (Direction C, R24)",
        "",
        "**Date**: 2026-03-29",
        "**Classifier**: RegimeClassifier(tob_adverse=50ms, tob_favorable=500ms, "
        "autocov_calm=500, tox_adverse=400)",
        "",
        "## Kill Gates",
        "",
        "1. FAVORABLE vs ADVERSE |fwd_30s| separation > 1 pt",
        "2. ADVERSE gating removes < 50% of ticks",
        "3. Transitions < 20/hour",
        "",
    ]

    for sym_key, day_results in sorted(all_results.items()):
        sym = sym_key.upper()
        lines.append(f"## {sym}")
        lines.append("")
        lines.append("| Date | FAV% | NEU% | ADV% | FAV |fwd| | ADV |fwd| | Separation | Trans/hr |")
        lines.append("|------|------|------|------|---------|---------|------------|----------|")

        all_fav_fwd = []
        all_adv_fwd = []
        all_trans_hr = []
        all_adv_pct = []

        for r in day_results:
            sep = r["adverse_mean_abs_fwd"] - r["favorable_mean_abs_fwd"]
            lines.append(
                f"| {r['date']} | {r['favorable_pct']:.1f}% | {r['neutral_pct']:.1f}% | "
                f"{r['adverse_pct']:.1f}% | {r['favorable_mean_abs_fwd']:.2f} | "
                f"{r['adverse_mean_abs_fwd']:.2f} | {sep:+.2f} | "
                f"{r['transitions_per_hour']:.1f} |"
            )
            all_fav_fwd.append(r["favorable_mean_abs_fwd"])
            all_adv_fwd.append(r["adverse_mean_abs_fwd"])
            all_trans_hr.append(r["transitions_per_hour"])
            all_adv_pct.append(r["adverse_pct"])

        lines.append("")
        mean_fav = np.mean(all_fav_fwd) if all_fav_fwd else 0
        mean_adv = np.mean(all_adv_fwd) if all_adv_fwd else 0
        mean_sep = mean_adv - mean_fav
        mean_trans = np.mean(all_trans_hr) if all_trans_hr else 0
        mean_adv_pct = np.mean(all_adv_pct) if all_adv_pct else 0

        lines.append(f"**{sym} Summary**:")
        lines.append(f"- Mean |fwd| in FAVORABLE: {mean_fav:.2f} pts")
        lines.append(f"- Mean |fwd| in ADVERSE: {mean_adv:.2f} pts")
        lines.append(f"- **Mean separation**: {mean_sep:+.2f} pts")
        lines.append(f"- Mean ADVERSE%: {mean_adv_pct:.1f}%")
        lines.append(f"- Mean transitions/hour: {mean_trans:.1f}")
        lines.append("")

        # Kill gate checks
        kg1_pass = mean_sep > 1.0
        kg2_pass = mean_adv_pct < 50.0
        kg3_pass = mean_trans < 20.0

        lines.append(f"**Kill Gate 1** (separation > 1 pt): {'PASS' if kg1_pass else 'FAIL'} ({mean_sep:+.2f})")
        lines.append(f"**Kill Gate 2** (ADVERSE < 50%): {'PASS' if kg2_pass else 'FAIL'} ({mean_adv_pct:.1f}%)")
        lines.append(f"**Kill Gate 3** (transitions < 20/hr): {'PASS' if kg3_pass else 'FAIL'} ({mean_trans:.1f})")
        lines.append("")

    return "\n".join(lines)


if __name__ == "__main__":
    os.chdir(Path(__file__).resolve().parents[4])
    all_results = run_backtest()
    report = format_results(all_results)
    print(report)

    out_path = Path("docs/alpha-research/r24/diagnostic_1_regime_backtest.md")
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(report)
    print(f"\nReport saved to {out_path}")
