"""Diagnostic 0a: Feature vs fill-quality correlation for Direction C.

Computes Spearman correlation between candidate Direction C features
(toxicity proxy, ret_autocov, tob_survival, spread_ema300s) and realized
adverse movement (signed price change 30s after each tick).

Since ClickHouse is offline, computes features directly from L1 numpy
research data files, replicating FeatureEngine v3 logic for the relevant
features.

Kill gate: If no feature shows |rho| > 0.05, Direction C is dead.

Usage:
    python -m research.experiments.validations.r24_diagnostics.diagnostic_0a_fill_quality
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import numpy as np
from scipy import stats

# -------------------------------------------------------------------
# Configuration
# -------------------------------------------------------------------
DATA_DIR = Path("research/data/raw")
SYMBOLS = ["txfd6", "tmfd6"]
FORWARD_WINDOW_NS = 30_000_000_000  # 30 seconds
EMA_ALPHA_8 = 2.0 / (8 + 1)  # ~0.222, matches FeatureEngine ema8
EMA_ALPHA_50 = 2.0 / (50 + 1)  # ~0.039, matches toxicity ema50
RET_AUTOCOV_WINDOW = 40  # ~5s at 125ms tick cadence
SPREAD_EMA300S_ALPHA = 2.0 / (2400 + 1)  # 300s at 125ms cadence

# -------------------------------------------------------------------
# Feature computation from L1 data
# -------------------------------------------------------------------


def compute_features(data: np.ndarray) -> dict[str, np.ndarray]:
    """Compute Direction C candidate features from L1 quote data.

    Features:
    1. toxicity_proxy: EMA50 of signed volume imbalance (using tick rule as proxy).
    2. ret_autocov_5s: Autocovariance of mid_price returns over 40 ticks.
    3. tob_survival_ms: Time since last best price change.
    4. spread_ema300s: EMA of spread with 300s window.

    Returns dict of feature_name -> array (same length as data, NaN where not warm).
    """
    n = len(data)
    mid = data["mid_price"]
    bid = data["bid_px"]
    ask = data["ask_px"]
    ts = data["local_ts"]
    volume = data["volume"]
    spread = ask - bid

    # Pre-allocate output arrays
    toxicity_proxy = np.full(n, np.nan)
    ret_autocov = np.full(n, np.nan)
    tob_survival = np.full(n, np.nan)
    spread_ema = np.full(n, np.nan)

    # --- Toxicity proxy (EMA50 of signed volume / total volume) ---
    # Tick rule: price up → buy, price down → sell
    signed_vol_ema = 0.0
    total_vol_ema = 1.0  # avoid div-by-zero
    prev_mid = mid[0]
    alpha_tox = EMA_ALPHA_50
    for i in range(n):
        cur_mid = mid[i]
        vol = float(volume[i])
        sign = 1.0 if cur_mid > prev_mid else (-1.0 if cur_mid < prev_mid else 0.0)
        signed_vol = sign * vol
        signed_vol_ema = alpha_tox * signed_vol + (1.0 - alpha_tox) * signed_vol_ema
        total_vol_ema = alpha_tox * abs(vol) + (1.0 - alpha_tox) * total_vol_ema
        if i >= 50 and total_vol_ema > 0:
            toxicity_proxy[i] = signed_vol_ema / total_vol_ema
        prev_mid = cur_mid

    # --- Return autocovariance (40-tick window) ---
    ret_buf = np.zeros(RET_AUTOCOV_WINDOW, dtype=np.float64)
    buf_pos = 0
    buf_count = 0
    prev_mid2 = mid[0]
    for i in range(n):
        delta = mid[i] - prev_mid2
        ret_buf[buf_pos] = delta
        buf_pos = (buf_pos + 1) % RET_AUTOCOV_WINDOW
        buf_count = min(buf_count + 1, RET_AUTOCOV_WINDOW)
        prev_mid2 = mid[i]
        if buf_count >= RET_AUTOCOV_WINDOW + 2:
            # Compute autocovariance: cov(r_t, r_{t-1})
            rets = np.empty(buf_count, dtype=np.float64)
            for j in range(buf_count):
                rets[j] = ret_buf[(buf_pos - buf_count + j) % RET_AUTOCOV_WINDOW]
            mean_r = rets.mean()
            autocov = np.mean((rets[1:] - mean_r) * (rets[:-1] - mean_r))
            ret_autocov[i] = autocov * 1e6  # scale like x1e6
        elif buf_count >= RET_AUTOCOV_WINDOW:
            # Enough data but be safe
            rets = np.empty(buf_count, dtype=np.float64)
            for j in range(buf_count):
                rets[j] = ret_buf[(buf_pos - buf_count + j) % RET_AUTOCOV_WINDOW]
            mean_r = rets.mean()
            if len(rets) > 1:
                autocov = np.mean((rets[1:] - mean_r) * (rets[:-1] - mean_r))
                ret_autocov[i] = autocov * 1e6

    # --- TOB survival (ms since last best price change) ---
    prev_bid = bid[0]
    prev_ask = ask[0]
    last_change_ns = ts[0]
    for i in range(n):
        if bid[i] != prev_bid or ask[i] != prev_ask:
            last_change_ns = ts[i]
            prev_bid = bid[i]
            prev_ask = ask[i]
        elapsed_ms = (ts[i] - last_change_ns) / 1_000_000
        if i >= 2:
            tob_survival[i] = elapsed_ms

    # --- Spread EMA 300s ---
    s_ema = float(spread[0])
    alpha_s = SPREAD_EMA300S_ALPHA
    for i in range(n):
        s_ema = alpha_s * float(spread[i]) + (1.0 - alpha_s) * s_ema
        if i >= 2400:  # warmup
            spread_ema[i] = s_ema

    return {
        "toxicity_proxy": toxicity_proxy,
        "ret_autocov_5s_x1e6": ret_autocov,
        "tob_survival_ms": tob_survival,
        "spread_ema300s": spread_ema,
    }


def compute_forward_return(data: np.ndarray) -> np.ndarray:
    """Compute forward price change 30s ahead (adverse movement proxy).

    Returns signed mid-price change: positive = price went up.
    For a BUY fill, positive forward return = favorable (not adverse).
    For a SELL fill, negative forward return = favorable.
    We use absolute forward return as the 'adverse movement magnitude'.
    """
    n = len(data)
    mid = data["mid_price"]
    ts = data["local_ts"]
    fwd_ret = np.full(n, np.nan)

    # Build forward lookup: for each tick, find price 30s later
    j = 0
    for i in range(n):
        target_ts = ts[i] + FORWARD_WINDOW_NS
        while j < n and ts[j] < target_ts:
            j += 1
        if j < n:
            fwd_ret[i] = mid[j] - mid[i]
        j = max(j - 1, i + 1)  # reset j slightly for next i

    return fwd_ret


def run_diagnostic() -> dict:
    """Run Diagnostic 0a across all available data files."""
    results: dict[str, list] = {}

    for symbol_dir in SYMBOLS:
        sym_path = DATA_DIR / symbol_dir
        if not sym_path.exists():
            continue

        npy_files = sorted(sym_path.glob(f"{symbol_dir.upper()}_*_l1.npy"))
        # Skip aggregate files
        npy_files = [f for f in npy_files if "_all_" not in f.name and "_march_" not in f.name]

        for fpath in npy_files:
            date_str = fpath.stem.split("_")[1]
            print(f"Processing {symbol_dir.upper()} {date_str}...")

            data = np.load(str(fpath), allow_pickle=True)
            if len(data) < 3000:
                print(f"  Skipping (only {len(data)} rows)")
                continue

            features = compute_features(data)
            fwd_ret = compute_forward_return(data)
            # Use absolute forward return as adverse movement magnitude
            abs_fwd = np.abs(fwd_ret)

            for feat_name, feat_vals in features.items():
                # Mask: both feature and forward return must be valid
                valid = ~np.isnan(feat_vals) & ~np.isnan(abs_fwd)
                n_valid = valid.sum()
                if n_valid < 100:
                    continue

                # Spearman correlation: feature vs |forward price change|
                rho_abs, p_abs = stats.spearmanr(feat_vals[valid], abs_fwd[valid])
                # Also: feature vs signed forward return (directional)
                rho_signed, p_signed = stats.spearmanr(feat_vals[valid], fwd_ret[valid])

                key = f"{symbol_dir.upper()}_{feat_name}"
                if key not in results:
                    results[key] = []
                results[key].append({
                    "date": date_str,
                    "n": int(n_valid),
                    "rho_abs": float(rho_abs),
                    "p_abs": float(p_abs),
                    "rho_signed": float(rho_signed),
                    "p_signed": float(p_signed),
                })

    return results


def format_results(results: dict) -> str:
    """Format results as markdown report."""
    lines = [
        "# Diagnostic 0a: Feature vs Fill-Quality Correlation",
        "",
        f"**Date**: 2026-03-29",
        f"**Forward window**: 30 seconds",
        f"**Kill gate**: |pooled rho| > 0.05 for at least one feature",
        "",
        "## Methodology",
        "",
        "For each L1 tick, compute:",
        "- 4 candidate Direction C features from raw LOB data",
        "- Forward price change 30s ahead (adverse movement proxy)",
        "- Spearman correlation between each feature and |forward change| (magnitude)",
        "  and signed forward change (directional prediction)",
        "",
        "**Limitation**: No actual fill data (ClickHouse offline). Using all L1 ticks",
        "as proxy for fill events. Actual fills would be a subset with different",
        "microstructure characteristics. This diagnostic is a necessary-but-not-sufficient",
        "signal: if features don't correlate with forward movement at all ticks,",
        "they won't correlate at fill times either.",
        "",
        "## Per-Day Results",
        "",
    ]

    # Group by feature
    feature_groups: dict[str, list] = {}
    for key, day_results in sorted(results.items()):
        parts = key.split("_", 1)
        sym = parts[0]
        feat = parts[1] if len(parts) > 1 else key
        if feat not in feature_groups:
            feature_groups[feat] = []
        for dr in day_results:
            feature_groups[feat].append({**dr, "symbol": sym})

    for feat, days in sorted(feature_groups.items()):
        lines.append(f"### {feat}")
        lines.append("")
        lines.append("| Symbol | Date | N | rho(|fwd|) | p-value | rho(signed fwd) | p-value |")
        lines.append("|--------|------|---|------------|---------|-----------------|---------|")

        all_rho_abs = []
        all_rho_signed = []
        for d in days:
            lines.append(
                f"| {d['symbol']} | {d['date']} | {d['n']:,} | "
                f"{d['rho_abs']:+.4f} | {d['p_abs']:.2e} | "
                f"{d['rho_signed']:+.4f} | {d['p_signed']:.2e} |"
            )
            all_rho_abs.append(d["rho_abs"])
            all_rho_signed.append(d["rho_signed"])

        # Pooled summary
        mean_abs = np.mean(all_rho_abs)
        mean_signed = np.mean(all_rho_signed)
        lines.append("")
        lines.append(f"**Pooled mean rho(|fwd|)**: {mean_abs:+.4f}")
        lines.append(f"**Pooled mean rho(signed)**: {mean_signed:+.4f}")
        consistent_sign = all(r > 0 for r in all_rho_signed) or all(r < 0 for r in all_rho_signed)
        lines.append(f"**Sign consistency**: {'YES' if consistent_sign else 'NO'}")
        lines.append("")

    # Kill gate assessment
    lines.append("## Kill Gate Assessment")
    lines.append("")
    pass_features = []
    for feat, days in feature_groups.items():
        pooled_rho = np.mean([d["rho_abs"] for d in days])
        if abs(pooled_rho) > 0.05:
            pass_features.append((feat, pooled_rho))
        pooled_signed = np.mean([d["rho_signed"] for d in days])
        if abs(pooled_signed) > 0.05:
            pass_features.append((f"{feat}(signed)", pooled_signed))

    if pass_features:
        lines.append(f"**PASS** — {len(pass_features)} feature(s) exceed |rho| > 0.05:")
        for feat, rho in pass_features:
            lines.append(f"- {feat}: pooled rho = {rho:+.4f}")
    else:
        # Check if any feature has |rho| > 0.03 (marginal)
        marginal = []
        for feat, days in feature_groups.items():
            pooled = np.mean([d["rho_abs"] for d in days])
            if abs(pooled) > 0.03:
                marginal.append((feat, pooled))
        if marginal:
            lines.append(f"**MARGINAL** — No feature reaches |rho| > 0.05, but {len(marginal)} are > 0.03:")
            for feat, rho in marginal:
                lines.append(f"- {feat}: pooled rho = {rho:+.4f}")
            lines.append("")
            lines.append("Direction C may still be viable with feature combination, but standalone")
            lines.append("regime classification will require tighter engineering.")
        else:
            lines.append("**FAIL** — No feature shows |rho| > 0.05. Direction C is DEAD.")
            lines.append("Recommendation: Skip Direction C, proceed directly to Direction A.")

    lines.append("")
    return "\n".join(lines)


if __name__ == "__main__":
    os.chdir(Path(__file__).resolve().parents[4])  # project root
    results = run_diagnostic()
    report = format_results(results)
    print(report)

    out_path = Path("docs/alpha-research/r24/diagnostic_0a_fill_quality.md")
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(report)
    print(f"\nReport saved to {out_path}")
