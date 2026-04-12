"""R47 Maker Pivot — Stage 4 Backtest (ClickHouse data source).

Compares naive maker vs signal-gated maker on TXFD6.
Signals: D1 (PE entropy), D2 (Queue survival), D3 (MFG inventory).

Usage:
    uv run python research/backtest/r47_maker_backtest.py
"""
from __future__ import annotations

import math
import os
import sys
from dataclasses import dataclass, field

import clickhouse_connect
import numpy as np

# ── ClickHouse connection ────────────────────────────────────────────────

def get_ch_client():
    return clickhouse_connect.get_client(
        host=os.getenv("HFT_CLICKHOUSE_HOST", "localhost"),
        port=int(os.getenv("HFT_CLICKHOUSE_PORT", "8123")),
        username=os.getenv("HFT_CLICKHOUSE_USER", "default"),
        password=os.getenv("CLICKHOUSE_PASSWORD", ""),
    )


def load_day_from_ch(client, date_str: str) -> dict:
    """Load one day of TXFD6 BidAsk + Tick data from ClickHouse."""
    # BidAsk data
    ba_result = client.query(f"""
        SELECT exch_ts, bids_price[1], bids_vol[1], asks_price[1], asks_vol[1]
        FROM hft.market_data
        WHERE symbol = 'TXFD6' AND type = 'BidAsk'
          AND toDate(toDateTime64(exch_ts/1e9, 3)) = '{date_str}'
          AND bids_price[1] > 0 AND asks_price[1] > 0
        ORDER BY exch_ts
    """)
    ba_rows = ba_result.result_rows
    if not ba_rows:
        return {}

    ba_ts = np.array([r[0] for r in ba_rows], dtype=np.int64)
    ba_bp = np.array([r[1] for r in ba_rows], dtype=np.int64)
    ba_bv = np.array([r[2] for r in ba_rows], dtype=np.int64)
    ba_ap = np.array([r[3] for r in ba_rows], dtype=np.int64)
    ba_av = np.array([r[4] for r in ba_rows], dtype=np.int64)

    # Tick data
    tk_result = client.query(f"""
        SELECT exch_ts, price_scaled, volume, trade_direction
        FROM hft.market_data
        WHERE symbol = 'TXFD6' AND type = 'Tick'
          AND toDate(toDateTime64(exch_ts/1e9, 3)) = '{date_str}'
          AND price_scaled > 0
        ORDER BY exch_ts
    """)
    tk_rows = tk_result.result_rows

    tk_ts = np.array([r[0] for r in tk_rows], dtype=np.int64) if tk_rows else np.array([], dtype=np.int64)
    tk_price = np.array([r[1] for r in tk_rows], dtype=np.int64) if tk_rows else np.array([], dtype=np.int64)
    tk_vol = np.array([r[2] for r in tk_rows], dtype=np.int64) if tk_rows else np.array([], dtype=np.int64)
    tk_dir = np.array([r[3] for r in tk_rows], dtype=np.int8) if tk_rows else np.array([], dtype=np.int8)

    # Infer trade direction via tick-rule where missing
    if len(tk_price) > 1:
        for i in range(1, len(tk_dir)):
            if tk_dir[i] == 0:
                diff = tk_price[i] - tk_price[i - 1]
                if diff > 0:
                    tk_dir[i] = 1
                elif diff < 0:
                    tk_dir[i] = -1
                elif i > 0:
                    tk_dir[i] = tk_dir[i - 1]

    # Derived fields (price scale = x1e6 in CK)
    SCALE = 1_000_000
    mid = (ba_bp.astype(np.float64) + ba_ap.astype(np.float64)) / 2.0
    spread_pts = (ba_ap.astype(np.float64) - ba_bp.astype(np.float64)) / SCALE
    total_vol = ba_bv + ba_av
    qi_1 = np.where(total_vol > 0,
                    (ba_bv.astype(np.float64) - ba_av.astype(np.float64)) / total_vol,
                    0.0)

    return {
        "date": date_str,
        "ba_ts": ba_ts, "ba_bp": ba_bp, "ba_bv": ba_bv,
        "ba_ap": ba_ap, "ba_av": ba_av,
        "mid": mid, "spread_pts": spread_pts, "qi_1": qi_1,
        "tk_ts": tk_ts, "tk_price": tk_price, "tk_vol": tk_vol, "tk_dir": tk_dir,
        "n_ba": len(ba_ts), "n_tk": len(tk_ts),
    }


# ── Signal Computation (vectorized) ─────────────────────────────────────

def _lehmer_code(vals):
    """Lehmer code for a single D-length segment. Returns 0..D!-1."""
    d = len(vals)
    ranks = [0] * d
    for i in range(d):
        for j in range(d):
            if vals[j] < vals[i] or (vals[j] == vals[i] and j < i):
                ranks[i] += 1
    idx = 0
    f = 1
    factorials = [1] * d
    for i in range(d - 1, -1, -1):
        factorials[i] = f
        f *= (d - i)
    for i in range(d):
        count = sum(1 for j in range(i + 1, d) if ranks[j] < ranks[i])
        idx += count * factorials[i]
    return idx


def compute_pe_signal(qi_1: np.ndarray, d: int = 4, w: int = 100) -> np.ndarray:
    """Compute PE entropy H for each BidAsk snapshot."""
    n = len(qi_1)
    h_arr = np.ones(n, dtype=np.float64)  # default: max entropy (safe)
    if n < w:
        return h_arr

    n_patterns = math.factorial(d)
    h_max = math.log2(n_patterns)
    pat_per_win = w - d + 1

    # Pre-compute all ordinal pattern indices
    all_patterns = np.empty(n - d + 1, dtype=np.int32)
    for i in range(len(all_patterns)):
        all_patterns[i] = _lehmer_code(qi_1[i:i + d].tolist())

    # Sliding histogram
    counts = np.bincount(all_patterns[:pat_per_win], minlength=n_patterns).astype(np.float64)

    def _entropy(counts, n_samp):
        probs = counts[counts > 0] / n_samp
        return -np.sum(probs * np.log2(probs)) / h_max

    out_len = len(all_patterns) - pat_per_win + 1
    step = max(1, out_len // 20_000)

    h_val = _entropy(counts, pat_per_win)
    for i in range(out_len):
        if i > 0:
            counts[all_patterns[i - 1]] -= 1
            counts[all_patterns[i + pat_per_win - 1]] += 1
        if i % step == 0:
            h_val = _entropy(counts, pat_per_win)
        # Map back to original index: pattern at position i corresponds to BA snapshot i + d - 1
        ba_idx = i + w - 1
        if ba_idx < n:
            h_arr[ba_idx] = h_val

    return h_arr


def compute_queue_signal(bv: np.ndarray, av: np.ndarray, ema_alpha: float = 0.05) -> tuple[np.ndarray, np.ndarray]:
    """Compute queue depletion probabilities for bid and ask sides."""
    n = len(bv)
    p_depl_bid = np.full(n, 0.5)
    p_depl_ask = np.full(n, 0.5)

    lam_b = 1.0; mu_b = 1.0
    lam_a = 1.0; mu_a = 1.0

    for i in range(1, n):
        db = int(bv[i]) - int(bv[i - 1])
        da = int(av[i]) - int(av[i - 1])

        if db > 0:
            lam_b = ema_alpha * db + (1 - ema_alpha) * lam_b
        elif db < 0:
            mu_b = ema_alpha * (-db) + (1 - ema_alpha) * mu_b

        if da > 0:
            lam_a = ema_alpha * da + (1 - ema_alpha) * lam_a
        elif da < 0:
            mu_a = ema_alpha * (-da) + (1 - ema_alpha) * mu_a

        rho_b = mu_b / max(lam_b, 1e-6)
        rho_a = mu_a / max(lam_a, 1e-6)
        qb = max(int(bv[i]), 1)
        qa = max(int(av[i]), 1)

        p_depl_bid[i] = min(1.0, rho_b ** qb)
        p_depl_ask[i] = min(1.0, rho_a ** qa)

    return p_depl_bid, p_depl_ask


def compute_mfg_signal(
    tk_ts: np.ndarray, tk_dir: np.ndarray, tk_vol: np.ndarray,
    ba_ts: np.ndarray, ema_alpha: float = 0.01,
) -> tuple[np.ndarray, np.ndarray]:
    """Compute MFG capitulation z-score and flow direction for each BA snapshot."""
    n_ba = len(ba_ts)
    cap_z = np.zeros(n_ba, dtype=np.float64)
    flow_dir = np.zeros(n_ba, dtype=np.int8)

    if len(tk_ts) == 0:
        return cap_z, flow_dir

    signed_flow_ema = 0.0
    flow_var_ema = 1.0
    tk_idx = 0

    for i in range(n_ba):
        # Process all ticks up to this BA snapshot
        while tk_idx < len(tk_ts) and tk_ts[tk_idx] <= ba_ts[i]:
            signed = int(tk_dir[tk_idx]) * int(tk_vol[tk_idx])
            signed_flow_ema = ema_alpha * signed + (1 - ema_alpha) * signed_flow_ema
            dev_sq = (signed - signed_flow_ema) ** 2
            flow_var_ema = ema_alpha * dev_sq + (1 - ema_alpha) * flow_var_ema
            tk_idx += 1

        std = max(math.sqrt(flow_var_ema), 1e-6)
        cap_z[i] = abs(signed_flow_ema) / std
        flow_dir[i] = 1 if signed_flow_ema > 0 else (-1 if signed_flow_ema < 0 else 0)

    return cap_z, flow_dir


# ── Backtest Engine ──────────────────────────────────────────────────────

@dataclass
class FillResult:
    side: str  # 'bid' or 'ask'
    fill_price: int  # scaled
    fwd_mid_1s: float  # mid price 1s after fill
    pnl_pts: float  # P&L in points
    spread_at_fill: float  # spread in points at time of fill
    h_at_fill: float  # PE entropy at fill
    p_depl_at_fill: float  # queue depletion prob at fill
    cap_z_at_fill: float  # MFG cap z at fill


@dataclass
class BacktestResult:
    name: str
    fills: list[FillResult] = field(default_factory=list)
    quotes_placed: int = 0
    quotes_suppressed: int = 0

    @property
    def n_fills(self) -> int:
        return len(self.fills)

    @property
    def pnl_total(self) -> float:
        return sum(f.pnl_pts for f in self.fills)

    @property
    def pnl_mean(self) -> float:
        return self.pnl_total / self.n_fills if self.n_fills > 0 else 0.0

    @property
    def pct_profitable(self) -> float:
        if not self.fills:
            return 0.0
        return sum(1 for f in self.fills if f.pnl_pts > 0) / self.n_fills * 100


SCALE_CK = 1_000_000  # ClickHouse price scale
RT_COST_NTD = 60  # 2 × 30 NTD commission
POINT_VALUE = 200  # NTD per point
RT_COST_PTS = RT_COST_NTD / POINT_VALUE  # 0.30 pts


def run_backtest(
    day: dict,
    h_arr: np.ndarray,
    p_depl_bid: np.ndarray,
    p_depl_ask: np.ndarray,
    cap_z: np.ndarray,
    flow_dir: np.ndarray,
    # Signal gates (None = disabled for naive baseline)
    pe_danger: float | None = None,
    pe_widen_thresh: float | None = None,
    queue_thresh: float | None = None,
    mfg_z_thresh: float | None = None,
    # Cost
    spread_min_pts: float = 1.0,
    name: str = "backtest",
) -> BacktestResult:
    """Simulate maker fills with optional signal gating."""
    result = BacktestResult(name=name)

    ba_ts = day["ba_ts"]
    ba_bp = day["ba_bp"]
    ba_ap = day["ba_ap"]
    mid = day["mid"]
    spread = day["spread_pts"]
    n = len(ba_ts)

    half_rt_cost = RT_COST_PTS / 2  # per-side cost

    for i in range(1, n - 100):
        s = spread[i]
        if s < spread_min_pts:
            continue

        # ── Signal gates ──────────────────────────────────────────
        quote_bid = True
        quote_ask = True

        # D1: PE regime gate
        if pe_danger is not None and h_arr[i] < pe_danger:
            result.quotes_suppressed += 1
            continue  # suppress all

        # D1: PE spread widening (widen = less aggressive = fewer adverse fills)
        pe_width_mult = 1
        if pe_widen_thresh is not None and h_arr[i] < pe_widen_thresh:
            pe_width_mult = 2

        # D2: Queue suppression
        if queue_thresh is not None:
            if p_depl_bid[i] > queue_thresh:
                quote_bid = False
            if p_depl_ask[i] > queue_thresh:
                quote_ask = False

        # D3: MFG asymmetric widening (reduce exposure on capitulation side)
        mfg_widen_bid = 0
        mfg_widen_ask = 0
        if mfg_z_thresh is not None and cap_z[i] > mfg_z_thresh:
            widen = 1  # 1 pt extra width
            if flow_dir[i] > 0:
                mfg_widen_ask = widen
            elif flow_dir[i] < 0:
                mfg_widen_bid = widen

        # ── Fill simulation ──────────────────────────────────────
        our_bid = ba_bp[i]  # post at best bid
        our_ask = ba_ap[i]  # post at best ask

        # Apply PE widening (move quotes further from mid)
        if pe_width_mult > 1:
            half_spread_ck = (ba_ap[i] - ba_bp[i]) // 2
            our_bid -= half_spread_ck * (pe_width_mult - 1)
            our_ask += half_spread_ck * (pe_width_mult - 1)

        # Apply MFG widening
        our_bid -= mfg_widen_bid * SCALE_CK
        our_ask += mfg_widen_ask * SCALE_CK

        next_mid = mid[i + 1]

        # Bid fill: price drops through our bid
        if quote_bid and next_mid <= our_bid:
            # Find mid 1s later
            fwd_idx = np.searchsorted(ba_ts, ba_ts[i + 1] + 1_000_000_000)
            if fwd_idx < n:
                fwd_mid = mid[fwd_idx]
                pnl = (fwd_mid - our_bid) / SCALE_CK - half_rt_cost
                result.fills.append(FillResult(
                    side="bid", fill_price=our_bid, fwd_mid_1s=fwd_mid,
                    pnl_pts=pnl, spread_at_fill=s,
                    h_at_fill=h_arr[i], p_depl_at_fill=p_depl_bid[i],
                    cap_z_at_fill=cap_z[i],
                ))

        # Ask fill: price rises through our ask
        if quote_ask and next_mid >= our_ask:
            fwd_idx = np.searchsorted(ba_ts, ba_ts[i + 1] + 1_000_000_000)
            if fwd_idx < n:
                fwd_mid = mid[fwd_idx]
                pnl = (our_ask - fwd_mid) / SCALE_CK - half_rt_cost
                result.fills.append(FillResult(
                    side="ask", fill_price=our_ask, fwd_mid_1s=fwd_mid,
                    pnl_pts=pnl, spread_at_fill=s,
                    h_at_fill=h_arr[i], p_depl_at_fill=p_depl_ask[i],
                    cap_z_at_fill=cap_z[i],
                ))

        result.quotes_placed += 1

    return result


# ── Main ─────────────────────────────────────────────────────────────────

def main():
    print("=" * 72, flush=True)
    print("R47 Maker Pivot — Stage 4 Backtest")
    print("=" * 72, flush=True)

    client = get_ch_client()

    # Use March-April dates with sufficient tick data
    dates = [
        "2026-03-19", "2026-03-20", "2026-03-23", "2026-03-24",
        "2026-03-26", "2026-03-27", "2026-03-30", "2026-03-31",
        "2026-04-01", "2026-04-02", "2026-04-07", "2026-04-08",
    ]

    all_naive = BacktestResult(name="Naive Maker")
    all_gated = BacktestResult(name="Signal-Gated Maker")
    all_d1only = BacktestResult(name="D1-Only (PE gate)")
    all_d2only = BacktestResult(name="D2-Only (Queue suppress)")
    all_d1d2 = BacktestResult(name="D1+D2 (PE+Queue)")

    for date_str in dates:
        print(f"\n--- {date_str} ---", flush=True)
        day = load_day_from_ch(client, date_str)
        if not day or day["n_ba"] < 1000:
            print(f"  Skipped (insufficient data: {day.get('n_ba', 0)} BA)", flush=True)
            continue

        print(f"  Loaded {day['n_ba']:,} BidAsk, {day['n_tk']:,} Ticks", flush=True)

        # Pre-compute signals
        print("  Computing PE...", end="", flush=True)
        h_arr = compute_pe_signal(day["qi_1"])
        print(f" done (median H={np.median(h_arr):.3f})", flush=True)

        print("  Computing Queue...", end="", flush=True)
        p_depl_bid, p_depl_ask = compute_queue_signal(day["ba_bv"], day["ba_av"])
        print(f" done", flush=True)

        print("  Computing MFG...", end="", flush=True)
        cap_z, flow_dir = compute_mfg_signal(
            day["tk_ts"], day["tk_dir"], day["tk_vol"], day["ba_ts"]
        )
        print(f" done (mean z={np.mean(cap_z):.2f})", flush=True)

        # Run backtests
        # 1. Naive baseline (no gates)
        naive = run_backtest(day, h_arr, p_depl_bid, p_depl_ask, cap_z, flow_dir,
                            name=f"naive_{date_str}")
        all_naive.fills.extend(naive.fills)
        all_naive.quotes_placed += naive.quotes_placed

        # 2. D1 only (PE gate)
        d1 = run_backtest(day, h_arr, p_depl_bid, p_depl_ask, cap_z, flow_dir,
                         pe_danger=0.55, pe_widen_thresh=0.70,
                         name=f"d1_{date_str}")
        all_d1only.fills.extend(d1.fills)
        all_d1only.quotes_placed += d1.quotes_placed
        all_d1only.quotes_suppressed += d1.quotes_suppressed

        # 3. D2 only (Queue suppress)
        d2 = run_backtest(day, h_arr, p_depl_bid, p_depl_ask, cap_z, flow_dir,
                         queue_thresh=0.7,
                         name=f"d2_{date_str}")
        all_d2only.fills.extend(d2.fills)
        all_d2only.quotes_placed += d2.quotes_placed

        # 4. D1+D2
        d1d2 = run_backtest(day, h_arr, p_depl_bid, p_depl_ask, cap_z, flow_dir,
                           pe_danger=0.55, pe_widen_thresh=0.70, queue_thresh=0.7,
                           name=f"d1d2_{date_str}")
        all_d1d2.fills.extend(d1d2.fills)
        all_d1d2.quotes_placed += d1d2.quotes_placed
        all_d1d2.quotes_suppressed += d1d2.quotes_suppressed

        # 5. Full gated (D1+D2+D3)
        gated = run_backtest(day, h_arr, p_depl_bid, p_depl_ask, cap_z, flow_dir,
                            pe_danger=0.55, pe_widen_thresh=0.70,
                            queue_thresh=0.7, mfg_z_thresh=2.0,
                            name=f"gated_{date_str}")
        all_gated.fills.extend(gated.fills)
        all_gated.quotes_placed += gated.quotes_placed
        all_gated.quotes_suppressed += gated.quotes_suppressed

        print(f"  Naive: {naive.n_fills} fills, "
              f"mean={naive.pnl_mean:.3f} pts, "
              f"profit%={naive.pct_profitable:.1f}%", flush=True)
        print(f"  Gated: {gated.n_fills} fills, "
              f"mean={gated.pnl_mean:.3f} pts, "
              f"profit%={gated.pct_profitable:.1f}%", flush=True)

    # ── Summary ──────────────────────────────────────────────────────
    print("\n" + "=" * 72)
    print("BACKTEST SUMMARY")
    print("=" * 72)

    strategies = [all_naive, all_d1only, all_d2only, all_d1d2, all_gated]

    print(f"\n{'Strategy':<25} {'Fills':>8} {'Mean PnL':>10} {'Total PnL':>12} "
          f"{'Profit%':>9} {'Suppressed':>12}")
    print("-" * 78)
    for s in strategies:
        print(f"{s.name:<25} {s.n_fills:>8,} {s.pnl_mean:>10.4f} "
              f"{s.pnl_total:>12.1f} {s.pct_profitable:>8.1f}% "
              f"{s.quotes_suppressed:>12,}")

    # ── Improvement Analysis ─────────────────────────────────────────
    print("\n" + "-" * 72)
    print("IMPROVEMENT vs NAIVE:")
    if all_naive.n_fills > 0:
        naive_pct = all_naive.pct_profitable
        for s in strategies[1:]:
            if s.n_fills > 0:
                delta = s.pct_profitable - naive_pct
                fill_reduction = (1 - s.n_fills / all_naive.n_fills) * 100
                print(f"  {s.name:<25}: profit% {naive_pct:.1f}% → {s.pct_profitable:.1f}% "
                      f"(Δ={delta:+.1f}pp), fills {fill_reduction:+.0f}%")

    # ── Per-spread breakdown for best strategy ───────────────────────
    best = max(strategies[1:], key=lambda s: s.pct_profitable if s.n_fills > 50 else 0)
    print(f"\nBest strategy: {best.name}")
    print(f"\nPer-spread breakdown ({best.name}):")
    spread_bins: dict[int, list[float]] = {}
    for f in best.fills:
        b = int(f.spread_at_fill)
        spread_bins.setdefault(b, []).append(f.pnl_pts)

    print(f"{'Spread':>8} {'Fills':>8} {'Mean PnL':>10} {'Profit%':>9}")
    for sp in sorted(spread_bins.keys()):
        pnls = spread_bins[sp]
        if len(pnls) >= 5:
            mean_p = np.mean(pnls)
            pct_p = np.mean(np.array(pnls) > 0) * 100
            print(f"{sp:>7}pt {len(pnls):>8,} {mean_p:>10.4f} {pct_p:>8.1f}%")

    # ── Gate C target check ──────────────────────────────────────────
    print("\n" + "=" * 72)
    target = 57.0
    if all_gated.n_fills > 0:
        achieved = all_gated.pct_profitable
        if achieved >= target:
            print(f"✅ GATE C TARGET MET: {achieved:.1f}% >= {target:.1f}%")
        else:
            gap = target - achieved
            print(f"❌ GATE C TARGET MISSED: {achieved:.1f}% < {target:.1f}% (gap: {gap:.1f}pp)")
    else:
        print("❌ No fills in gated strategy")


if __name__ == "__main__":
    main()
