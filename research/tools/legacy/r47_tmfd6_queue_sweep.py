"""R47 TMFD6 hftbacktest — queue model parameter sweep.

Deployed config: spr>=4, mp=3, all gates off.
Sweep PowerProbQueueModel power=[0.5, 1.0, 1.5, 2.0, 2.5, 3.0] + ProbQueueModel.
Cost: 2.0 pts/side = 4.0 pts RT (TMFD6).

Usage:
    uv run python research/tools/r47_tmfd6_queue_sweep.py
"""
from __future__ import annotations

import json
import math
import os
import sys
import time
from pathlib import Path

import numpy as np

os.environ.setdefault("HFT_STRICT_PRICE_MODE", "0")

_REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_REPO / "src"))

import structlog
structlog.configure(wrapper_class=structlog.make_filtering_bound_logger(50))
import logging
logging.disable(logging.WARNING)

from hftbacktest import BacktestAsset, HashMapMarketDepthBacktest
from hftbacktest.order import GTC, LIMIT

TICK = 1.0
LOT = 1.0
PRICE_SCALE = 10_000
ELAPSE = 100_000_000  # 100ms
POINT_VALUE = 10  # NTD per pt (TMFD6)
COST_PER_SIDE_PTS = 2.0  # 1.3 comm + 0.7 tax
RT_COST_PTS = COST_PER_SIDE_PTS * 2  # 4.0

DATA_DIR = _REPO / "research" / "data" / "raw" / "tmfd6"
DATA_FILES = sorted(DATA_DIR.glob("TMFD6_2026-*_l2.hftbt.npz"))
OUT = _REPO / "outputs" / "team_artifacts" / "alpha-research" / "R47_maker_pivot"

# Deployed config
SPR_THRESH = 4
MAX_POS = 3

_B0 = 0; _B1 = 1; _B8 = 8; _B9 = 9; _B10 = 10


def run_day(data_path: Path, queue_setup: str, power: float = 3.0) -> dict | None:
    """Run one day. queue_setup: 'power' or 'prob'."""
    from hft_platform.contracts.strategy import IntentType, Side, TIF, OrderIntent
    from hft_platform.events import FeatureUpdateEvent, LOBStatsEvent
    from hft_platform.strategy.base import StrategyContext
    from hft_platform.strategies.r47_maker import R47MakerStrategy

    date = data_path.stem.replace("TMFD6_", "").replace("_l2.hftbt", "")

    strat = R47MakerStrategy(
        strategy_id="r47_sweep",
        pe_danger_threshold=0.0,
        pe_window=100,
        queue_cancel_threshold=1.0,   # disabled
        mfg_skew_z_threshold=100.0,   # disabled
        spread_threshold_pts=SPR_THRESH,
        toxicity_max=9999,
        max_pos=MAX_POS,
    )

    builder = BacktestAsset().data([str(data_path)]).linear_asset(1.0)
    builder = builder.constant_order_latency(47_000, 47_000)
    if queue_setup == "power":
        builder = builder.power_prob_queue_model(power)
    else:
        builder = builder.prob_queue_model()
    builder = builder.tick_size(TICK).lot_size(LOT).no_partial_fill_exchange()

    hbt = HashMapMarketDepthBacktest([builder])

    pos_dict = {"TMFD6": 0}
    seq = [0]
    intents: list[OrderIntent] = []

    def factory(strategy_id, symbol, side, price, qty, tif, intent_type, **kw):
        seq[0] += 1
        i = OrderIntent(intent_id=seq[0], strategy_id=strategy_id,
                        symbol=symbol, intent_type=intent_type,
                        side=side, price=price, qty=qty, tif=tif)
        intents.append(i)
        return i

    def scaler(sym, p):
        from decimal import Decimal
        return p if isinstance(p, int) else int(Decimal(str(p)) * Decimal(PRICE_SCALE))

    ctx = StrategyContext(positions=pos_dict, strategy_id=strat.strategy_id,
                          intent_factory=factory, price_scaler=scaler)

    oid = 0; ab: int | None = None; as_: int | None = None
    eq: list[float] = []; step = 0; t0_ns = 0; te_ns = 0
    spread_samples: list[float] = []

    while hbt.elapse(ELAPSE) == 0:
        dp = hbt.depth(0)
        bb, ba = dp.best_bid, dp.best_ask
        if bb != bb or ba != ba or bb <= 0 or ba >= 2147483647 or bb >= ba:
            continue
        ts = int(hbt.current_timestamp)
        if not t0_ns: t0_ns = ts
        te_ns = ts

        spread_samples.append(ba - bb)

        if ab is not None: hbt.cancel(0, ab, False); ab = None
        if as_ is not None: hbt.cancel(0, as_, False); as_ = None
        hbt.clear_inactive_orders(0)

        pos = int(hbt.position(0)); pos_dict["TMFD6"] = pos

        bq = int(getattr(dp, "best_bid_qty", 0) or 0)
        aq = int(getattr(dp, "best_ask_qty", 0) or 0)
        bs = int(round(bb * PRICE_SCALE)); a_s = int(round(ba * PRICE_SCALE))
        tot = bq + aq; imb = (bq - aq) / tot if tot > 0 else 0.0

        v = [0]*27; v[_B0]=bs; v[_B1]=a_s; v[_B8]=bq; v[_B9]=aq
        v[_B10] = int(imb*1_000_000)
        fids = tuple(f"f{i}" for i in range(27))

        fe = FeatureUpdateEvent(symbol="TMFD6", ts=ts, local_ts=ts, seq=step,
                                feature_set_id="lob_shared_v3", schema_version=3,
                                changed_mask=0xFFFFFFFF, warmup_ready_mask=0xFFFFFFFF,
                                quality_flags=0, feature_ids=fids, values=tuple(v))
        se = LOBStatsEvent(symbol="TMFD6", ts=ts, imbalance=imb,
                           best_bid=bs, best_ask=a_s, bid_depth=bq, ask_depth=aq)

        intents.clear()
        strat.handle_event(ctx, fe)
        strat.handle_event(ctx, se)

        bd = sd = False
        for i in intents:
            if i.intent_type != IntentType.NEW: continue
            px = round(i.price / PRICE_SCALE / TICK) * TICK
            if i.side == Side.BUY and not bd:
                oid += 1; hbt.submit_buy_order(0, oid, px, float(i.qty), GTC, LIMIT, False)
                ab = oid; bd = True
            elif i.side == Side.SELL and not sd:
                oid += 1; hbt.submit_sell_order(0, oid, px, float(i.qty), GTC, LIMIT, False)
                as_ = oid; sd = True

        if step % 10 == 0:
            sv = hbt.state_values(0)
            eq.append(sv.balance + pos * (bb + ba) / 2.0)
        step += 1

    sv = hbt.state_values(0)
    fp = int(hbt.position(0))
    fm = (bb + ba) / 2.0 if bb > 0 and ba < 2147483647 else 0.0
    pnl = sv.balance + fp * fm
    vol = int(sv.trading_volume)
    hbt.close()

    # Costs
    if fp >= 0:
        buys = (vol + fp) // 2; sells = vol - buys
    else:
        sells = (vol - fp) // 2; buys = vol - sells
    rts = min(buys, sells)
    remaining = vol - rts * 2
    cost_pts = rts * RT_COST_PTS + remaining * COST_PER_SIDE_PTS
    net_pts = pnl - cost_pts

    # Equity curve
    ea = np.array(eq, dtype=np.float64) if eq else np.array([0.0])
    if len(ea) >= 2:
        r = np.diff(ea); c = ea - ea[0]
        dd = float(np.max(np.maximum.accumulate(c) - c))
        s = float(np.std(r))
        sharpe = float(np.mean(r)) / s * math.sqrt(len(ea) * 252) if s > 1e-12 else 0.0
    else:
        dd = sharpe = 0.0

    hrs = (te_ns - t0_ns) / 3.6e12 if te_ns > t0_ns else 0.0
    avg_spr = float(np.mean(spread_samples)) if spread_samples else 0.0

    return {
        "date": date, "gross_pnl": round(pnl, 2), "cost_pts": round(cost_pts, 2),
        "net_pnl": round(net_pts, 2), "net_ntd": round(net_pts * POINT_VALUE, 0),
        "fills": vol, "rts": rts, "pnl_per_fill": round(pnl / vol, 3) if vol else 0,
        "net_per_fill": round(net_pts / vol, 3) if vol else 0,
        "max_dd": round(dd, 2), "sharpe": round(sharpe, 2),
        "pos": fp, "hours": round(hrs, 2), "avg_spread": round(avg_spr, 2),
        "spr_blocked": getattr(strat, "_spread_blocked", 0),
        "quotes": getattr(strat, "_quotes_sent", 0),
    }


def main():
    print(f"R47 TMFD6 Queue Model Sweep — {len(DATA_FILES)} days")
    print(f"Config: spr>={SPR_THRESH}, mp={MAX_POS}, gates=off")
    print(f"Cost: {COST_PER_SIDE_PTS} pts/side = {RT_COST_PTS} pts RT")
    print("=" * 100)

    configs = [
        ("power_0.5", "power", 0.5),
        ("power_1.0", "power", 1.0),
        ("power_1.5", "power", 1.5),
        ("power_2.0", "power", 2.0),
        ("power_2.5", "power", 2.5),
        ("power_3.0", "power", 3.0),
        ("prob_queue", "prob", 0.0),
    ]

    all_results = {}

    for label, qtype, pw in configs:
        t0 = time.monotonic()
        days = []
        for dp in DATA_FILES:
            try:
                r = run_day(dp, qtype, pw)
                if r: days.append(r)
            except Exception as e:
                print(f"  {label} {dp.stem} FAIL: {e}")

        dt = time.monotonic() - t0
        n = len(days)
        if not n:
            print(f"{label:>12}: NO DATA")
            continue

        gross = sum(d["gross_pnl"] for d in days)
        cost = sum(d["cost_pts"] for d in days)
        net = sum(d["net_pnl"] for d in days)
        fills = sum(d["fills"] for d in days)
        rts = sum(d["rts"] for d in days)
        win = sum(1 for d in days if d["net_pnl"] > 0)
        worst_dd = max(d["max_dd"] for d in days)

        daily_net = [d["net_pnl"] for d in days]
        da = np.array(daily_net)
        mn = float(np.mean(da)); sd = float(np.std(da, ddof=1)) if n > 1 else 0.0
        t_stat = mn / (sd / math.sqrt(n)) if sd > 0 else 0.0

        net_per_fill = net / fills if fills else 0
        avg_spr = float(np.mean([d["avg_spread"] for d in days]))

        print(f"{label:>12} | net={net:>+10.0f} pts ({net*POINT_VALUE:>+10,.0f} NTD) | "
              f"fills={fills:>6} rts={rts:>5} | net/fill={net_per_fill:>+6.3f} | "
              f"win={win}/{n} t={t_stat:>+5.2f} | dd={worst_dd:>7.0f} | "
              f"spr={avg_spr:.1f} | {dt:.0f}s")

        all_results[label] = {
            "queue_type": qtype, "power": pw,
            "n_days": n, "gross_pnl": round(gross, 2),
            "cost_pts": round(cost, 2), "net_pnl": round(net, 2),
            "net_ntd": round(net * POINT_VALUE, 0),
            "fills": fills, "round_trips": rts,
            "net_per_fill": round(net_per_fill, 3),
            "winning_days": win, "losing_days": n - win,
            "t_stat": round(t_stat, 3),
            "worst_max_dd": round(worst_dd, 2),
            "avg_spread": round(avg_spr, 2),
            "daily_mean_ntd": round(mn * POINT_VALUE, 0),
            "daily_std_ntd": round(sd * POINT_VALUE, 0),
            "per_day": days,
        }

    # Summary
    print("\n" + "=" * 100)
    print(f"{'Config':>12} | {'Net PnL':>10} | {'NTD':>12} | {'Fills':>6} | {'Net/Fill':>8} | "
          f"{'Win':>5} | {'t-stat':>6} | {'MaxDD':>7}")
    print("-" * 100)
    for label, r in all_results.items():
        print(f"{label:>12} | {r['net_pnl']:>+10.0f} | {r['net_ntd']:>+12,.0f} | "
              f"{r['fills']:>6} | {r['net_per_fill']:>+8.3f} | "
              f"{r['winning_days']}/{r['n_days']:>2} | {r['t_stat']:>+6.2f} | "
              f"{r['worst_max_dd']:>7.0f}")

    # Find sweet spot
    profitable = {k: v for k, v in all_results.items() if v["net_pnl"] > 0}
    if profitable:
        best = max(profitable.items(), key=lambda x: x[1]["t_stat"])
        print(f"\nBest config: {best[0]} (t={best[1]['t_stat']:.2f}, "
              f"net={best[1]['net_ntd']:+,.0f} NTD)")
    else:
        least_bad = max(all_results.items(), key=lambda x: x[1]["net_pnl"])
        print(f"\nLeast bad: {least_bad[0]} (net={least_bad[1]['net_ntd']:+,.0f} NTD)")

    out_path = OUT / "tmfd6_queue_sweep_results.json"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with open(out_path, "w") as f:
        json.dump(all_results, f, indent=2)
    print(f"Saved to {out_path}")


if __name__ == "__main__":
    main()
