"""Export TXFD6 data from ClickHouse to hftbacktest .npz, then run R47 validation.

Usage:
    uv run python research/tools/r47_ck_export_and_validate.py
"""
from __future__ import annotations

import json
import math
import os
import sys
import time
from pathlib import Path

import numpy as np

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
OUT_DIR = Path("research/data/raw/txfd6")
RESULTS_DIR = Path("outputs/team_artifacts/alpha-research/R47_maker_pivot")
POINT_VALUE = 200  # NTD per point (TXFD6 = 大台)
COMMISSION_PER_SIDE = 30  # NTD

DATES_TO_EXPORT = [
    "2026-03-26", "2026-03-27", "2026-03-30", "2026-03-31",
    "2026-04-01", "2026-04-02", "2026-04-07", "2026-04-08",
]

ALL_DATES = [
    "2026-03-19", "2026-03-20", "2026-03-23", "2026-03-24",
    "2026-03-26", "2026-03-27", "2026-03-30", "2026-03-31",
    "2026-04-01", "2026-04-02", "2026-04-07", "2026-04-08",
]


# ---------------------------------------------------------------------------
# Export CK → hftbacktest .npz
# ---------------------------------------------------------------------------

def export_day(client, date_str: str, symbol: str = "TXFD6") -> Path | None:
    """Export one day of data from ClickHouse to hftbacktest .npz format."""
    from hftbacktest.types import (
        BUY_EVENT, SELL_EVENT, DEPTH_EVENT, DEPTH_SNAPSHOT_EVENT,
        DEPTH_CLEAR_EVENT, TRADE_EVENT, EXCH_EVENT, LOCAL_EVENT,
    )
    from hftbacktest.types import event_dtype

    sym_dir = OUT_DIR if symbol == "TXFD6" else Path("research/data/raw/tmfd6")
    sym_dir.mkdir(parents=True, exist_ok=True)
    out_path = sym_dir / f"{symbol}_{date_str}_l2.hftbt.npz"
    if out_path.exists():
        print(f"  {date_str}: already exists, skipping", flush=True)
        return out_path

    # Query L5 BidAsk data
    ba = client.query(f"""
        SELECT exch_ts,
               bids_price[1], bids_vol[1], bids_price[2], bids_vol[2],
               bids_price[3], bids_vol[3], bids_price[4], bids_vol[4],
               bids_price[5], bids_vol[5],
               asks_price[1], asks_vol[1], asks_price[2], asks_vol[2],
               asks_price[3], asks_vol[3], asks_price[4], asks_vol[4],
               asks_price[5], asks_vol[5]
        FROM hft.market_data
        WHERE symbol = '{symbol}' AND type = 'BidAsk'
          AND toDate(toDateTime64(exch_ts/1e9, 3)) = '{date_str}'
          AND bids_price[1] > 0 AND asks_price[1] > 0
        ORDER BY exch_ts
    """)

    # Query Tick data
    tk = client.query(f"""
        SELECT exch_ts, price_scaled, volume, trade_direction
        FROM hft.market_data
        WHERE symbol = '{symbol}' AND type = 'Tick'
          AND toDate(toDateTime64(exch_ts/1e9, 3)) = '{date_str}'
          AND price_scaled > 0
        ORDER BY exch_ts
    """)

    ba_rows = ba.result_rows
    tk_rows = tk.result_rows

    if not ba_rows:
        print(f"  {date_str}: no BidAsk data", flush=True)
        return None

    print(f"  {date_str}: {len(ba_rows):,} BA + {len(tk_rows):,} Ticks", end="", flush=True)

    # CK prices are x1e6, hftbacktest uses raw points
    CK_SCALE = 1_000_000

    # Delta-based incremental depth updates.
    # Each snapshot is compared to the previous one. Only changed/new/removed
    # price levels emit DEPTH_EVENT. Removed levels emit qty=0.
    # This avoids accumulation of stale levels and preserves correct spread.
    bid_depth_code = int(DEPTH_EVENT | EXCH_EVENT | LOCAL_EVENT | BUY_EVENT)
    ask_depth_code = int(DEPTH_EVENT | EXCH_EVENT | LOCAL_EVENT | SELL_EVENT)
    buy_trade_code = int(TRADE_EVENT | EXCH_EVENT | LOCAL_EVENT | BUY_EVENT)
    sell_trade_code = int(TRADE_EVENT | EXCH_EVENT | LOCAL_EVENT | SELL_EVENT)
    trade_code = int(TRADE_EVENT | EXCH_EVENT | LOCAL_EVENT)

    # Worst case: 20 depth deltas per BA (10 removes + 10 adds) + 1 trade per tick
    est = len(ba_rows) * 20 + len(tk_rows)
    events = np.zeros(est, dtype=event_dtype)
    w = 0

    # Track previous snapshot state for delta computation
    prev_bids: dict[float, float] = {}  # price → qty
    prev_asks: dict[float, float] = {}

    # Merge BA and Tick by timestamp
    bi, ti = 0, 0
    while bi < len(ba_rows) or ti < len(tk_rows):
        ba_ts = ba_rows[bi][0] if bi < len(ba_rows) else float('inf')
        tk_ts = tk_rows[ti][0] if ti < len(tk_rows) else float('inf')

        if ba_ts <= tk_ts and bi < len(ba_rows):
            row = ba_rows[bi]
            ts = int(row[0])

            # Parse current L5 bids/asks
            cur_bids: dict[float, float] = {}
            for lvl in range(5):
                px = float(row[1 + lvl * 2]) / CK_SCALE
                qty = float(row[2 + lvl * 2])
                if px > 0 and qty > 0:
                    cur_bids[px] = qty
            cur_asks: dict[float, float] = {}
            for lvl in range(5):
                px = float(row[11 + lvl * 2]) / CK_SCALE
                qty = float(row[12 + lvl * 2])
                if px > 0 and qty > 0:
                    cur_asks[px] = qty

            # Emit deltas for bids: removed + changed + new
            for px in prev_bids:
                if px not in cur_bids:
                    # Level removed: emit qty=0
                    if w < len(events):
                        events[w] = (bid_depth_code, ts, ts, px, 0.0, 0, 0, 0.0)
                        w += 1
            for px, qty in cur_bids.items():
                if px not in prev_bids or prev_bids[px] != qty:
                    if w < len(events):
                        events[w] = (bid_depth_code, ts, ts, px, qty, 0, 0, 0.0)
                        w += 1

            # Emit deltas for asks
            for px in prev_asks:
                if px not in cur_asks:
                    if w < len(events):
                        events[w] = (ask_depth_code, ts, ts, px, 0.0, 0, 0, 0.0)
                        w += 1
            for px, qty in cur_asks.items():
                if px not in prev_asks or prev_asks[px] != qty:
                    if w < len(events):
                        events[w] = (ask_depth_code, ts, ts, px, qty, 0, 0, 0.0)
                        w += 1

            prev_bids = cur_bids
            prev_asks = cur_asks
            bi += 1
        else:
            row = tk_rows[ti]
            ts = int(row[0])
            px = float(row[1]) / CK_SCALE
            qty = float(row[2])
            direction = int(row[3])
            if direction > 0:
                code = buy_trade_code
            elif direction < 0:
                code = sell_trade_code
            else:
                code = trade_code
            if w < len(events):
                events[w] = (code, ts, ts, px, qty, 0, 0, 0.0)
                w += 1
            ti += 1

    events = events[:w]
    # Stable sort preserves delta ordering within same timestamp
    events = np.sort(events, order='exch_ts', kind='stable')

    np.savez_compressed(str(out_path), data=events)
    print(f" → {w:,} events → {out_path.name}", flush=True)
    return out_path


# ---------------------------------------------------------------------------
# Run hftbacktest validation (reuse the existing script's approach)
# ---------------------------------------------------------------------------

def run_hftbt_day(data_path: Path, mode: str = "r47") -> dict:
    """Run one day through hftbacktest, return result dict."""
    from hftbacktest import HashMapMarketDepthBacktest
    from hftbacktest.types import (
        BUY, SELL, LIMIT, GTC,
    )

    data = np.load(str(data_path))["data"]
    date_str = data_path.stem.replace("TXFD6_", "").replace("_l2.hftbt", "")

    hbt = HashMapMarketDepthBacktest(
        [data],
        tick_size=1.0,
        lot_size=1.0,
        maker_fee=0.0,
        taker_fee=0.0,
        order_latency=47_000,  # 47μs in ns... actually hftbacktest uses ns
        queue_model=None,  # will set below
    )

    # Simple maker loop with signal gating
    max_pos = 3
    elapse_ns = 100_000_000  # 100ms
    order_id = 1

    # D1 PE state (simplified inline)
    qi_buf = []
    pattern_counts = [0] * 24
    pat_window = []
    pe_h = 1.0
    pe_warmup = 0

    # D2 Queue state
    prev_bv = 0
    prev_av = 0
    lam_b = 1.0; mu_b = 1.0; lam_a = 1.0; mu_a = 1.0
    q_warmup = 0

    fills = []
    quotes_sent = 0
    pe_blocked = 0
    q_suppressed = 0

    while hbt.elapse(elapse_ns) == 0:
        # Get current state
        mid = (hbt.best_bid + hbt.best_ask) / 2.0
        spread = hbt.best_ask - hbt.best_bid
        if mid <= 0 or spread <= 0:
            continue

        bid_qty = float(hbt.bid_depth if hasattr(hbt, 'bid_depth') else 1)
        ask_qty = float(hbt.ask_depth if hasattr(hbt, 'ask_depth') else 1)

        # Get L1 quantities from market depth
        try:
            bid_qty = float(hbt.market_depth.get(hbt.best_bid * 10000, {}).get('qty', 1))
        except Exception:
            bid_qty = 1.0
        try:
            ask_qty = float(hbt.market_depth.get(hbt.best_ask * 10000, {}).get('qty', 1))
        except Exception:
            ask_qty = 1.0

        # D1: PE (simplified - use spread as regime proxy since full PE is slow)
        # Use spread > 2 as "safe" proxy
        if mode == "r47" and spread < 2.0:
            pe_blocked += 1
            continue

        # D2: Queue survival (simplified)
        suppress_bid = False
        suppress_ask = False
        if mode == "r47":
            db = bid_qty - prev_bv
            da = ask_qty - prev_av
            if db > 0: lam_b = 0.05 * db + 0.95 * lam_b
            elif db < 0: mu_b = 0.05 * (-db) + 0.95 * mu_b
            if da > 0: lam_a = 0.05 * da + 0.95 * lam_a
            elif da < 0: mu_a = 0.05 * (-da) + 0.95 * mu_a
            prev_bv = bid_qty
            prev_av = ask_qty

            rho_b = mu_b / max(lam_b, 1e-6)
            rho_a = mu_a / max(lam_a, 1e-6)
            if rho_b > 1.5:
                suppress_bid = True
                q_suppressed += 1
            if rho_a > 1.5:
                suppress_ask = True
                q_suppressed += 1

        # Cancel existing orders
        hbt.clear_inactive_orders(0)

        # Place new quotes
        pos = int(hbt.position)
        if pos < max_pos and not suppress_bid:
            hbt.submit_buy_order(0, order_id, hbt.best_bid, 1.0, GTC, LIMIT, False)
            order_id += 1
            quotes_sent += 1
        if pos > -max_pos and not suppress_ask:
            hbt.submit_sell_order(0, order_id, hbt.best_ask, 1.0, GTC, LIMIT, False)
            order_id += 1
            quotes_sent += 1

        # Check for fills
        # hbt processes fills internally; position changes reflect fills

    # Final stats
    final_pos = int(hbt.position)
    # Approximate PnL from equity
    equity = hbt.equity(0)

    return {
        "date": date_str,
        "mode": mode,
        "equity": float(equity),
        "position": final_pos,
        "quotes_sent": quotes_sent,
        "pe_blocked": pe_blocked,
        "q_suppressed": q_suppressed,
    }


def run_hftbt_day_v2(data_path: Path, mode: str = "r47") -> dict:
    """Run via HftBacktestAdapter (preferred, uses platform strategy)."""
    sys.path.insert(0, str(Path("src")))

    from hft_platform.backtest.adapter import HftBacktestAdapter
    from hft_platform.strategies.r47_maker import R47MakerStrategy
    from hft_platform.strategies.simple_mm import SimpleMarketMaker

    date_str = data_path.stem.replace("TXFD6_", "").replace("_l2.hftbt", "")

    if mode == "r47":
        strategy = R47MakerStrategy(
            strategy_id="r47_maker",
            pe_safe_threshold=0.85,
            pe_danger_threshold=0.55,
            pe_window=100,
            queue_cancel_threshold=0.7,
            mfg_skew_z_threshold=2.0,
            spread_threshold_pts=1,
            toxicity_max=700,
            max_pos=3,
        )
    else:
        strategy = SimpleMarketMaker(strategy_id="naive_mm", max_pos=3)

    adapter = HftBacktestAdapter(
        strategy=strategy,
        asset_symbol="TXFD6",
        data_path=str(data_path),
        latency_us=47,
        price_scale=10_000,
        tick_size=1.0,
        lot_size=1.0,
        maker_fee=0.0,
        taker_fee=0.0,
        partial_fill=True,
        queue_model="PowerProbQueueModel(3.0)",
        tick_mode="elapse",
        elapse_ns=100_000_000,
        feature_mode="lob_feature",
        dispatch_feature_events=True,
        equity_sample_ns=1_000_000_000,
        initial_balance=0.0,
    )

    t0 = time.monotonic()
    adapter.run()
    elapsed = time.monotonic() - t0

    eq_vals = adapter.equity_values
    total_pnl = float(eq_vals[-1] - eq_vals[0]) if len(eq_vals) >= 2 else 0.0

    fill_stats = adapter.fill_stats
    n_fills = fill_stats["total_fills"]

    return {
        "date": date_str,
        "mode": mode,
        "total_pnl_pts": round(total_pnl, 2),
        "total_pnl_ntd": round(total_pnl * POINT_VALUE, 0),
        "fills": n_fills,
        "buy_fills": fill_stats["buy_fills"],
        "sell_fills": fill_stats["sell_fills"],
        "elapsed_s": round(elapsed, 1),
        "pe_blocked": getattr(strategy, "_pe_blocked", 0),
        "queue_suppressed": getattr(strategy, "_queue_suppressed", 0),
        "mfg_skewed": getattr(strategy, "_mfg_skewed", 0),
        "quotes_sent": getattr(strategy, "_quotes_sent", 0),
    }


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    import clickhouse_connect

    print("=" * 72, flush=True)
    print("R47 Full Validation — CK Export + hftbacktest on ALL TXFD6 days", flush=True)
    print("=" * 72, flush=True)

    # Step 1: Export missing days
    print("\n[1] Exporting missing days from ClickHouse...", flush=True)
    client = clickhouse_connect.get_client(
        host=os.getenv("HFT_CLICKHOUSE_HOST", "localhost"),
        port=int(os.getenv("HFT_CLICKHOUSE_PORT", "8123")),
        username=os.getenv("HFT_CLICKHOUSE_USER", "default"),
        password=os.getenv("HFT_CLICKHOUSE_PASSWORD", "changeme"),
    )
    OUT_DIR.mkdir(parents=True, exist_ok=True)

    for date_str in DATES_TO_EXPORT:
        export_day(client, date_str)

    # Step 2: Run validation on all days
    print("\n[2] Running hftbacktest validation...", flush=True)

    results = []
    for date_str in ALL_DATES:
        npz_path = OUT_DIR / f"TXFD6_{date_str}_l2.hftbt.npz"
        if not npz_path.exists():
            print(f"  {date_str}: .npz not found, skipping", flush=True)
            continue

        for mode in ("r47", "naive"):
            print(f"  {date_str} [{mode}]...", end="", flush=True)
            try:
                r = run_hftbt_day_v2(npz_path, mode=mode)
                results.append(r)
                pnl = r["total_pnl_pts"]
                fills = r["fills"]
                print(f" PnL={pnl:+.1f} pts, fills={fills}, "
                      f"elapsed={r['elapsed_s']}s", flush=True)
            except Exception as e:
                print(f" ERROR: {e}", flush=True)
                results.append({
                    "date": date_str, "mode": mode,
                    "total_pnl_pts": 0, "fills": 0, "error": str(e),
                })

    # Step 3: Summary
    print("\n" + "=" * 72, flush=True)
    print("FULL VALIDATION SUMMARY", flush=True)
    print("=" * 72, flush=True)

    r47_results = [r for r in results if r["mode"] == "r47" and "error" not in r]
    naive_results = [r for r in results if r["mode"] == "naive" and "error" not in r]

    def _summary(res_list, label):
        if not res_list:
            print(f"\n{label}: No results", flush=True)
            return
        total_pnl = sum(r["total_pnl_pts"] for r in res_list)
        total_fills = sum(r["fills"] for r in res_list)
        n_days = len(res_list)
        profitable_days = sum(1 for r in res_list if r["total_pnl_pts"] > 0)
        print(f"\n{label} ({n_days} days):", flush=True)
        print(f"  Total PnL: {total_pnl:+,.1f} pts ({total_pnl * POINT_VALUE:+,.0f} NTD)", flush=True)
        print(f"  Avg PnL/day: {total_pnl / n_days:+,.1f} pts ({total_pnl * POINT_VALUE / n_days:+,.0f} NTD)", flush=True)
        print(f"  Total fills: {total_fills:,} ({total_fills / n_days:.0f}/day)", flush=True)
        print(f"  Profitable days: {profitable_days}/{n_days}", flush=True)

        # Commission-adjusted
        commission = total_fills * COMMISSION_PER_SIDE
        net_pnl_ntd = total_pnl * POINT_VALUE - commission
        print(f"  Commission: {commission:,.0f} NTD", flush=True)
        print(f"  Net PnL (after commission): {net_pnl_ntd:+,.0f} NTD ({net_pnl_ntd / n_days:+,.0f}/day)", flush=True)

        print(f"\n  Per-day:", flush=True)
        for r in res_list:
            fills = r["fills"]
            comm = fills * COMMISSION_PER_SIDE
            net = r["total_pnl_pts"] * POINT_VALUE - comm
            marker = "✅" if r["total_pnl_pts"] > 0 else "❌"
            extra = ""
            if r.get("pe_blocked"):
                extra = f" PE_blk={r['pe_blocked']:,} Q_sup={r.get('queue_suppressed', 0):,}"
            print(f"    {marker} {r['date']}: {r['total_pnl_pts']:+8.1f} pts, "
                  f"{fills:>4} fills, net={net:+,.0f} NTD{extra}", flush=True)

    _summary(r47_results, "R47 Signal-Gated")
    _summary(naive_results, "Naive Symmetric MM")

    # Save results
    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    with open(RESULTS_DIR / "full_validation_results.json", "w") as f:
        json.dump({"r47": r47_results, "naive": naive_results}, f, indent=2)
    print(f"\nResults saved to {RESULTS_DIR / 'full_validation_results.json'}", flush=True)


if __name__ == "__main__":
    main()
