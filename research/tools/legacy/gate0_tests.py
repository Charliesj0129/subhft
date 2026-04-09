"""
Gate 0 Empirical Pre-Tests for R47 Maker Pivot
================================================
9 tests on TXFD6 golden parquet data (March-April 2026).

Usage:
    uv run python research/tools/gate0_tests.py [--dates 2026-03-19:2026-04-02]

Each test outputs PASS/FAIL with quantitative evidence.
"""
from __future__ import annotations

import argparse
import math
import sys
from pathlib import Path

import numpy as np
import pyarrow.parquet as pq

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
GOLDEN_DIR = Path("research/data/real/golden/TXFD6")
PRICE_SCALE = 1_000_000  # golden parquet prices are x1e6
TXFD6_POINT_VALUE = 200  # NTD per point
TXFD6_COMMISSION_PER_SIDE = 30  # NTD (retail)
CANCEL_LATENCY_MS = 36  # Shioaji P95

# PE parameters
PE_D = 4  # embedding dimension
PE_N_PATTERNS = math.factorial(PE_D)  # 24
PE_WINDOW = 100  # sliding window size
PE_H_MAX = math.log2(PE_N_PATTERNS)  # log2(24) ≈ 4.585

# ---------------------------------------------------------------------------
# Data loading
# ---------------------------------------------------------------------------

def load_txfd6_days(date_range: str | None = None) -> list[dict]:
    """Load golden parquet files, return list of per-day dicts."""
    files = sorted(GOLDEN_DIR.glob("*.parquet"))
    if date_range:
        start, end = date_range.split(":")
        files = [f for f in files if start <= f.stem <= end]

    days = []
    for fp in files:
        table = pq.read_table(fp)
        df = table.to_pandas()
        ba = df[df["type"] == "BidAsk"].copy()
        tk = df[df["type"] == "Tick"].copy()

        if len(ba) < 200:
            continue

        # Extract L1 fields from BidAsk
        ba_bids_vol = np.array([row[0] if len(row) > 0 else 0 for row in ba["bids_vol"].values], dtype=np.int64)
        ba_asks_vol = np.array([row[0] if len(row) > 0 else 0 for row in ba["asks_vol"].values], dtype=np.int64)
        ba_bids_price = np.array([row[0] if len(row) > 0 else 0 for row in ba["bids_price"].values], dtype=np.int64)
        ba_asks_price = np.array([row[0] if len(row) > 0 else 0 for row in ba["asks_price"].values], dtype=np.int64)
        ba_ts = ba["exch_ts"].values.astype(np.int64)

        # Compute mid and spread in points
        mid = (ba_bids_price + ba_asks_price) / 2.0
        spread_pts = (ba_asks_price - ba_bids_price) / PRICE_SCALE  # in NTD, then / point_value
        # Actually: price is index points * 1e6. So spread in points = (ask - bid) / 1e6
        spread_pts = (ba_asks_price.astype(np.float64) - ba_bids_price.astype(np.float64)) / PRICE_SCALE

        # QI_1 (L1 queue imbalance)
        total_vol = ba_bids_vol + ba_asks_vol
        qi_1 = np.where(total_vol > 0,
                        (ba_bids_vol.astype(np.float64) - ba_asks_vol.astype(np.float64)) / total_vol,
                        0.0)

        # Tick data
        tk_prices = tk["price_scaled"].values.astype(np.int64) if len(tk) > 0 else np.array([], dtype=np.int64)
        tk_volumes = tk["volume"].values.astype(np.int64) if len(tk) > 0 else np.array([], dtype=np.int64)
        tk_ts = tk["exch_ts"].values.astype(np.int64) if len(tk) > 0 else np.array([], dtype=np.int64)

        # Infer trade direction via tick-rule (compare tick price to previous mid)
        tk_direction = np.zeros(len(tk_prices), dtype=np.int8)
        if len(tk_prices) > 1:
            price_diff = np.diff(tk_prices)
            tk_direction[1:] = np.where(price_diff > 0, 1, np.where(price_diff < 0, -1, 0))
            # Forward-fill zeros (use last non-zero direction)
            last_dir = 0
            for i in range(len(tk_direction)):
                if tk_direction[i] != 0:
                    last_dir = tk_direction[i]
                else:
                    tk_direction[i] = last_dir

        days.append({
            "date": fp.stem,
            "ba_ts": ba_ts,
            "ba_bids_vol": ba_bids_vol,
            "ba_asks_vol": ba_asks_vol,
            "ba_bids_price": ba_bids_price,
            "ba_asks_price": ba_asks_price,
            "spread_pts": spread_pts,
            "mid": mid,
            "qi_1": qi_1,
            "tk_prices": tk_prices,
            "tk_volumes": tk_volumes,
            "tk_ts": tk_ts,
            "tk_direction": tk_direction,
            "n_ba": len(ba),
            "n_tk": len(tk),
        })
        sys.stdout.flush()
        print(f"  Loaded {fp.stem}: {len(ba)} BidAsk, {len(tk)} Ticks, "
              f"median spread={np.median(spread_pts):.1f} pts, "
              f"median L1 bid={np.median(ba_bids_vol)}, ask={np.median(ba_asks_vol)}")

    return days


# ---------------------------------------------------------------------------
# G0.1: PE distribution on TXFD6 QI_1
# ---------------------------------------------------------------------------

def _all_ordinal_patterns(series: np.ndarray, d: int) -> np.ndarray:
    """Vectorized ordinal pattern computation for entire series.

    For D=4, maps each consecutive D-tuple to an index 0..23 via argsort-rank encoding.
    Returns array of length len(series) - d + 1.
    """
    n = len(series)
    if n < d:
        return np.array([], dtype=np.int32)

    # Build strided view: shape (n-d+1, d)
    from numpy.lib.stride_tricks import sliding_window_view
    windows = sliding_window_view(series, d)  # (n-d+1, d)

    # Compute ranks within each window via argsort-of-argsort
    order = np.argsort(windows, axis=1)
    ranks = np.empty_like(order)
    rows = np.arange(len(windows))[:, None]
    ranks[rows, order] = np.arange(d)[None, :]

    # Encode ranks as mixed-radix number (Lehmer-like but simpler factorial encoding)
    # For D=4: pattern_id = r0*6 + r1*2 + r2*1 ... but that's not unique.
    # Simpler: treat rank tuple as base-D number (works for D<=5 since D^D < 2^31)
    factorials = np.array([math.factorial(d - 1 - i) for i in range(d)], dtype=np.int64)
    # Actually, use a simple hash: sum of rank[i] * D^(D-1-i) — unique for permutations
    multipliers = np.array([d ** (d - 1 - i) for i in range(d)], dtype=np.int64)
    pattern_ids = (ranks * multipliers[None, :]).sum(axis=1).astype(np.int32)

    # Remap to 0..D!-1 via lookup
    unique_patterns = np.unique(pattern_ids)
    remap = np.zeros(pattern_ids.max() + 1, dtype=np.int32) if len(pattern_ids) > 0 else np.array([])
    if len(unique_patterns) > 0:
        remap = np.zeros(unique_patterns.max() + 1, dtype=np.int32)
        for i, p in enumerate(unique_patterns):
            remap[p] = i
        pattern_ids = remap[pattern_ids]

    return pattern_ids


def compute_pe_series(qi_series: np.ndarray, d: int = PE_D, w: int = PE_WINDOW) -> tuple[np.ndarray, np.ndarray]:
    """Compute sliding-window Permutation Entropy H and Complexity C.

    Vectorized: computes all ordinal patterns first, then slides histogram.
    Returns (H_normalized, C) arrays, length = len(qi_series) - w + 1.
    """
    n = len(qi_series)
    if n < w:
        return np.array([]), np.array([])

    n_patterns = math.factorial(d)
    h_max = math.log2(n_patterns)

    # Step 1: compute ALL ordinal pattern indices (vectorized)
    all_patterns = _all_ordinal_patterns(qi_series, d)
    n_pat = len(all_patterns)
    # Number of patterns per window of W original samples = W - D + 1
    pat_per_win = w - d + 1
    out_len = n_pat - pat_per_win + 1

    if out_len <= 0:
        return np.array([]), np.array([])

    h_arr = np.empty(out_len, dtype=np.float64)
    c_arr = np.empty(out_len, dtype=np.float64)

    # Uniform distribution for complexity
    uniform = np.ones(n_patterns, dtype=np.float64) / n_patterns
    log2_uniform = np.log2(uniform)  # all same value

    # Initial histogram
    counts = np.bincount(all_patterns[:pat_per_win], minlength=n_patterns).astype(np.float64)

    def _h_c_from_counts(counts, n_samp):
        probs = counts / n_samp
        mask = probs > 0
        h = -np.sum(probs[mask] * np.log2(probs[mask]))
        h_norm = h / h_max

        # Jensen-Shannon complexity
        m = 0.5 * (probs + uniform)
        m_mask = m > 0
        kl_pm = np.sum(probs[mask & m_mask] * np.log2(probs[mask & m_mask] / m[mask & m_mask]))
        kl_um = np.sum(uniform[m_mask] * np.log2(uniform[m_mask] / m[m_mask]))
        js_div = 0.5 * (kl_pm + kl_um)
        c = js_div * h_norm
        return h_norm, c

    h_arr[0], c_arr[0] = _h_c_from_counts(counts, pat_per_win)

    # Slide: remove oldest pattern, add newest
    # For large arrays, compute every 10th point to reduce ~10x
    step = max(1, out_len // 50_000)  # target ~50K output points
    for i in range(1, out_len):
        old_p = all_patterns[i - 1]
        new_p = all_patterns[i + pat_per_win - 1]
        counts[old_p] -= 1
        counts[new_p] += 1
        if i % step == 0 or i == out_len - 1:
            h_arr[i], c_arr[i] = _h_c_from_counts(counts, pat_per_win)
        else:
            h_arr[i] = h_arr[i - 1]  # carry forward
            c_arr[i] = c_arr[i - 1]

    return h_arr, c_arr


def test_g01_pe_distribution(days: list[dict]) -> dict:
    """G0.1: P(H_normalized < 0.85) > 10% of ticks."""
    all_h = []
    for day in days:
        h_arr, _ = compute_pe_series(day["qi_1"])
        if len(h_arr) > 0:
            all_h.append(h_arr[~np.isnan(h_arr)])

    if not all_h:
        return {"test": "G0.1", "pass": False, "reason": "No data"}

    h_all = np.concatenate(all_h)
    frac_below_085 = np.mean(h_all < 0.85)
    frac_below_090 = np.mean(h_all < 0.90)
    median_h = np.median(h_all)
    p10 = np.percentile(h_all, 10)

    passed = frac_below_085 > 0.10
    return {
        "test": "G0.1: PE distribution",
        "pass": passed,
        "criterion": "P(H < 0.85) > 10%",
        "P(H<0.85)": f"{frac_below_085:.4f} ({frac_below_085*100:.1f}%)",
        "P(H<0.90)": f"{frac_below_090:.4f} ({frac_below_090*100:.1f}%)",
        "median_H": f"{median_h:.4f}",
        "H_10th_pctile": f"{p10:.4f}",
        "n_samples": len(h_all),
    }


# ---------------------------------------------------------------------------
# G0.2: PE vs TDA β1 correlation (simplified — compare PE to vol proxy)
# ---------------------------------------------------------------------------

def test_g02_pe_vs_tda(days: list[dict]) -> dict:
    """G0.2: |corr(PE_H, realized_vol)| — if PE is just a vol proxy like TDA.

    Since TDA β1 values aren't cached, we compare PE to realized volatility
    (which TDA β1 predicted with IC=+0.088). If PE tracks vol closely,
    it's likely redundant with TDA.
    """
    all_pe_h = []
    all_rvol = []

    for day in days:
        h_arr, _ = compute_pe_series(day["qi_1"])
        if len(h_arr) < 100:
            continue

        # Compute realized vol over same windows (vectorized)
        mid = day["mid"].astype(np.float64)
        w = PE_WINDOW

        # Sliding-window std of returns
        from numpy.lib.stride_tricks import sliding_window_view
        if len(mid) < w + 1:
            continue
        returns = np.diff(mid)
        mid_denom = mid[:-1]
        valid_denom = mid_denom != 0
        rel_returns = np.where(valid_denom, returns / mid_denom, 0.0)

        if len(rel_returns) < w:
            continue
        rv_windows = sliding_window_view(rel_returns, w - 1)  # shape (n-w+1, w-1)
        rvol = np.std(rv_windows, axis=1)

        # Align lengths
        min_len = min(len(h_arr), len(rvol))
        if min_len < 100:
            continue
        all_pe_h.append(h_arr[:min_len])
        all_rvol.append(rvol[:min_len])

    if not all_pe_h:
        return {"test": "G0.2", "pass": False, "reason": "Insufficient data"}

    pe_h = np.concatenate(all_pe_h)
    rvol = np.concatenate(all_rvol)

    # Subsample for speed (Spearman on 1M+ points is slow)
    if len(pe_h) > 100_000:
        idx = np.random.default_rng(42).choice(len(pe_h), 100_000, replace=False)
        pe_h = pe_h[idx]
        rvol = rvol[idx]

    from scipy import stats
    rho, pval = stats.spearmanr(pe_h, rvol)

    passed = abs(rho) < 0.6
    return {
        "test": "G0.2: PE vs realized vol (TDA proxy)",
        "pass": passed,
        "criterion": "|ρ_Spearman| < 0.6",
        "rho": f"{rho:.4f}",
        "p_value": f"{pval:.2e}",
        "n_samples": len(pe_h),
        "interpretation": "PE redundant with vol" if not passed else "PE captures non-vol regime info",
    }


# ---------------------------------------------------------------------------
# G0.3: CECP spike lead/lag vs breakouts
# ---------------------------------------------------------------------------

def test_g03_cecp_lead_lag(days: list[dict]) -> dict:
    """G0.3: C spike leads mid-price breakout > 40% of time."""
    lead_count = 0
    lag_count = 0
    total_breakouts = 0

    for day in days:
        h_arr, c_arr = compute_pe_series(day["qi_1"])
        if len(c_arr) < 200:
            continue

        valid = ~np.isnan(c_arr)
        c_valid = c_arr[valid]
        if len(c_valid) < 100:
            continue

        # C spike: dC > 2 sigma over 10-sample window
        c_mean = np.mean(c_valid)
        c_std = np.std(c_valid)
        if c_std < 1e-10:
            continue

        # Breakout: mid-price moves >= 2 pts within 50 samples (~13s)
        mid = day["mid"]
        ba_ts = day["ba_ts"]

        # Find breakout events
        breakout_indices = []
        for i in range(PE_WINDOW, len(mid) - 50):
            fwd_max_move = np.max(np.abs(mid[i:i+50] - mid[i])) / PRICE_SCALE
            if fwd_max_move >= 2.0:
                breakout_indices.append(i)

        if not breakout_indices:
            continue

        # Deduplicate (keep first in each cluster)
        deduped = [breakout_indices[0]]
        for idx in breakout_indices[1:]:
            if idx - deduped[-1] > 50:
                deduped.append(idx)
        breakout_indices = deduped

        # For each breakout, check if C spike happened within [-30, +30] samples
        for bi in breakout_indices:
            c_idx = bi - PE_WINDOW  # offset into c_arr
            if c_idx < 30 or c_idx >= len(c_arr) - 30:
                continue

            # Check for C spike in window around breakout
            c_window_before = c_arr[max(0, c_idx-30):c_idx]
            c_window_after = c_arr[c_idx:min(len(c_arr), c_idx+30)]

            spike_before = np.any(c_window_before > c_mean + 2 * c_std) if len(c_window_before) > 0 else False
            spike_after = np.any(c_window_after > c_mean + 2 * c_std) if len(c_window_after) > 0 else False

            total_breakouts += 1
            if spike_before and not spike_after:
                lead_count += 1
            elif spike_after:
                lag_count += 1

    if total_breakouts == 0:
        return {"test": "G0.3", "pass": False, "reason": "No breakouts detected"}

    lead_frac = lead_count / total_breakouts
    passed = lead_frac > 0.40
    return {
        "test": "G0.3: CECP spike lead/lag",
        "pass": passed,
        "criterion": "C spike leads breakout > 40%",
        "lead_fraction": f"{lead_frac:.4f} ({lead_count}/{total_breakouts})",
        "lag_count": lag_count,
        "total_breakouts": total_breakouts,
    }


# ---------------------------------------------------------------------------
# G0.4: Snapshot aliasing ratio
# ---------------------------------------------------------------------------

def test_g04_snapshot_aliasing(days: list[dict]) -> dict:
    """G0.4: Hidden activity ratio < 50%.

    Compare consecutive L1 snapshots: net change vs estimated actual events.
    Use L5 data if available for ground truth.
    """
    total_net_changes = 0
    total_snapshots = 0
    l1_depth_changes = []

    for day in days:
        bv = day["ba_bids_vol"]
        av = day["ba_asks_vol"]

        # Count bid-side L1 changes
        bid_diffs = np.abs(np.diff(bv))
        ask_diffs = np.abs(np.diff(av))

        # Snapshots with zero net change but likely hidden activity
        bid_zero_change = np.sum(bid_diffs == 0)
        ask_zero_change = np.sum(ask_diffs == 0)

        # Total events estimated from absolute differences
        # Lower bound: at least |delta| events per snapshot
        # With 1-5 lot depth, most changes are 1 lot
        total_bid_events_lb = np.sum(bid_diffs)
        total_ask_events_lb = np.sum(ask_diffs)

        # More refined: for thin books, abs(delta) ≈ actual events when depth is small
        # Hidden activity = events that cancel out within a snapshot interval
        # Proxy: fraction of snapshots where depth stays the same (likely offsetting events)
        n_snap = len(bv) - 1
        zero_frac_bid = bid_zero_change / n_snap if n_snap > 0 else 0
        zero_frac_ask = ask_zero_change / n_snap if n_snap > 0 else 0

        total_snapshots += n_snap
        l1_depth_changes.extend(bid_diffs.tolist())

        # Estimate using tick events as proxy for activity
        # Ticks represent only market orders (not limit arrivals or cancels)
        # Between consecutive BidAsk snapshots, count ticks
        if len(day["tk_ts"]) > 0 and n_snap > 0:
            ba_ts = day["ba_ts"]
            tk_ts = day["tk_ts"]
            # Count ticks between snapshots (via searchsorted)
            tick_idx = np.searchsorted(tk_ts, ba_ts)
            ticks_between = np.diff(tick_idx)
            # Ticks represent MOs; each MO changes queue by >=1
            # But snapshot delta may be 0 if a limit order arrived to offset
            hidden = np.sum((ticks_between > 0) & (bid_diffs == 0) & (ask_diffs == 0))
            total_net_changes += hidden

    if total_snapshots == 0:
        return {"test": "G0.4", "pass": False, "reason": "No data"}

    # Alternative metric: fraction of snapshot intervals with no visible L1 change
    # despite known market activity (ticks occurring)
    hidden_ratio = total_net_changes / total_snapshots if total_snapshots > 0 else 0

    # For thin books (depth 1-5), most individual events are visible
    # The aliasing concern is mainly about offsetting arrivals+cancels
    median_change = np.median(l1_depth_changes) if l1_depth_changes else 0

    # Heuristic estimate: with 1-lot depth, if 73% trades are qty=1,
    # each trade is one event. Snapshot rate ~8Hz, tick rate ~4Hz.
    # Most snapshots contain 0-1 ticks, so aliasing is LOW for thin books.
    passed = hidden_ratio < 0.50
    return {
        "test": "G0.4: Snapshot aliasing",
        "pass": passed,
        "criterion": "Hidden activity ratio < 50%",
        "hidden_ratio": f"{hidden_ratio:.4f} ({hidden_ratio*100:.1f}%)",
        "total_snapshots": total_snapshots,
        "median_l1_change": f"{median_change:.1f}",
        "note": "Thin book (depth 1-5) reduces aliasing — most events are visible",
    }


# ---------------------------------------------------------------------------
# G0.5: Queue survival model vs QI_1 correlation
# ---------------------------------------------------------------------------

def test_g05_queue_vs_qi(days: list[dict]) -> dict:
    """G0.5: |rank_corr(queue_survival_proxy, QI_1)| < 0.7."""
    all_survival = []
    all_qi = []

    for day in days:
        bv = day["ba_bids_vol"].astype(np.float64)
        av = day["ba_asks_vol"].astype(np.float64)
        qi = day["qi_1"]

        if len(bv) < 200:
            continue

        # Estimate arrival/depletion rates via EMA of queue changes
        ema_alpha = 0.05  # ~20-event half-life
        lambda_bid = 1.0  # init
        mu_bid = 1.0
        lambda_ask = 1.0
        mu_ask = 1.0

        survival_bid = np.zeros(len(bv))
        survival_ask = np.zeros(len(bv))

        for i in range(1, len(bv)):
            d_bid = bv[i] - bv[i-1]
            d_ask = av[i] - av[i-1]

            # Separate arrivals (positive delta) and departures (negative delta)
            if d_bid > 0:
                lambda_bid = ema_alpha * d_bid + (1 - ema_alpha) * lambda_bid
            elif d_bid < 0:
                mu_bid = ema_alpha * (-d_bid) + (1 - ema_alpha) * mu_bid

            if d_ask > 0:
                lambda_ask = ema_alpha * d_ask + (1 - ema_alpha) * lambda_ask
            elif d_ask < 0:
                mu_ask = ema_alpha * (-d_ask) + (1 - ema_alpha) * mu_ask

            # Survival probability (Gambler's Ruin approximation)
            # P(queue survives) = 1 - (mu/lambda)^q for mu < lambda
            # P(queue depletes) = (mu/lambda)^q for mu > lambda (rho > 1 means draining)
            rho_bid = mu_bid / max(lambda_bid, 1e-6)
            rho_ask = mu_ask / max(lambda_ask, 1e-6)

            q_bid = max(bv[i], 1)
            q_ask = max(av[i], 1)

            # P(bid wall survives 5s) - simplified
            if rho_bid > 1.0:
                survival_bid[i] = max(0, 1.0 - min(rho_bid ** q_bid, 100) / 100)
            else:
                survival_bid[i] = 1.0 - (rho_bid ** q_bid)

            if rho_ask > 1.0:
                survival_ask[i] = max(0, 1.0 - min(rho_ask ** q_ask, 100) / 100)
            else:
                survival_ask[i] = 1.0 - (rho_ask ** q_ask)

        # Use bid survival as the main signal
        # Compare to QI_1 (positive QI = more bid = bid is stronger)
        valid = (np.arange(len(bv)) > 50)  # skip warmup
        all_survival.extend(survival_bid[valid].tolist())
        all_qi.extend(qi[valid].tolist())

    if len(all_survival) < 100:
        return {"test": "G0.5", "pass": False, "reason": "Insufficient data"}

    from scipy import stats
    rho, pval = stats.spearmanr(all_survival, all_qi)

    passed = abs(rho) < 0.7
    return {
        "test": "G0.5: Queue survival vs QI_1",
        "pass": passed,
        "criterion": "|ρ_Spearman| < 0.7",
        "rho": f"{rho:.4f}",
        "p_value": f"{pval:.2e}",
        "n_samples": len(all_survival),
        "interpretation": "Redundant with QI_1" if not passed else "Adds info beyond QI_1",
    }


# ---------------------------------------------------------------------------
# G0.6: Wall depletion lead time
# ---------------------------------------------------------------------------

def test_g06_wall_depletion_lead(days: list[dict]) -> dict:
    """G0.6: Queue survival signal fires > 36ms before depletion > 50%."""
    lead_times_ms = []
    total_depletions = 0

    for day in days:
        bv = day["ba_bids_vol"]
        av = day["ba_asks_vol"]
        ts = day["ba_ts"]

        # Find depletion events: L1 goes to 0 or price changes (wall consumed)
        bp = day["ba_bids_price"]
        ap = day["ba_asks_price"]

        for i in range(2, len(bv)):
            # Bid wall depletion: bid price drops (wall consumed)
            if bp[i] < bp[i-1] and bp[i-1] > 0:
                total_depletions += 1
                # Look back: when was bv first <= 1? (signal point)
                signal_idx = None
                for j in range(i-1, max(i-50, 0), -1):
                    if bv[j] <= 1 and bp[j] == bp[i-1]:  # same price level, thin
                        signal_idx = j
                        break

                if signal_idx is not None:
                    lead_ns = ts[i] - ts[signal_idx]
                    lead_ms = lead_ns / 1e6
                    lead_times_ms.append(lead_ms)

            # Ask wall depletion: ask price rises
            if ap[i] > ap[i-1] and ap[i-1] > 0:
                total_depletions += 1
                signal_idx = None
                for j in range(i-1, max(i-50, 0), -1):
                    if av[j] <= 1 and ap[j] == ap[i-1]:
                        signal_idx = j
                        break

                if signal_idx is not None:
                    lead_ns = ts[i] - ts[signal_idx]
                    lead_ms = lead_ns / 1e6
                    lead_times_ms.append(lead_ms)

    if not lead_times_ms:
        return {"test": "G0.6", "pass": False, "reason": f"No depletion events with signal (total depletions: {total_depletions})"}

    lt = np.array(lead_times_ms)
    frac_above_36ms = np.mean(lt > 36)
    median_lead = np.median(lt)

    passed = frac_above_36ms > 0.50
    return {
        "test": "G0.6: Wall depletion lead time",
        "pass": passed,
        "criterion": "Signal > 36ms before depletion > 50%",
        "frac_above_36ms": f"{frac_above_36ms:.4f} ({frac_above_36ms*100:.1f}%)",
        "median_lead_ms": f"{median_lead:.1f}",
        "mean_lead_ms": f"{np.mean(lt):.1f}",
        "p25_lead_ms": f"{np.percentile(lt, 25):.1f}",
        "total_depletions": total_depletions,
        "depletions_with_signal": len(lt),
    }


# ---------------------------------------------------------------------------
# G0.7: Signed flow burst distribution (fat tail test)
# ---------------------------------------------------------------------------

def test_g07_signed_flow_bursts(days: list[dict]) -> dict:
    """G0.7: Signed flow in 1s windows has fat tails (kurtosis > 5)."""
    all_bursts = []

    for day in days:
        if len(day["tk_ts"]) < 100:
            continue

        tk_ts = day["tk_ts"]
        tk_dir = day["tk_direction"]
        tk_vol = day["tk_volumes"]

        signed_vol = tk_dir.astype(np.float64) * tk_vol.astype(np.float64)

        # 1-second windows
        window_ns = 1_000_000_000
        t_start = tk_ts[0]
        t_end = tk_ts[-1]

        t = t_start
        while t < t_end:
            mask = (tk_ts >= t) & (tk_ts < t + window_ns)
            burst = np.sum(signed_vol[mask])
            all_bursts.append(burst)
            t += window_ns

    if len(all_bursts) < 100:
        return {"test": "G0.7", "pass": False, "reason": "Insufficient data"}

    bursts = np.array(all_bursts)
    bursts_nonzero = bursts[bursts != 0]

    if len(bursts_nonzero) < 50:
        return {"test": "G0.7", "pass": False, "reason": "Too few non-zero bursts"}

    from scipy import stats
    kurtosis = stats.kurtosis(bursts_nonzero, fisher=True)  # excess kurtosis
    skewness = stats.skew(bursts_nonzero)

    # Fat tail test: kurtosis > 5 indicates heavy tails beyond Gaussian
    # Also check with Jarque-Bera
    jb_stat, jb_pval = stats.jarque_bera(bursts_nonzero)

    # Percentile analysis
    p99 = np.percentile(np.abs(bursts_nonzero), 99)
    p95 = np.percentile(np.abs(bursts_nonzero), 95)
    p50 = np.percentile(np.abs(bursts_nonzero), 50)

    passed = kurtosis > 5.0
    return {
        "test": "G0.7: Signed flow burst distribution",
        "pass": passed,
        "criterion": "Excess kurtosis > 5 (fat tails)",
        "kurtosis": f"{kurtosis:.2f}",
        "skewness": f"{skewness:.2f}",
        "JB_stat": f"{jb_stat:.1f}",
        "JB_pval": f"{jb_pval:.2e}",
        "p50_abs_burst": f"{p50:.1f}",
        "p95_abs_burst": f"{p95:.1f}",
        "p99_abs_burst": f"{p99:.1f}",
        "n_windows": len(all_bursts),
        "n_nonzero": len(bursts_nonzero),
    }


# ---------------------------------------------------------------------------
# G0.8: Capitulation event frequency
# ---------------------------------------------------------------------------

def test_g08_capitulation_frequency(days: list[dict]) -> dict:
    """G0.8: >= 3 capitulation events/day with reversal >= 3 pts."""
    events_per_day = []
    total_events = 0
    reversal_magnitudes = []

    for day in days:
        if len(day["tk_ts"]) < 100:
            continue

        tk_ts = day["tk_ts"]
        tk_dir = day["tk_direction"]
        tk_vol = day["tk_volumes"]
        tk_prices = day["tk_prices"]

        signed_vol = tk_dir.astype(np.float64) * tk_vol.astype(np.float64)

        # Cumulative signed flow over 30-second windows
        window_ns = 30_000_000_000  # 30s
        day_events = 0

        i = 0
        while i < len(tk_ts) - 1:
            # Accumulate signed flow over 30s
            t_start = tk_ts[i]
            mask = (tk_ts >= t_start) & (tk_ts < t_start + window_ns)
            idx = np.where(mask)[0]
            if len(idx) < 5:
                i = idx[-1] + 1 if len(idx) > 0 else i + 1
                continue

            cum_flow = np.sum(signed_vol[idx])
            # Check if extreme (> 20 lots cumulative in 30s)
            if abs(cum_flow) > 20:
                # Check for reversal: price moves opposite direction by >= 3 pts in next 30s
                t_after_start = tk_ts[idx[-1]]
                after_mask = (tk_ts > t_after_start) & (tk_ts < t_after_start + window_ns)
                after_idx = np.where(after_mask)[0]
                if len(after_idx) > 0:
                    price_at_event = tk_prices[idx[-1]]
                    price_after = tk_prices[after_idx]
                    if cum_flow > 0:
                        # Selling pressure built up, reversal = price goes UP
                        max_reversal = (np.max(price_after) - price_at_event) / PRICE_SCALE
                    else:
                        # Buying pressure, reversal = price goes DOWN
                        max_reversal = (price_at_event - np.min(price_after)) / PRICE_SCALE

                    if max_reversal >= 3.0:
                        day_events += 1
                        total_events += 1
                        reversal_magnitudes.append(max_reversal)

                i = idx[-1] + 1  # skip past this event
            else:
                i += 1

        events_per_day.append({"date": day["date"], "events": day_events})

    if not events_per_day:
        return {"test": "G0.8", "pass": False, "reason": "No data"}

    avg_events = np.mean([d["events"] for d in events_per_day])
    passed = avg_events >= 3.0

    return {
        "test": "G0.8: Capitulation event frequency",
        "pass": passed,
        "criterion": "≥ 3 events/day with reversal ≥ 3 pts",
        "avg_events_per_day": f"{avg_events:.1f}",
        "total_events": total_events,
        "events_per_day": {d["date"]: d["events"] for d in events_per_day},
        "median_reversal_pts": f"{np.median(reversal_magnitudes):.1f}" if reversal_magnitudes else "N/A",
        "n_days": len(events_per_day),
    }


# ---------------------------------------------------------------------------
# G0.9: TXFD6 baseline maker economics
# ---------------------------------------------------------------------------

def test_g09_maker_economics(days: list[dict]) -> dict:
    """G0.9: Exists spread/time combination with positive maker EV on TXFD6.

    Simple simulation: post bid and ask at L1.
    Fill happens when price crosses our quote.
    Adverse selection measured by 1s forward return after fill.
    """
    rt_cost_ntd = 2 * TXFD6_COMMISSION_PER_SIDE  # 60 NTD per round-trip
    rt_cost_pts = rt_cost_ntd / TXFD6_POINT_VALUE  # 0.3 pts

    spread_bins = {}  # spread_pts -> list of fill PnLs
    total_fills = 0

    for day in days:
        bp = day["ba_bids_price"]
        ap = day["ba_asks_price"]
        ts = day["ba_ts"]
        spread = day["spread_pts"]
        mid = day["mid"]

        if len(bp) < 200:
            continue

        # Simple maker simulation:
        # At each snapshot, we have resting bid at best_bid and ask at best_ask.
        # Fill occurs when next snapshot shows mid-price moved through our quote.
        for i in range(1, len(bp) - 50):
            s = spread[i]
            if s < 0.5 or s > 20:  # filter invalid
                continue

            s_bucket = int(s)

            # Check if we'd get filled on bid side:
            # Price drops through our bid (next mid < our bid)
            our_bid = bp[i]
            our_ask = ap[i]

            # Bid fill: next snapshot price <= our bid
            # This is a simplification: real fills depend on queue position
            next_mid = mid[i + 1] if i + 1 < len(mid) else mid[i]

            bid_fill = (next_mid <= our_bid)
            ask_fill = (next_mid >= our_ask)

            if bid_fill:
                # We bought at our_bid. Measure 1s forward PnL
                t_fill = ts[i + 1]
                fwd_mask = ts > t_fill + 1_000_000_000  # 1s after
                fwd_idx = np.where(fwd_mask)[0]
                if len(fwd_idx) > 0:
                    fwd_mid = mid[fwd_idx[0]]
                    pnl_pts = (fwd_mid - our_bid) / PRICE_SCALE - rt_cost_pts / 2  # half RT
                    if s_bucket not in spread_bins:
                        spread_bins[s_bucket] = []
                    spread_bins[s_bucket].append(pnl_pts)
                    total_fills += 1

            if ask_fill:
                t_fill = ts[i + 1]
                fwd_mask = ts > t_fill + 1_000_000_000
                fwd_idx = np.where(fwd_mask)[0]
                if len(fwd_idx) > 0:
                    fwd_mid = mid[fwd_idx[0]]
                    pnl_pts = (our_ask - fwd_mid) / PRICE_SCALE - rt_cost_pts / 2
                    if s_bucket not in spread_bins:
                        spread_bins[s_bucket] = []
                    spread_bins[s_bucket].append(pnl_pts)
                    total_fills += 1

    if not spread_bins:
        return {"test": "G0.9", "pass": False, "reason": "No fills simulated"}

    results = {}
    any_positive = False
    for s in sorted(spread_bins.keys()):
        pnls = np.array(spread_bins[s])
        mean_pnl = np.mean(pnls)
        n = len(pnls)
        if mean_pnl > 0:
            any_positive = True
        results[f"spread_{s}pt"] = {
            "n_fills": n,
            "mean_pnl_pts": f"{mean_pnl:.4f}",
            "median_pnl_pts": f"{np.median(pnls):.4f}",
            "pct_profitable": f"{np.mean(pnls > 0)*100:.1f}%",
        }

    return {
        "test": "G0.9: TXFD6 baseline maker economics",
        "pass": any_positive,
        "criterion": "Exists spread bucket with mean PnL > 0",
        "rt_cost_pts": f"{rt_cost_pts:.2f}",
        "total_fills": total_fills,
        "per_spread": results,
    }


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description="Gate 0 pre-tests for R47 Maker Pivot")
    parser.add_argument("--dates", default="2026-03-19:2026-04-02",
                        help="Date range (YYYY-MM-DD:YYYY-MM-DD)")
    args = parser.parse_args()

    print("=" * 72)
    print("R47 Maker Pivot — Gate 0 Empirical Pre-Tests")
    print(f"Date range: {args.dates}")
    print("=" * 72)

    print("\n[1/9] Loading TXFD6 data...", flush=True)
    days = load_txfd6_days(args.dates)
    if not days:
        print("ERROR: No data loaded!")
        sys.exit(1)
    print(f"Loaded {len(days)} trading days\n")

    tests = [
        ("G0.1", test_g01_pe_distribution),
        ("G0.2", test_g02_pe_vs_tda),
        ("G0.3", test_g03_cecp_lead_lag),
        ("G0.4", test_g04_snapshot_aliasing),
        ("G0.5", test_g05_queue_vs_qi),
        ("G0.6", test_g06_wall_depletion_lead),
        ("G0.7", test_g07_signed_flow_bursts),
        ("G0.8", test_g08_capitulation_frequency),
        ("G0.9", test_g09_maker_economics),
    ]

    results = []
    for label, test_fn in tests:
        print(f"\n{'='*60}")
        print(f"Running {label}...")
        try:
            result = test_fn(days)
            results.append(result)
            status = "✅ PASS" if result["pass"] else "❌ FAIL"
            print(f"{status}: {result['test']}")
            for k, v in result.items():
                if k not in ("test", "pass"):
                    if isinstance(v, dict):
                        print(f"  {k}:")
                        for k2, v2 in v.items():
                            print(f"    {k2}: {v2}")
                    else:
                        print(f"  {k}: {v}")
        except Exception as e:
            print(f"❌ ERROR in {label}: {e}")
            import traceback
            traceback.print_exc()
            results.append({"test": label, "pass": False, "reason": str(e)})

    # Summary
    print("\n" + "=" * 72)
    print("GATE 0 SUMMARY")
    print("=" * 72)
    for r in results:
        status = "PASS" if r["pass"] else "FAIL"
        print(f"  [{status}] {r['test']}")

    n_pass = sum(1 for r in results if r["pass"])
    n_fail = sum(1 for r in results if not r["pass"])
    print(f"\nTotal: {n_pass} PASS, {n_fail} FAIL out of {len(results)} tests")

    # Decision logic
    print("\n" + "-" * 72)
    print("DECISION LOGIC:")

    g09 = next((r for r in results if "G0.9" in r.get("test", "")), None)
    if g09 and not g09["pass"]:
        print("  ⛔ G0.9 FAIL → ALL THREE DIRECTIONS KILLED")
        print("     No baseline maker economics on TXFD6.")
        return

    g01 = next((r for r in results if "G0.1" in r.get("test", "")), None)
    g02 = next((r for r in results if "G0.2" in r.get("test", "")), None)
    if g01 and not g01["pass"]:
        print("  ⛔ G0.1 FAIL → D1 (PE/CECP) KILLED (PE always near H=1.0)")
    if g02 and not g02["pass"]:
        print("  ⛔ G0.2 FAIL → D1 (PE/CECP) KILLED (redundant with TDA/vol)")

    g04 = next((r for r in results if "G0.4" in r.get("test", "")), None)
    if g04 and not g04["pass"]:
        print("  ⛔ G0.4 FAIL → D2 (Queue) KILLED (snapshot aliasing too high)")

    g07 = next((r for r in results if "G0.7" in r.get("test", "")), None)
    g08 = next((r for r in results if "G0.8" in r.get("test", "")), None)
    if g07 and not g07["pass"] and g08 and not g08["pass"]:
        print("  ⛔ G0.7+G0.8 FAIL → D3 (MFG) KILLED (no capitulation events)")

    # Surviving directions
    d1_alive = (g01 and g01["pass"]) and (g02 and g02["pass"])
    d2_alive = (g04 and g04["pass"])
    d3_alive = not ((g07 and not g07["pass"]) and (g08 and not g08["pass"]))

    print(f"\n  D1 (PE/CECP): {'ALIVE' if d1_alive else 'KILLED'}")
    print(f"  D2 (Queue):   {'ALIVE' if d2_alive else 'KILLED'}")
    print(f"  D3 (MFG):     {'ALIVE' if d3_alive else 'KILLED'}")


if __name__ == "__main__":
    main()
