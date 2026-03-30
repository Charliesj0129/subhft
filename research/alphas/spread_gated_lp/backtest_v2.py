"""
SG-LP Backtest v2 — All Reviewer Fixes Applied

Fixes:
1. 36ms latency penalty on order posting
2. Strong-signal fills only (mid-change or price-level disappearance)
3. Regular hours only (08:45-13:45 TW = UTC+8)
4. Per-day P&L breakdown
5. IS stats with/without Mar 23
6. Mar 23 characterization

Config: SG=5, OBI=0.0 (two-sided, no OBI filter)
"""

from __future__ import annotations

import sys
from pathlib import Path
from dataclasses import dataclass, field
from datetime import datetime, timezone, timedelta

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent))
from impl import SGLPStrategy, Side, PendingOrder

TW = timezone(timedelta(hours=8))
LATENCY_NS = 36_000_000  # 36ms in nanoseconds
FEE_PER_LEG_PTS = 2.0
PT_VALUE_NTD = 10.0
HORIZON_1S_NS = 1_000_000_000
HORIZON_5S_NS = 5_000_000_000

DATA_DIR = Path("research/data/raw/tmfd6")
ALL_DATES = ["2026-03-20", "2026-03-23", "2026-03-24", "2026-03-25", "2026-03-26"]
IS_DATES = ["2026-03-20", "2026-03-23"]
OOS_DATES = ["2026-03-24", "2026-03-25", "2026-03-26"]


@dataclass(slots=True)
class FillV2:
    fill_ts: int
    side_str: str
    fill_px: float
    spread_at_fill: float
    mid_at_fill: float
    mid_5s: float | None
    gross_capture_pts: float
    pnl_5s_drift: float
    net_pnl_pts: float
    day: str


def load_day_rh(date_str: str) -> np.ndarray:
    """Load a single day, filter to regular hours (08:45-13:45 TW)."""
    path = DATA_DIR / f"TMFD6_{date_str}_l1.npy"
    data = np.load(str(path), allow_pickle=True)
    ts = data['local_ts']

    # Filter to regular hours
    # Convert all timestamps to TW hour:minute
    # For efficiency, compute seconds-of-day in TW timezone
    # Taiwan is UTC+8, so offset = 8*3600 = 28800
    ts_sec = ts / 1e9
    # Seconds since midnight TW
    sod_tw = (ts_sec + 8 * 3600) % 86400

    rh_start = 8 * 3600 + 45 * 60  # 08:45 = 31500
    rh_end = 13 * 3600 + 45 * 60    # 13:45 = 49500

    mask = (sod_tw >= rh_start) & (sod_tw < rh_end)
    filtered = data[mask]
    return filtered


def is_strong_fill(
    prev_mid: float,
    curr_mid: float,
    prev_qty_at_level: float,
    curr_qty_at_level: float,
) -> bool:
    """Check if a queue depletion is a strong trade signal.

    Returns True if:
    (a) Mid-price changed at the depletion event, OR
    (b) The price level qty went to 0 (disappeared)

    Returns False if qty decreased but mid unchanged and level persists
    (likely cancellation).
    """
    if abs(curr_mid - prev_mid) > 0.01:
        return True  # mid changed — real trade
    if curr_qty_at_level <= 0:
        return True  # level disappeared — trade through
    return False


def run_backtest_v2(
    data: np.ndarray,
    spread_gate: int = 5,
    day_label: str = "",
) -> list[FillV2]:
    """Run SG-LP backtest with all v2 fixes."""
    bid_px = data['bid_px']
    ask_px = data['ask_px']
    bid_qty = data['bid_qty']
    ask_qty = data['ask_qty']
    mid_price = data['mid_price']
    local_ts = data['local_ts']
    n = len(data)

    if n == 0:
        return []

    fills = []

    # State
    pending_bid: PendingOrder | None = None
    pending_ask: PendingOrder | None = None
    position = 0
    max_pos = 1

    # Latency: when we decide to post, we record the "arrival time" = ts + 36ms
    # We look for the row at that arrival time to actually post
    pending_post_bid_ts: int | None = None  # timestamp when bid order would arrive
    pending_post_ask_ts: int | None = None

    prev_mid = 0.0

    for i in range(n):
        ts_i = int(local_ts[i])
        bp = float(bid_px[i])
        ap = float(ask_px[i])
        bq = float(bid_qty[i])
        aq = float(ask_qty[i])
        mp = float(mid_price[i])
        spread = ap - bp

        # --- Latency: check if pending posts have arrived ---
        if pending_post_bid_ts is not None and ts_i >= pending_post_bid_ts:
            # Order arrives now — check if spread still wide enough
            if spread >= spread_gate and position < max_pos and pending_bid is None:
                pending_bid = PendingOrder(
                    side=Side.BUY,
                    price=bp,
                    post_ts=ts_i,
                    queue_ahead=bq,
                    spread_at_post=spread,
                    obi_at_post=0.0,
                )
            pending_post_bid_ts = None

        if pending_post_ask_ts is not None and ts_i >= pending_post_ask_ts:
            if spread >= spread_gate and position > -max_pos and pending_ask is None:
                pending_ask = PendingOrder(
                    side=Side.SELL,
                    price=ap,
                    post_ts=ts_i,
                    queue_ahead=aq,
                    spread_at_post=spread,
                    obi_at_post=0.0,
                )
            pending_post_ask_ts = None

        # --- Cancel on tighten ---
        if spread < spread_gate:
            if pending_bid is not None:
                pending_bid = None
            if pending_ask is not None:
                pending_ask = None
            pending_post_bid_ts = None
            pending_post_ask_ts = None
            prev_mid = mp
            continue

        # --- Fill detection with strong-signal filter ---
        if pending_bid is not None:
            order = pending_bid
            if bp == order.price:
                if bq < order.queue_ahead:
                    consumed = order.queue_ahead - bq
                    order.queue_ahead = max(0.0, order.queue_ahead - consumed)
                    if order.queue_ahead <= 0:
                        # Potential fill — check strong signal
                        if is_strong_fill(prev_mid, mp, order.queue_ahead + consumed, bq):
                            fills.append(_make_fill(
                                ts_i, order, mp, data, local_ts, mid_price, i, n, day_label))
                            pending_bid = None
                            position += 1
                        else:
                            # Weak signal (cancellation) — reset queue but don't fill
                            order.queue_ahead = bq  # re-estimate position
            elif bp > order.price:
                # Price improved past us — strong trade-through
                fills.append(_make_fill(
                    ts_i, order, mp, data, local_ts, mid_price, i, n, day_label))
                pending_bid = None
                position += 1
            elif bp < order.price:
                pending_bid = None  # cancel stale

        if pending_ask is not None:
            order = pending_ask
            if ap == order.price:
                if aq < order.queue_ahead:
                    consumed = order.queue_ahead - aq
                    order.queue_ahead = max(0.0, order.queue_ahead - consumed)
                    if order.queue_ahead <= 0:
                        if is_strong_fill(prev_mid, mp, order.queue_ahead + consumed, aq):
                            fills.append(_make_fill(
                                ts_i, order, mp, data, local_ts, mid_price, i, n, day_label))
                            pending_ask = None
                            position -= 1
                        else:
                            order.queue_ahead = aq
            elif ap < order.price:
                fills.append(_make_fill(
                    ts_i, order, mp, data, local_ts, mid_price, i, n, day_label))
                pending_ask = None
                position -= 1
            elif ap > order.price:
                pending_ask = None

        # --- Post new orders (with latency delay) ---
        if pending_bid is None and pending_post_bid_ts is None and position < max_pos:
            pending_post_bid_ts = ts_i + LATENCY_NS

        if pending_ask is None and pending_post_ask_ts is None and position > -max_pos:
            pending_post_ask_ts = ts_i + LATENCY_NS

        # --- Cancel stale (price moved away from our level) ---
        if pending_bid is not None and pending_bid.price != bp:
            if pending_bid.price < bp:
                pending_bid = None

        if pending_ask is not None and pending_ask.price != ap:
            if pending_ask.price > ap:
                pending_ask = None

        prev_mid = mp

    return fills


def _make_fill(
    ts_i: int,
    order: PendingOrder,
    mid_at_fill: float,
    data: np.ndarray,
    local_ts: np.ndarray,
    mid_price: np.ndarray,
    idx: int,
    n: int,
    day_label: str,
) -> FillV2:
    """Create a FillV2 with post-fill P&L."""
    # Find mid at +5s
    target_5s = ts_i + HORIZON_5S_NS
    idx_5s = np.searchsorted(local_ts, target_5s, side='right') - 1
    mid_5s = float(mid_price[idx_5s]) if 0 <= idx_5s < n else None

    sign = 1 if order.side == Side.BUY else -1

    if order.side == Side.BUY:
        gross_capture = mid_at_fill - order.price
    else:
        gross_capture = order.price - mid_at_fill

    pnl_5s_drift = sign * (mid_5s - mid_at_fill) if mid_5s is not None else 0.0
    net_pnl = gross_capture + pnl_5s_drift - FEE_PER_LEG_PTS

    return FillV2(
        fill_ts=ts_i,
        side_str=order.side.name,
        fill_px=order.price,
        spread_at_fill=order.spread_at_post,
        mid_at_fill=mid_at_fill,
        mid_5s=mid_5s,
        gross_capture_pts=gross_capture,
        pnl_5s_drift=pnl_5s_drift,
        net_pnl_pts=net_pnl,
        day=day_label,
    )


def compute_day_stats(fills: list[FillV2], day: str, eligible_minutes: float) -> dict:
    """Stats for a single day."""
    day_fills = [f for f in fills if f.day == day]
    n = len(day_fills)
    if n == 0:
        return {
            'day': day, 'fills': 0, 'eligible_min': eligible_minutes,
            'eligible_pct': eligible_minutes / 300 * 100,
            'daily_pnl_pts': 0, 'daily_pnl_ntd': 0, 'win_rate': 0,
            'avg_pnl': 0, 'avg_gross': 0, 'avg_drift': 0,
        }
    pnls = np.array([f.net_pnl_pts for f in day_fills])
    gross = np.array([f.gross_capture_pts for f in day_fills])
    drifts = np.array([f.pnl_5s_drift for f in day_fills])
    return {
        'day': day,
        'fills': n,
        'eligible_min': eligible_minutes,
        'eligible_pct': eligible_minutes / 300 * 100,
        'daily_pnl_pts': float(pnls.sum()),
        'daily_pnl_ntd': float(pnls.sum() * PT_VALUE_NTD),
        'win_rate': float((pnls > 0).sum() / n),
        'avg_pnl': float(pnls.mean()),
        'avg_gross': float(gross.mean()),
        'avg_drift': float(drifts.mean()),
    }


def compute_eligible_minutes(data: np.ndarray, spread_gate: int) -> float:
    """Compute minutes where spread >= gate in regular hours data."""
    spread = data['ask_px'] - data['bid_px']
    ts = data['local_ts']
    wide_mask = spread >= spread_gate
    if wide_mask.sum() == 0:
        return 0.0
    # Estimate time: wide_rows / total_rows * total_duration_minutes
    total_duration_ns = float(ts[-1] - ts[0])
    total_min = total_duration_ns / 60e9
    return total_min * wide_mask.mean()


def main():
    print("=" * 90)
    print("SG-LP BACKTEST v2 — ALL REVIEWER FIXES APPLIED")
    print("Config: SG=5, OBI=0.0 (two-sided)")
    print("Fixes: 36ms latency, strong-signal fills, regular hours, per-day breakdown")
    print("=" * 90)

    # === Fix 6: Classify Mar 23 ===
    print("\n--- Fix 6: Mar 23 Characterization ---")
    mar23 = load_day_rh("2026-03-23")
    spread_23 = mar23['ask_px'] - mar23['bid_px']
    ts_23 = mar23['local_ts']
    print(f"Mar 23 regular hours: {len(mar23):,} rows")
    print(f"Spread distribution: min={spread_23.min():.0f}, median={np.median(spread_23):.0f}, "
          f"mean={spread_23.mean():.1f}, max={spread_23.max():.0f}")
    print(f"Spread >= 5: {100*np.mean(spread_23>=5):.1f}%")
    print(f"Spread >= 10: {100*np.mean(spread_23>=10):.1f}%")
    elig_min_23 = compute_eligible_minutes(mar23, 5)
    print(f"Eligible minutes (spread >= 5): {elig_min_23:.1f} min ({elig_min_23/300*100:.1f}% of 300)")

    # Time breakdown for Mar 23
    for label, h_start, h_end in [
        ("open 08:45-09:15", 8*3600+45*60, 9*3600+15*60),
        ("mid  09:15-12:00", 9*3600+15*60, 12*3600),
        ("close 12:00-13:45", 12*3600, 13*3600+45*60),
    ]:
        sod = (ts_23 / 1e9 + 8*3600) % 86400
        mask = (sod >= h_start) & (sod < h_end)
        if mask.sum() > 0:
            pct_wide = 100 * np.mean(spread_23[mask] >= 5)
            avg_s = spread_23[mask].mean()
            print(f"  {label}: {mask.sum():>8,} rows, avg_spread={avg_s:.1f}, >=5: {pct_wide:.1f}%")

    # === Load all days, regular hours only ===
    print("\n--- Loading Data (Regular Hours Only) ---")
    day_data = {}
    for d in ALL_DATES:
        try:
            arr = load_day_rh(d)
            if len(arr) > 0:
                day_data[d] = arr
                spread_d = arr['ask_px'] - arr['bid_px']
                elig = compute_eligible_minutes(arr, 5)
                print(f"  {d}: {len(arr):>9,} rows, elig={elig:.1f}min, "
                      f"spread>=5: {100*np.mean(spread_d>=5):.1f}%")
            else:
                print(f"  {d}: 0 rows in regular hours — SKIPPED")
        except FileNotFoundError:
            print(f"  {d}: file not found — SKIPPED")

    if not day_data:
        print("ERROR: No data loaded")
        return

    # === Run backtests per day ===
    print("\n--- Running Backtests (SG=5, OBI=0.0, 36ms latency, strong-signal fills) ---")
    all_fills: list[FillV2] = []
    day_stats_list = []

    for d, data in sorted(day_data.items()):
        fills = run_backtest_v2(data, spread_gate=5, day_label=d)
        all_fills.extend(fills)
        elig = compute_eligible_minutes(data, 5)
        stats = compute_day_stats(fills + all_fills[:0], d, elig)  # use all_fills pattern
        # Recompute from fills directly
        stats = compute_day_stats(all_fills, d, elig)
        day_stats_list.append(stats)

    # === Fix 4: Per-Day P&L Breakdown ===
    print("\n--- Fix 4: Per-Day P&L Breakdown ---")
    print(f"{'Day':<14} {'Fills':>6} {'Elig Min':>10} {'Elig %':>8} "
          f"{'P&L pts':>10} {'P&L NTD':>10} {'WR':>6} {'Avg P&L':>8} {'Avg Gross':>10} {'Avg Drift':>10}")
    print("-" * 100)

    for stats in day_stats_list:
        d = stats['day']
        period = "IS" if d in IS_DATES else "OOS"
        print(f"{d} ({period})  {stats['fills']:>6} {stats['eligible_min']:>9.1f} {stats['eligible_pct']:>7.1f}% "
              f"{stats['daily_pnl_pts']:>+9.1f} {stats['daily_pnl_ntd']:>+9.0f} "
              f"{stats['win_rate']:>5.0%} {stats['avg_pnl']:>+7.2f} {stats['avg_gross']:>+9.2f} {stats['avg_drift']:>+9.3f}")

    # === Aggregate stats ===
    is_fills = [f for f in all_fills if f.day in IS_DATES]
    oos_fills = [f for f in all_fills if f.day in OOS_DATES]
    is_excl23_fills = [f for f in all_fills if f.day in IS_DATES and f.day != "2026-03-23"]

    def agg_stats(fills: list[FillV2], label: str, n_days: int) -> dict:
        if not fills:
            return {'label': label, 'n': 0, 'avg_pnl': 0, 'total_pnl': 0,
                    'daily_ntd': 0, 'wr': 0, 'max_consec_loss': 0,
                    'fills_per_session': 0, 'avg_gross': 0, 'avg_drift': 0}
        pnls = np.array([f.net_pnl_pts for f in fills])
        gross = np.array([f.gross_capture_pts for f in fills])
        drifts = np.array([f.pnl_5s_drift for f in fills])
        max_cl = 0
        cl = 0
        for p in pnls:
            if p <= 0:
                cl += 1
                max_cl = max(max_cl, cl)
            else:
                cl = 0
        return {
            'label': label,
            'n': len(fills),
            'avg_pnl': float(pnls.mean()),
            'median_pnl': float(np.median(pnls)),
            'std_pnl': float(pnls.std()),
            'total_pnl': float(pnls.sum()),
            'daily_pts': float(pnls.sum() / max(n_days, 1)),
            'daily_ntd': float(pnls.sum() * PT_VALUE_NTD / max(n_days, 1)),
            'wr': float((pnls > 0).sum() / len(pnls)),
            'max_consec_loss': max_cl,
            'fills_per_session': len(fills) / max(n_days, 1),
            'avg_gross': float(gross.mean()),
            'avg_drift': float(drifts.mean()),
        }

    is_agg = agg_stats(is_fills, "IS (all)", len([d for d in IS_DATES if d in day_data]))
    is_excl23_agg = agg_stats(is_excl23_fills, "IS (excl Mar 23)",
                               len([d for d in IS_DATES if d in day_data and d != "2026-03-23"]))
    oos_agg = agg_stats(oos_fills, "OOS", len([d for d in OOS_DATES if d in day_data]))

    print("\n--- Aggregate Results ---")
    for agg in [is_agg, is_excl23_agg, oos_agg]:
        print(f"\n  {agg['label']}:")
        print(f"    Fills: {agg['n']} ({agg['fills_per_session']:.1f}/session)")
        print(f"    Avg P&L/fill: {agg['avg_pnl']:+.3f} pts (median: {agg.get('median_pnl',0):+.3f})")
        print(f"    Std P&L/fill: {agg.get('std_pnl',0):.3f} pts")
        print(f"    Win rate: {agg['wr']:.1%}")
        print(f"    Daily P&L: {agg.get('daily_pts',0):+.1f} pts ({agg['daily_ntd']:+.0f} NTD)")
        print(f"    Total P&L: {agg['total_pnl']:+.1f} pts")
        print(f"    Max consec losses: {agg['max_consec_loss']}")
        print(f"    Avg gross capture: {agg['avg_gross']:+.2f} pts")
        print(f"    Avg 5s drift: {agg['avg_drift']:+.3f} pts")

    # === Spread bucket breakdown (OOS) ===
    print("\n--- OOS Spread Bucket Breakdown ---")
    for bname, bmin, bmax in [('5-6', 5, 6), ('7-10', 7, 10), ('11-20', 11, 20), ('20+', 20, 99999)]:
        bf = [f for f in oos_fills if bmin <= f.spread_at_fill <= bmax]
        if bf:
            pnls = np.array([f.net_pnl_pts for f in bf])
            print(f"  {bname}: n={len(bf)}, avg_pnl={pnls.mean():+.2f}, "
                  f"wr={100*(pnls>0).sum()/len(pnls):.0f}%, total={pnls.sum():+.1f}")

    # === KILL GATES ===
    print("\n" + "=" * 90)
    print("KILL GATE EVALUATION (STRICT)")
    print("=" * 90)

    gate1_pass = oos_agg['avg_pnl'] > 1.5
    gate2_pass = oos_agg['fills_per_session'] >= 5
    gate3_pass = is_excl23_agg['avg_pnl'] > 0 if is_excl23_agg['n'] > 0 else False

    print(f"\n  [GATE 1] OOS avg P&L/fill > +1.5 pts: {oos_agg['avg_pnl']:+.3f}")
    print(f"           {'PASS' if gate1_pass else '>>> KILL <<<'}")

    print(f"\n  [GATE 2] OOS fills/session >= 5: {oos_agg['fills_per_session']:.1f}")
    print(f"           {'PASS' if gate2_pass else '>>> KILL <<<'}")

    print(f"\n  [GATE 3] IS excl Mar 23 avg P&L > 0: {is_excl23_agg['avg_pnl']:+.3f}")
    print(f"           {'PASS' if gate3_pass else '>>> KILL <<<'}")

    all_pass = gate1_pass and gate2_pass and gate3_pass

    print(f"\n  {'=' * 40}")
    if all_pass:
        print(f"  VERDICT: PASS — READY FOR SHADOW")
    else:
        print(f"  VERDICT: KILL — NOT READY")
    print(f"  {'=' * 40}")


if __name__ == '__main__':
    main()
