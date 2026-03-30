"""
Direction E: Multi-Factor Combination IC Test

Combines 2330 lead signal with TMFD6 LOB features.

Factors:
1. 2330_ret_300s — TSMC 5-min return
2. tmf_depth_imbalance — (bid_qty - ask_qty) / (bid_qty + ask_qty)
3. tmf_spread_bps — TMFD6 spread
4. tmf_self_ret_300s — TMFD6 own 5-min return (control)

Tests: single IC, equal-weight z-score, ridge regression, sign-agreement.
Kill gate: combined IC > max(individual) + 0.01 to justify complexity.
"""

import numpy as np
import pandas as pd
from pathlib import Path
from scipy import stats
from sklearn.linear_model import Ridge
from sklearn.preprocessing import StandardScaler

BASE = Path(__file__).resolve().parent.parent.parent.parent.parent
DATA_DIR = BASE / "research" / "data" / "processed" / "tsmc_leadlag"

LOOKBACK = 300
HORIZONS = [120, 300]
STEP = 300  # non-overlapping


def load_all_days() -> dict:
    days = {}
    for f in sorted(DATA_DIR.glob("aligned_*.npz")):
        date_str = f.stem.replace("aligned_", "")
        data = np.load(f)
        stock = pd.DataFrame(data["stock"])
        futures = pd.DataFrame(data["futures"])
        stock["ts"] = pd.to_datetime(stock["local_ts"], unit="ns")
        futures["ts"] = pd.to_datetime(futures["local_ts"], unit="ns")
        stock = stock.set_index("ts").sort_index()
        futures = futures.set_index("ts").sort_index()
        s1 = stock.resample("1s").last().dropna(subset=["mid_price"])
        f1 = futures.resample("1s").last().dropna(subset=["mid_price"])
        common = s1.index.intersection(f1.index)
        if len(common) < 1000:
            continue
        df = pd.DataFrame(index=common)
        df["mid_stock"] = s1.loc[common, "mid_price"].values
        df["mid_fut"] = f1.loc[common, "mid_price"].values
        df["bid_qty_fut"] = f1.loc[common, "bid_qty"].values
        df["ask_qty_fut"] = f1.loc[common, "ask_qty"].values
        # Compute spread_bps from bid/ask if available in stock data
        df["bid_qty_stock"] = s1.loc[common, "bid_qty"].values
        df["ask_qty_stock"] = s1.loc[common, "ask_qty"].values
        days[date_str] = df
    return days


def log_ret(prices, shift):
    ret = np.full(len(prices), np.nan)
    if shift > 0 and shift < len(prices):
        valid = (prices[shift:] > 0) & (prices[:-shift] > 0)
        ret[shift:] = np.where(valid, np.log(prices[shift:] / prices[:-shift]), np.nan)
    return ret


def fwd_ret(prices, horizon):
    ret = np.full(len(prices), np.nan)
    n = len(prices)
    if horizon < n:
        valid = (prices[horizon:] > 0) & (prices[:n - horizon] > 0)
        ret[:n - horizon] = np.where(valid, np.log(prices[horizon:] / prices[:n - horizon]), np.nan)
    return ret


def spearman_ic(s, r):
    valid = np.isfinite(s) & np.isfinite(r)
    if valid.sum() < 20:
        return np.nan, np.nan, 0
    ic, pval = stats.spearmanr(s[valid], r[valid])
    return ic, pval, int(valid.sum())


def run():
    print("Loading data...")
    days = load_all_days()
    print(f"Loaded {len(days)} days\n")

    for horizon in HORIZONS:
        print(f"\n{'=' * 80}")
        print(f"HORIZON = {horizon}s (non-overlapping step = {STEP}s)")
        print(f"{'=' * 80}")

        all_factors = []
        all_fwd = []
        day_results = []

        for date_str, df in sorted(days.items()):
            mid_s = df["mid_stock"].values
            mid_f = df["mid_fut"].values
            bid_q = df["bid_qty_fut"].values
            ask_q = df["ask_qty_fut"].values

            # Compute factors
            f1 = log_ret(mid_s, LOOKBACK)        # 2330_ret_300s
            f2_raw = (bid_q - ask_q)
            f2_denom = (bid_q + ask_q)
            f2 = np.where(f2_denom > 0, f2_raw / f2_denom, np.nan)  # tmf_depth_imbalance
            # tmf_spread_bps: need bid/ask prices, but we only have mid. Use bid/ask qty ratio as proxy
            # Actually we don't have spread in the exported data. Use imbalance as the LOB signal.
            f3 = log_ret(mid_f, LOOKBACK)         # tmf_self_ret_300s

            y = fwd_ret(mid_f, horizon)

            # Non-overlapping sampling
            idx = np.arange(0, len(mid_f), STEP)
            f1_s = f1[idx]
            f2_s = f2[idx]
            f3_s = f3[idx]
            y_s = y[idx]

            valid = np.isfinite(f1_s) & np.isfinite(f2_s) & np.isfinite(f3_s) & np.isfinite(y_s)
            if valid.sum() < 10:
                continue

            X = np.column_stack([f1_s[valid], f2_s[valid], f3_s[valid]])
            Y = y_s[valid]

            all_factors.append(X)
            all_fwd.append(Y)

            # Per-day single-factor ICs
            ic1, _, _ = spearman_ic(f1_s, y_s)
            ic2, _, _ = spearman_ic(f2_s, y_s)
            ic3, _, _ = spearman_ic(f3_s, y_s)

            day_results.append({
                "date": date_str,
                "ic_2330": ic1,
                "ic_imb": ic2,
                "ic_self": ic3,
                "n": int(valid.sum()),
            })

        if not all_factors:
            print("  No valid data")
            continue

        # Pooled analysis
        X_all = np.vstack(all_factors)
        Y_all = np.concatenate(all_fwd)
        n_total = len(Y_all)

        print(f"\nTotal non-overlapping observations: {n_total}")

        # ---- Single-factor ICs ----
        print(f"\n--- Single-Factor ICs ---")
        factor_names = ["2330_ret_300s", "tmf_imbalance", "tmf_self_300s"]
        single_ics = {}
        for i, name in enumerate(factor_names):
            ic, pval, n = spearman_ic(X_all[:, i], Y_all)
            single_ics[name] = ic
            print(f"  {name:>20}: IC={ic:+.4f} (p={pval:.4f}, n={n})")

        # ---- Equal-weight z-score combination ----
        print(f"\n--- Equal-Weight Z-Score Combination ---")
        scaler = StandardScaler()
        X_z = scaler.fit_transform(X_all)
        combo_eq = X_z.mean(axis=1)  # equal weight
        ic_eq, pval_eq, n_eq = spearman_ic(combo_eq, Y_all)
        print(f"  Equal-weight combo: IC={ic_eq:+.4f} (p={pval_eq:.4f}, n={n_eq})")

        # Without self (only 2330 + imbalance)
        combo_no_self = X_z[:, :2].mean(axis=1)
        ic_ns, pval_ns, _ = spearman_ic(combo_no_self, Y_all)
        print(f"  2330 + imb only:    IC={ic_ns:+.4f} (p={pval_ns:.4f})")

        # ---- Ridge regression (in-sample) ----
        print(f"\n--- Ridge Regression (in-sample, alpha=1.0) ---")
        ridge = Ridge(alpha=1.0)
        ridge.fit(X_z, Y_all)
        Y_pred = ridge.predict(X_z)
        ic_ridge, pval_ridge, _ = spearman_ic(Y_pred, Y_all)
        r2 = ridge.score(X_z, Y_all)

        # Adjusted R²
        p = X_z.shape[1]
        adj_r2 = 1 - (1 - r2) * (n_total - 1) / (n_total - p - 1)

        print(f"  Ridge combo:   IC={ic_ridge:+.4f} (p={pval_ridge:.4f})")
        print(f"  R²:            {r2:.6f}")
        print(f"  Adjusted R²:   {adj_r2:.6f}")
        print(f"  Coefficients:  {dict(zip(factor_names, ridge.coef_))}")

        # ---- Leave-one-day-out cross-validation ----
        print(f"\n--- Leave-One-Day-Out CV Ridge ---")
        loo_ics = []
        day_starts = np.cumsum([0] + [len(f) for f in all_factors])
        for d in range(len(all_factors)):
            train_X = np.vstack([all_factors[j] for j in range(len(all_factors)) if j != d])
            train_Y = np.concatenate([all_fwd[j] for j in range(len(all_fwd)) if j != d])
            test_X = all_factors[d]
            test_Y = all_fwd[d]

            sc = StandardScaler()
            train_Xz = sc.fit_transform(train_X)
            test_Xz = sc.transform(test_X)

            r = Ridge(alpha=1.0)
            r.fit(train_Xz, train_Y)
            pred = r.predict(test_Xz)
            ic_loo, _, _ = spearman_ic(pred, test_Y)
            loo_ics.append(ic_loo)

        valid_loo = [x for x in loo_ics if not np.isnan(x)]
        mean_loo = np.mean(valid_loo) if valid_loo else np.nan
        print(f"  LOO-CV mean IC: {mean_loo:+.4f} (n={len(valid_loo)} days)")

        # ---- Factor sign-agreement analysis ----
        print(f"\n--- Sign-Agreement Analysis ---")
        sign_2330 = np.sign(X_all[:, 0])
        sign_imb = np.sign(X_all[:, 1])
        agree = sign_2330 == sign_imb
        disagree = ~agree

        ic_agree, _, n_agree = spearman_ic(X_all[agree, 0], Y_all[agree])
        ic_disagree, _, n_disagree = spearman_ic(X_all[disagree, 0], Y_all[disagree])
        print(f"  2330+imb agree (n={n_agree}):    2330 IC={ic_agree:+.4f}")
        print(f"  2330+imb disagree (n={n_disagree}): 2330 IC={ic_disagree:+.4f}")

        # ---- Kill gate ----
        print(f"\n--- KILL GATE ---")
        max_single = max(abs(v) for v in single_ics.values())
        best_combo = max(abs(ic_eq), abs(ic_ridge), abs(mean_loo))
        threshold = max_single + 0.01
        print(f"  Max single |IC|:  {max_single:.4f}")
        print(f"  Best combo |IC|:  {best_combo:.4f}")
        print(f"  Threshold:        {threshold:.4f}")
        if best_combo > threshold:
            print(f"  PASS: combo > single + 0.01")
        else:
            print(f"  FAIL: combo does not beat single + 0.01")

        # ---- Per-day detail ----
        print(f"\n--- Per-Day ICs ---")
        print(f"{'Date':>12} {'2330':>7} {'Imb':>7} {'Self':>7} {'N':>5}")
        for r in day_results:
            print(
                f"{r['date']:>12} {r['ic_2330']:>+7.3f} {r['ic_imb']:>+7.3f} "
                f"{r['ic_self']:>+7.3f} {r['n']:>5}"
            )


if __name__ == "__main__":
    run()
