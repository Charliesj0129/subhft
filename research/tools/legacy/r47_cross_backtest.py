"""R47 Cross-Instrument Backtest: TXFD6 signals → TMFD6 execution.

Commission: 1.3 pts per side (user-confirmed TMFD6 retail rate).
Fill model: queue-hit (L1 qty decrease at same price).
Spread threshold: 5 pts (breakeven = 2.6 pts RT).

Usage:
    uv run python research/tools/r47_cross_backtest.py
"""
from __future__ import annotations

import math
import os
import sys

import clickhouse_connect
import numpy as np

# ── Config ───────────────────────────────────────────────────────────────
COMMISSION_PER_SIDE_PTS = 1.3  # TMFD6 手續費
TMFD6_POINT_VALUE = 10  # NTD per point
CK_SCALE = 1_000_000
SPREAD_THRESHOLD_PTS = 5  # from strategies.yaml

DATES = [
    "2026-03-19", "2026-03-20", "2026-03-23", "2026-03-24",
    "2026-03-26", "2026-03-27", "2026-03-30", "2026-03-31",
    "2026-04-01", "2026-04-02", "2026-04-07", "2026-04-08",
]


def get_ch():
    return clickhouse_connect.get_client(
        host=os.getenv("HFT_CLICKHOUSE_HOST", "localhost"),
        port=int(os.getenv("HFT_CLICKHOUSE_PORT", "8123")),
        username=os.getenv("HFT_CLICKHOUSE_USER", "default"),
        password=os.getenv("HFT_CLICKHOUSE_PASSWORD", ""),
    )


def load_ba(client, symbol: str, date: str):
    """Load BidAsk snapshots: (ts, bp, bv, ap, av)."""
    r = client.query(f"""
        SELECT exch_ts, bids_price[1], bids_vol[1], asks_price[1], asks_vol[1]
        FROM hft.market_data
        WHERE symbol = '{symbol}' AND type = 'BidAsk'
          AND toDate(toDateTime64(exch_ts/1e9, 3)) = '{date}'
          AND bids_price[1] > 0 AND asks_price[1] > 0
        ORDER BY exch_ts
    """)
    rows = r.result_rows
    if not rows:
        return None
    return {
        "ts": np.array([x[0] for x in rows], dtype=np.int64),
        "bp": np.array([x[1] for x in rows], dtype=np.int64),
        "bv": np.array([x[2] for x in rows], dtype=np.int64),
        "ap": np.array([x[3] for x in rows], dtype=np.int64),
        "av": np.array([x[4] for x in rows], dtype=np.int64),
    }


def load_ticks(client, symbol: str, date: str):
    r = client.query(f"""
        SELECT exch_ts, price_scaled, volume, trade_direction
        FROM hft.market_data
        WHERE symbol = '{symbol}' AND type = 'Tick'
          AND toDate(toDateTime64(exch_ts/1e9, 3)) = '{date}'
          AND price_scaled > 0
        ORDER BY exch_ts
    """)
    rows = r.result_rows
    if not rows:
        return {"ts": np.array([], dtype=np.int64), "dir": np.array([], dtype=np.int8),
                "vol": np.array([], dtype=np.int64)}
    ts = np.array([x[0] for x in rows], dtype=np.int64)
    px = np.array([x[1] for x in rows], dtype=np.int64)
    d = np.array([x[3] for x in rows], dtype=np.int8)
    v = np.array([x[2] for x in rows], dtype=np.int64)
    # Tick-rule for missing direction
    for i in range(1, len(d)):
        if d[i] == 0:
            diff = px[i] - px[i - 1]
            d[i] = 1 if diff > 0 else (-1 if diff < 0 else d[i - 1])
    return {"ts": ts, "dir": d, "vol": v}


# ── Signals (from TXFD6) ─────────────────────────────────────────────────

def _lehmer(vals):
    d = len(vals)
    ranks = [0] * d
    for i in range(d):
        for j in range(d):
            if vals[j] < vals[i] or (vals[j] == vals[i] and j < i):
                ranks[i] += 1
    idx = 0
    f = 1
    facts = [1] * d
    for i in range(d - 1, -1, -1):
        facts[i] = f
        f *= (d - i)
    for i in range(d):
        c = sum(1 for j in range(i + 1, d) if ranks[j] < ranks[i])
        idx += c * facts[i]
    return idx


def compute_pe(qi, d=4, w=100):
    n = len(qi)
    h_out = np.ones(n)
    if n < w:
        return h_out
    np_pat = math.factorial(d)
    h_max = math.log2(np_pat)
    ppw = w - d + 1

    pats = np.empty(n - d + 1, dtype=np.int32)
    for i in range(len(pats)):
        pats[i] = _lehmer(qi[i:i + d].tolist())

    counts = np.bincount(pats[:ppw], minlength=np_pat).astype(np.float64)
    step = max(1, (len(pats) - ppw + 1) // 20_000)

    def _h(c, ns):
        p = c[c > 0] / ns
        return -np.sum(p * np.log2(p)) / h_max

    hv = _h(counts, ppw)
    for i in range(len(pats) - ppw + 1):
        if i > 0:
            counts[pats[i - 1]] -= 1
            counts[pats[i + ppw - 1]] += 1
        if i % step == 0:
            hv = _h(counts, ppw)
        idx = i + w - 1
        if idx < n:
            h_out[idx] = hv
    return h_out


def compute_queue(bv, av, alpha=0.05):
    n = len(bv)
    pb = np.full(n, 0.5)
    pa = np.full(n, 0.5)
    lb = mb = la = ma = 1.0
    for i in range(1, n):
        db = int(bv[i]) - int(bv[i - 1])
        da = int(av[i]) - int(av[i - 1])
        if db > 0: lb = alpha * db + (1 - alpha) * lb
        elif db < 0: mb = alpha * (-db) + (1 - alpha) * mb
        if da > 0: la = alpha * da + (1 - alpha) * la
        elif da < 0: ma = alpha * (-da) + (1 - alpha) * ma
        rb = mb / max(lb, 1e-6)
        ra = ma / max(la, 1e-6)
        pb[i] = min(1.0, rb ** max(int(bv[i]), 1))
        pa[i] = min(1.0, ra ** max(int(av[i]), 1))
    return pb, pa


def compute_mfg(tk_ts, tk_dir, tk_vol, ba_ts, alpha=0.01):
    n = len(ba_ts)
    cz = np.zeros(n)
    fd = np.zeros(n, dtype=np.int8)
    if len(tk_ts) == 0:
        return cz, fd
    sfe = 0.0; fve = 1.0; ti = 0
    for i in range(n):
        while ti < len(tk_ts) and tk_ts[ti] <= ba_ts[i]:
            s = int(tk_dir[ti]) * int(tk_vol[ti])
            sfe = alpha * s + (1 - alpha) * sfe
            dv = (s - sfe) ** 2
            fve = alpha * dv + (1 - alpha) * fve
            ti += 1
        std = max(math.sqrt(fve), 1e-6)
        cz[i] = abs(sfe) / std
        fd[i] = 1 if sfe > 0 else (-1 if sfe < 0 else 0)
    return cz, fd


# ── Cross-Instrument Fill Simulation ─────────────────────────────────────

def run_cross_backtest(
    sig_ba,   # TXFD6 BidAsk (signals)
    exec_ba,  # TMFD6 BidAsk (execution)
    h_arr,    # PE entropy on TXFD6
    pb, pa,   # Queue depletion on TXFD6
    cz, fd,   # MFG on TXFD6
    pe_danger=0.55,
    queue_thresh=0.7,
    spread_min_pts=5,
    name="",
):
    """Simulate fills on TMFD6 using TXFD6 signals.

    Signal timing: for each TMFD6 snapshot, find the latest TXFD6 signal.
    Fill model: queue-hit on TMFD6 L1 (bid_qty/ask_qty decreases at same price).
    """
    # Align: for each TMFD6 timestamp, find nearest prior TXFD6 signal
    sig_ts = sig_ba["ts"]
    exec_ts = exec_ba["ts"]
    exec_bp = exec_ba["bp"]
    exec_bv = exec_ba["bv"]
    exec_ap = exec_ba["ap"]
    exec_av = exec_ba["av"]
    n_exec = len(exec_ts)

    # Pre-compute TMFD6 spread and mid
    exec_spread = (exec_ap.astype(np.float64) - exec_bp.astype(np.float64)) / CK_SCALE
    exec_mid = (exec_bp.astype(np.float64) + exec_ap.astype(np.float64)) / 2.0

    # Signal lookup: for each exec timestamp, find latest signal index
    sig_idx_map = np.searchsorted(sig_ts, exec_ts, side="right") - 1
    sig_idx_map = np.clip(sig_idx_map, 0, len(sig_ts) - 1)

    fills = []
    n_quotes = 0
    n_pe_blocked = 0
    n_q_suppressed = 0
    n_spread_blocked = 0

    for i in range(1, n_exec - 100):
        # TMFD6 spread gate
        s = exec_spread[i]
        if s < spread_min_pts:
            n_spread_blocked += 1
            continue

        # Get TXFD6 signal at this time
        si = sig_idx_map[i]

        # D1: PE regime gate (from TXFD6)
        if pe_danger is not None and h_arr[si] < pe_danger:
            n_pe_blocked += 1
            continue

        # D2: Queue suppression (from TXFD6)
        sup_bid = queue_thresh is not None and pb[si] > queue_thresh
        sup_ask = queue_thresh is not None and pa[si] > queue_thresh
        if sup_bid:
            n_q_suppressed += 1
        if sup_ask:
            n_q_suppressed += 1

        n_quotes += 1

        # Fill detection on TMFD6: queue-hit at L1
        bid_fill = (
            not sup_bid
            and exec_bp[i] == exec_bp[i - 1]
            and exec_bv[i] < exec_bv[i - 1]
            and exec_bv[i - 1] > 0
        )
        ask_fill = (
            not sup_ask
            and exec_ap[i] == exec_ap[i - 1]
            and exec_av[i] < exec_av[i - 1]
            and exec_av[i - 1] > 0
        )

        if bid_fill:
            prob = 1.0 / max(int(exec_bv[i - 1]), 1)
            fwd = np.searchsorted(exec_ts, exec_ts[i] + 1_000_000_000)
            if fwd < n_exec:
                fwd_mid = exec_mid[fwd]
                pnl_gross = (fwd_mid - exec_bp[i]) / CK_SCALE
                pnl_net = pnl_gross - COMMISSION_PER_SIDE_PTS
                fills.append({"side": "bid", "pnl_gross": pnl_gross,
                              "pnl_net": pnl_net, "prob": prob,
                              "spread": s, "h": h_arr[si]})

        if ask_fill:
            prob = 1.0 / max(int(exec_av[i - 1]), 1)
            fwd = np.searchsorted(exec_ts, exec_ts[i] + 1_000_000_000)
            if fwd < n_exec:
                fwd_mid = exec_mid[fwd]
                pnl_gross = (exec_ap[i] - fwd_mid) / CK_SCALE
                pnl_net = pnl_gross - COMMISSION_PER_SIDE_PTS
                fills.append({"side": "ask", "pnl_gross": pnl_gross,
                              "pnl_net": pnl_net, "prob": prob,
                              "spread": s, "h": h_arr[si]})

    if not fills:
        return {"name": name, "n_fills": 0, "n_exp": 0,
                "mean_gross": 0, "mean_net": 0, "total_net": 0,
                "pct_profitable": 0, "n_quotes": n_quotes,
                "pe_blocked": n_pe_blocked, "q_suppressed": n_q_suppressed,
                "spread_blocked": n_spread_blocked}

    pnls_gross = np.array([f["pnl_gross"] for f in fills])
    pnls_net = np.array([f["pnl_net"] for f in fills])
    probs = np.array([f["prob"] for f in fills])

    return {
        "name": name,
        "n_fills": len(fills),
        "n_exp": float(np.sum(probs)),
        "mean_gross": float(np.mean(pnls_gross)),
        "mean_net": float(np.mean(pnls_net)),
        "mean_net_weighted": float(np.sum(pnls_net * probs) / max(np.sum(probs), 1e-6)),
        "total_gross": float(np.sum(pnls_gross)),
        "total_net": float(np.sum(pnls_net)),
        "total_net_weighted": float(np.sum(pnls_net * probs)),
        "pct_profitable_gross": float(np.mean(pnls_gross > 0) * 100),
        "pct_profitable_net": float(np.mean(pnls_net > 0) * 100),
        "n_quotes": n_quotes,
        "pe_blocked": n_pe_blocked,
        "q_suppressed": n_q_suppressed,
        "spread_blocked": n_spread_blocked,
        "fills": fills,
    }


def main():
    print("=" * 72, flush=True)
    print("R47 Cross-Instrument Backtest: TXFD6 signals → TMFD6 execution", flush=True)
    print(f"Commission: {COMMISSION_PER_SIDE_PTS} pts/side, "
          f"Spread min: {SPREAD_THRESHOLD_PTS} pts", flush=True)
    print("=" * 72, flush=True)

    client = get_ch()

    configs = [
        ("Naive (no gates)", {"pe_danger": None, "queue_thresh": None}),
        ("D1 PE(0.55)", {"pe_danger": 0.55, "queue_thresh": None}),
        ("D1 PE(0.65)", {"pe_danger": 0.65, "queue_thresh": None}),
        ("D1(0.55)+D2(0.7)", {"pe_danger": 0.55, "queue_thresh": 0.7}),
        ("D1(0.65)+D2(0.7)", {"pe_danger": 0.65, "queue_thresh": 0.7}),
        ("D1(0.55)+D2(0.5)", {"pe_danger": 0.55, "queue_thresh": 0.5}),
    ]

    # Aggregate results per config
    agg = {name: [] for name, _ in configs}
    day_results = []

    for date in DATES:
        print(f"\n--- {date} ---", flush=True)

        # Load TXFD6 (signal source)
        sig_ba = load_ba(client, "TXFD6", date)
        sig_tk = load_ticks(client, "TXFD6", date)
        if sig_ba is None or len(sig_ba["ts"]) < 1000:
            print(f"  TXFD6: insufficient data, skip", flush=True)
            continue

        # Load TMFD6 (execution target)
        exec_ba = load_ba(client, "TMFD6", date)
        if exec_ba is None or len(exec_ba["ts"]) < 1000:
            print(f"  TMFD6: insufficient data, skip", flush=True)
            continue

        print(f"  TXFD6: {len(sig_ba['ts']):,} BA | TMFD6: {len(exec_ba['ts']):,} BA", flush=True)

        # Compute signals from TXFD6
        total_vol = sig_ba["bv"] + sig_ba["av"]
        qi = np.where(total_vol > 0,
                      (sig_ba["bv"].astype(np.float64) - sig_ba["av"].astype(np.float64)) / total_vol,
                      0.0)
        print("  Computing PE...", end="", flush=True)
        h_arr = compute_pe(qi)
        print(f" H={np.median(h_arr):.3f}", end="", flush=True)

        pb, pa = compute_queue(sig_ba["bv"], sig_ba["av"])
        cz, fd = compute_mfg(sig_tk["ts"], sig_tk["dir"], sig_tk["vol"], sig_ba["ts"])
        print(f" | Queue+MFG done", flush=True)

        # TMFD6 spread distribution
        tmf_spread = (exec_ba["ap"].astype(np.float64) - exec_ba["bp"].astype(np.float64)) / CK_SCALE
        pct_above_5 = np.mean(tmf_spread >= 5.0) * 100
        print(f"  TMFD6 median spread: {np.median(tmf_spread):.0f} pts, "
              f">= 5pts: {pct_above_5:.1f}%", flush=True)

        # Run each config
        for cfg_name, kwargs in configs:
            r = run_cross_backtest(
                sig_ba, exec_ba, h_arr, pb, pa, cz, fd,
                spread_min_pts=SPREAD_THRESHOLD_PTS, name=cfg_name, **kwargs,
            )
            agg[cfg_name].append(r)

            if cfg_name == configs[0][0]:  # Naive
                print(f"  Naive: {r['n_fills']} fills, "
                      f"gross={r['mean_gross']:+.2f}, net={r['mean_net']:+.2f}, "
                      f"profit%(net)={r['pct_profitable_net']:.1f}%, "
                      f"spr_blk={r['spread_blocked']:,}", flush=True)
            elif cfg_name == configs[3][0]:  # D1+D2
                print(f"  D1+D2: {r['n_fills']} fills, "
                      f"gross={r['mean_gross']:+.2f}, net={r['mean_net']:+.2f}, "
                      f"profit%(net)={r['pct_profitable_net']:.1f}%", flush=True)

    # ── Summary ──────────────────────────────────────────────────────
    print("\n" + "=" * 72, flush=True)
    print("AGGREGATE RESULTS (12 days)", flush=True)
    print(f"Commission: {COMMISSION_PER_SIDE_PTS} pts/side | "
          f"TMFD6 point value: {TMFD6_POINT_VALUE} NTD", flush=True)
    print("=" * 72, flush=True)

    print(f"\n{'Config':<22} {'Fills':>7} {'E[F]':>7} "
          f"{'Gross':>8} {'Net':>8} {'E[Net]':>8} "
          f"{'P%(g)':>7} {'P%(n)':>7} {'TotNet':>10} {'NTD':>10}", flush=True)
    print("-" * 100, flush=True)

    for cfg_name, _ in configs:
        results = agg[cfg_name]
        all_fills = []
        for r in results:
            all_fills.extend(r.get("fills", []))

        if not all_fills:
            print(f"{cfg_name:<22} {'no fills':>7}", flush=True)
            continue

        pnls_g = np.array([f["pnl_gross"] for f in all_fills])
        pnls_n = np.array([f["pnl_net"] for f in all_fills])
        probs = np.array([f["prob"] for f in all_fills])

        n_f = len(all_fills)
        n_e = float(np.sum(probs))
        mg = float(np.mean(pnls_g))
        mn = float(np.mean(pnls_n))
        me = float(np.sum(pnls_n * probs) / max(np.sum(probs), 1e-6))
        pg = float(np.mean(pnls_g > 0) * 100)
        pn = float(np.mean(pnls_n > 0) * 100)
        tn = float(np.sum(pnls_n))
        ntd = tn * TMFD6_POINT_VALUE

        print(f"{cfg_name:<22} {n_f:>7,} {n_e:>7.0f} "
              f"{mg:>+8.2f} {mn:>+8.2f} {me:>+8.2f} "
              f"{pg:>6.1f}% {pn:>6.1f}% {tn:>+10.1f} {ntd:>+10,.0f}", flush=True)

    # ── Per-Spread for best config ───────────────────────────────────
    best_name = configs[3][0]  # D1+D2
    best_fills = []
    for r in agg[best_name]:
        best_fills.extend(r.get("fills", []))

    if best_fills:
        print(f"\nPer-Spread Breakdown ({best_name}):", flush=True)
        print(f"{'Spread':>8} {'Fills':>8} {'Gross':>9} {'Net':>9} "
              f"{'P%(g)':>8} {'P%(n)':>8}", flush=True)

        spread_bins: dict[int, list] = {}
        for f in best_fills:
            b = int(f["spread"])
            spread_bins.setdefault(b, []).append(f)

        for sp in sorted(spread_bins.keys()):
            fs = spread_bins[sp]
            if len(fs) >= 3:
                pg_arr = np.array([f["pnl_gross"] for f in fs])
                pn_arr = np.array([f["pnl_net"] for f in fs])
                print(f"{sp:>7}pt {len(fs):>8,} {np.mean(pg_arr):>+9.2f} "
                      f"{np.mean(pn_arr):>+9.2f} "
                      f"{np.mean(pg_arr > 0)*100:>7.1f}% "
                      f"{np.mean(pn_arr > 0)*100:>7.1f}%", flush=True)

    # ── Per-Day for best config ──────────────────────────────────────
    print(f"\nPer-Day ({best_name}):", flush=True)
    for r in agg[best_name]:
        if r["n_fills"] > 0:
            net_ntd = r["total_net"] * TMFD6_POINT_VALUE
            marker = "✅" if r["total_net"] > 0 else "❌"
            print(f"  {marker} fills={r['n_fills']:>5}, "
                  f"net={r['total_net']:>+8.1f} pts = {net_ntd:>+8,.0f} NTD, "
                  f"P%(net)={r['pct_profitable_net']:.1f}%, "
                  f"PE_blk={r['pe_blocked']:,}, Q_sup={r['q_suppressed']:,}, "
                  f"spr_blk={r['spread_blocked']:,}", flush=True)
        else:
            print(f"  ⚠️  0 fills (spread_blocked={r['spread_blocked']:,})", flush=True)


if __name__ == "__main__":
    main()
