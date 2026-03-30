"""TMFD6 OpMM tick-level backtest v2.

Fixes from Challenger + Execution review:
- C1: Production-parity quoting (micro-price + imbalance skew + inventory skew)
- C2: Threshold in bps (matching production OpMM)
- C3: Stop-loss per trade (explicit, not just timeout)
- C4: Cancel latency modeled (47ms)
- C5: Adverse selection measured at 1s, 5s, 30s horizons
- C6: No force-close timeout; cancel when spread narrows (production behavior)
- C7: Explicit fill-rate discount for queue position risk

Fee: 40 NTD RT = 4 points. Latency: 36ms submit, 47ms cancel.
"""

from __future__ import annotations

import json
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

import numpy as np

# --- Production-parity constants (from simple_mm.py) ---
IMBALANCE_COEFF = 0.20  # 20% of spread for imbalance skew
INVENTORY_SKEW_DIVISOR = 5
TICK_SIZE_RATIO_PCT = 50  # quote_width = max(tick_size, half_spread)
TMFD6_TICK_SIZE = 1  # 1 index point


@dataclass
class Config:
    data_path: str = "research/data/raw/tmfd6/TMFD6_all_l1.npy"
    spread_threshold_bps: float = 4.0  # production-style bps threshold
    rt_cost_pts: float = 4.0
    half_cost_pts: float = 2.0
    submit_latency_ns: int = 36_000_000  # 36ms
    cancel_latency_ns: int = 47_000_000  # 47ms
    max_position: int = 1
    stop_loss_pts: float = 20.0  # exit if unrealized loss exceeds this
    fill_discount: float = 0.7  # probability we're actually filled (queue priority discount)
    output_dir: str = "research/experiments/validations/tmfd6_opmm"


@dataclass
class Stats:
    total_pnl: float = 0.0
    n_buys: int = 0
    n_sells: int = 0
    n_stop_losses: int = 0
    n_cancel_exits: int = 0  # closed by cancelling + re-entering at worse price
    trade_pnls: list[float] = field(default_factory=list)
    # Adverse selection at multiple horizons
    adverse_1s: int = 0
    adverse_5s: int = 0
    adverse_30s: int = 0
    total_as_measured: int = 0
    daily_pnl: dict[str, float] = field(default_factory=dict)
    daily_trades: dict[str, int] = field(default_factory=dict)


def _find_tick_at_offset(ts_arr: np.ndarray, start_idx: int, offset_ns: int, n: int) -> int:
    """Find the tick index closest to start_idx + offset_ns."""
    target = ts_arr[start_idx] + offset_ns
    # Linear scan (data is sorted by time)
    idx = start_idx
    while idx < n and ts_arr[idx] < target:
        idx += 1
    return min(idx, n - 1)


def run_backtest(cfg: Config) -> dict:
    data = np.load(cfg.data_path)
    bid = data["bid_px"]
    ask = data["ask_px"]
    bid_qty = data["bid_qty"]
    ask_qty = data["ask_qty"]
    ts = data["local_ts"]
    n = len(data)

    rng = np.random.default_rng(42)  # for fill discount sampling

    st = Stats()
    position = 0  # -1, 0, +1
    entry_price = 0.0
    entry_side = 0  # +1 bought, -1 sold

    # Quote state
    q_bid = 0.0
    q_ask = 0.0
    q_live_ts = 0
    q_active = False
    q_cancel_pending = False
    q_cancel_live_ts = 0

    DAY_GAP_NS = 4 * 3600 * 1_000_000_000
    current_day = ""

    for i in range(1, n):
        cb = bid[i]
        ca = ask[i]
        ct = ts[i]
        spread_pts = ca - cb
        mid = (cb + ca) / 2.0

        # Spread in bps
        spread_bps = (spread_pts / mid * 10000.0) if mid > 0 else 0.0

        # Day boundary
        if ct - ts[i - 1] > DAY_GAP_NS or i == 1:
            if position != 0:
                pnl = (mid - entry_price) * position - cfg.half_cost_pts
                st.trade_pnls.append(pnl)
                st.total_pnl += pnl
                if current_day:
                    st.daily_pnl[current_day] = st.daily_pnl.get(current_day, 0.0) + pnl
                    st.daily_trades[current_day] = st.daily_trades.get(current_day, 0) + 1
                position = 0
            q_active = False
            q_cancel_pending = False
            dt = datetime.fromtimestamp(ct / 1e9, tz=timezone.utc)
            current_day = dt.strftime("%Y-%m-%d")
            st.daily_pnl.setdefault(current_day, 0.0)
            st.daily_trades.setdefault(current_day, 0)
            continue

        # Stop loss check
        if position != 0:
            unrealized = (mid - entry_price) * position
            if unrealized < -cfg.stop_loss_pts:
                pnl = unrealized - cfg.half_cost_pts  # exit at mid + cost
                st.trade_pnls.append(pnl)
                st.total_pnl += pnl
                st.n_stop_losses += 1
                if current_day:
                    st.daily_pnl[current_day] += pnl
                    st.daily_trades[current_day] += 1
                position = 0
                q_active = False
                q_cancel_pending = False
                continue

        # Cancel completion check
        if q_cancel_pending and ct >= q_cancel_live_ts:
            q_active = False
            q_cancel_pending = False

        # Fill detection
        if q_active and not q_cancel_pending and ct >= q_live_ts:
            prev_b = bid[i - 1]
            prev_a = ask[i - 1]

            # Buy fill: bid dropped through our bid (conservative maker model)
            buy_triggered = (cb < q_bid and prev_b >= q_bid)
            # Sell fill: ask rose through our ask
            sell_triggered = (ca > q_ask and prev_a <= q_ask)

            # Fill discount: simulate queue priority (not always first)
            if buy_triggered and rng.random() > cfg.fill_discount:
                buy_triggered = False
            if sell_triggered and rng.random() > cfg.fill_discount:
                sell_triggered = False

            if buy_triggered and position <= 0:
                fill_px = q_bid
                st.n_buys += 1

                if position == -1:
                    # Close short
                    pnl = entry_price - fill_px - cfg.half_cost_pts
                    st.trade_pnls.append(pnl)
                    st.total_pnl += pnl
                    if current_day:
                        st.daily_pnl[current_day] += pnl
                        st.daily_trades[current_day] += 1
                    position = 0
                else:
                    # Open long
                    position = 1
                    entry_price = fill_px + cfg.half_cost_pts
                    entry_side = 1
                    _measure_adverse(st, ts, bid, ask, i, n, entry_side)

                q_active = False
                continue

            if sell_triggered and position >= 0:
                fill_px = q_ask
                st.n_sells += 1

                if position == 1:
                    # Close long
                    pnl = fill_px - entry_price - cfg.half_cost_pts
                    st.trade_pnls.append(pnl)
                    st.total_pnl += pnl
                    if current_day:
                        st.daily_pnl[current_day] += pnl
                        st.daily_trades[current_day] += 1
                    position = 0
                else:
                    # Open short
                    position = -1
                    entry_price = fill_px - cfg.half_cost_pts
                    entry_side = -1
                    _measure_adverse(st, ts, bid, ask, i, n, entry_side)

                q_active = False
                continue

        # --- Quote generation (production-parity logic) ---

        if spread_bps >= cfg.spread_threshold_bps:
            # Compute imbalance
            total_qty = bid_qty[i] + ask_qty[i]
            imbalance = (bid_qty[i] - ask_qty[i]) / total_qty if total_qty > 0 else 0.0

            # Micro-price = mid + imbalance * spread * coefficient
            micro_price = mid + imbalance * spread_pts * IMBALANCE_COEFF

            # Inventory skew
            inv_skew = -(position * TMFD6_TICK_SIZE) / INVENTORY_SKEW_DIVISOR
            fair_value = micro_price + inv_skew

            # Quote width = max(tick_size, half_spread)
            half_spread = spread_pts / 2.0
            tick_size = max(TMFD6_TICK_SIZE, spread_pts * TICK_SIZE_RATIO_PCT // 100)
            quote_width = max(tick_size, half_spread)

            new_bid = np.floor(fair_value - quote_width)
            new_ask = np.ceil(fair_value + quote_width)

            # Safety: bid must be > 0 and ask > bid
            if new_bid > 0 and new_ask > new_bid:
                if not q_active or abs(new_bid - q_bid) >= 1 or abs(new_ask - q_ask) >= 1:
                    q_bid = new_bid
                    q_ask = new_ask
                    q_live_ts = ct + cfg.submit_latency_ns
                    q_active = True
                    q_cancel_pending = False

        elif q_active and not q_cancel_pending:
            # Spread narrowed: initiate cancel (production behavior)
            q_cancel_pending = True
            q_cancel_live_ts = ct + cfg.cancel_latency_ns

    # End-of-data close
    if position != 0:
        mid = (bid[-1] + ask[-1]) / 2.0
        pnl = (mid - entry_price) * position - cfg.half_cost_pts
        st.trade_pnls.append(pnl)
        st.total_pnl += pnl
        if current_day:
            st.daily_pnl[current_day] += pnl

    return _compile_results(cfg, st)


def _measure_adverse(st: Stats, ts: np.ndarray, bid: np.ndarray, ask: np.ndarray,
                     fill_idx: int, n: int, side: int) -> None:
    """Measure adverse selection at 1s, 5s, 30s after fill."""
    st.total_as_measured += 1
    fill_mid = (bid[fill_idx] + ask[fill_idx]) / 2.0

    for offset_ns, attr in [(1_000_000_000, "adverse_1s"),
                             (5_000_000_000, "adverse_5s"),
                             (30_000_000_000, "adverse_30s")]:
        future_idx = _find_tick_at_offset(ts, fill_idx, offset_ns, n)
        if future_idx > fill_idx:
            future_mid = (bid[future_idx] + ask[future_idx]) / 2.0
            # Adverse = mid moved against our position (bought and mid dropped, or sold and mid rose)
            if side == 1 and future_mid < fill_mid - 0.5:
                setattr(st, attr, getattr(st, attr) + 1)
            elif side == -1 and future_mid > fill_mid + 0.5:
                setattr(st, attr, getattr(st, attr) + 1)


def _compile_results(cfg: Config, st: Stats) -> dict:
    pnls = np.array(st.trade_pnls) if st.trade_pnls else np.array([0.0])
    n_days = len(st.daily_pnl)
    dpnl = np.array(list(st.daily_pnl.values())) if st.daily_pnl else np.array([0.0])
    n_fills = st.n_buys + st.n_sells
    n_rts = len(st.trade_pnls)

    cum = np.cumsum(dpnl)
    peak = np.maximum.accumulate(cum)
    max_dd = float(np.min(cum - peak)) if len(cum) > 0 else 0.0

    # t-statistic on daily PnL
    if n_days >= 2 and np.std(dpnl) > 0:
        t_stat = float(np.mean(dpnl) / (np.std(dpnl, ddof=1) / np.sqrt(n_days)))
    else:
        t_stat = 0.0

    # Top-2 day concentration
    sorted_dpnl = np.sort(dpnl)[::-1]
    top2_pnl = float(np.sum(sorted_dpnl[:2])) if len(sorted_dpnl) >= 2 else 0.0
    total_pnl = float(np.sum(dpnl))

    return {
        "config": {
            "spread_threshold_bps": cfg.spread_threshold_bps,
            "rt_cost_pts": cfg.rt_cost_pts,
            "submit_latency_ms": cfg.submit_latency_ns // 1_000_000,
            "cancel_latency_ms": cfg.cancel_latency_ns // 1_000_000,
            "stop_loss_pts": cfg.stop_loss_pts,
            "fill_discount": cfg.fill_discount,
            "max_position": cfg.max_position,
        },
        "summary": {
            "total_pnl_pts": round(total_pnl, 2),
            "total_pnl_ntd": round(total_pnl * 10, 2),
            "n_fills": n_fills,
            "n_buys": st.n_buys,
            "n_sells": st.n_sells,
            "n_round_trips": n_rts,
            "n_days": n_days,
            "fills_per_day": round(n_fills / max(1, n_days), 1),
            "rts_per_day": round(n_rts / max(1, n_days), 1),
            "n_stop_losses": st.n_stop_losses,
            "stop_loss_pct": round(st.n_stop_losses / max(1, n_rts) * 100, 1),
        },
        "pnl_stats": {
            "mean_pnl_per_rt_pts": round(float(np.mean(pnls)), 3),
            "median_pnl_per_rt_pts": round(float(np.median(pnls)), 3),
            "std_pnl_per_rt_pts": round(float(np.std(pnls)), 3),
            "win_rate": round(float(np.mean(pnls > 0)), 3) if len(pnls) > 1 else 0.0,
        },
        "adverse_selection": {
            "total_measured": st.total_as_measured,
            "adverse_1s": st.adverse_1s,
            "adverse_1s_rate": round(st.adverse_1s / max(1, st.total_as_measured), 3),
            "adverse_5s": st.adverse_5s,
            "adverse_5s_rate": round(st.adverse_5s / max(1, st.total_as_measured), 3),
            "adverse_30s": st.adverse_30s,
            "adverse_30s_rate": round(st.adverse_30s / max(1, st.total_as_measured), 3),
        },
        "statistical": {
            "t_stat_daily": round(t_stat, 3),
            "top2_day_pnl_pts": round(top2_pnl, 2),
            "top2_concentration_pct": round(top2_pnl / max(0.01, abs(total_pnl)) * 100, 1) if total_pnl > 0 else 0.0,
            "pnl_ex_top2_pts": round(total_pnl - top2_pnl, 2),
        },
        "daily_stats": {
            "mean_daily_pnl_pts": round(float(np.mean(dpnl)), 2),
            "std_daily_pnl_pts": round(float(np.std(dpnl)), 2),
            "sharpe_daily": round(
                float(np.mean(dpnl) / np.std(dpnl)) * np.sqrt(252)
                if np.std(dpnl) > 0 else 0.0, 2,
            ),
            "win_days": int(np.sum(dpnl > 0)),
            "lose_days": int(np.sum(dpnl < 0)),
            "max_dd_pts": round(max_dd, 2),
        },
        "daily_pnl": {k: round(v, 2) for k, v in sorted(st.daily_pnl.items())},
        "daily_trades": {k: v for k, v in sorted(st.daily_trades.items())},
    }


def main() -> None:
    cfg = Config()
    thresholds = [3.0, 4.0, 5.0, 7.0, 10.0]
    if len(sys.argv) > 1:
        thresholds = [float(x) for x in sys.argv[1:]]

    all_results = {}
    for thr in thresholds:
        cfg.spread_threshold_bps = thr
        print(f"\n{'='*60}")
        print(f"TMFD6 OpMM v2 | threshold={thr:.1f} bps | cost=4pts | latency=36/47ms | stop=20pts | fill_disc=0.70")
        results = run_backtest(cfg)

        s = results["summary"]
        p = results["pnl_stats"]
        a = results["adverse_selection"]
        d = results["daily_stats"]
        t = results["statistical"]

        print(f"Days: {s['n_days']} | Fills: {s['n_fills']} ({s['fills_per_day']}/d) | RTs: {s['n_round_trips']} ({s['rts_per_day']}/d)")
        print(f"PnL: {s['total_pnl_pts']:+.0f} pts ({s['total_pnl_ntd']:+.0f} NTD)")
        print(f"Per RT: mean={p['mean_pnl_per_rt_pts']:+.2f}, median={p['median_pnl_per_rt_pts']:+.2f}, win={p['win_rate']:.0%}")
        print(f"Stops: {s['n_stop_losses']} ({s['stop_loss_pct']:.0f}%)")
        print(f"AS: 1s={a['adverse_1s_rate']:.0%} | 5s={a['adverse_5s_rate']:.0%} | 30s={a['adverse_30s_rate']:.0%}")
        print(f"Daily: Sharpe={d['sharpe_daily']:.2f}, t={t['t_stat_daily']:.2f}, DD={d['max_dd_pts']:.0f}")
        print(f"Top-2 concentration: {t['top2_concentration_pct']:.0f}% | Ex-top2: {t['pnl_ex_top2_pts']:+.0f} pts")
        print(f"Win/Lose days: {d['win_days']}/{d['lose_days']}")

        out = Path(cfg.output_dir) / f"backtest_v2_bps{thr:.0f}.json"
        out.parent.mkdir(parents=True, exist_ok=True)
        with open(out, "w") as f:
            json.dump(results, f, indent=2)

        all_results[f"{thr:.0f}bps"] = {
            "pnl_pts": s["total_pnl_pts"],
            "pnl_ntd": s["total_pnl_ntd"],
            "sharpe": d["sharpe_daily"],
            "t_stat": t["t_stat_daily"],
            "rts_day": s["rts_per_day"],
            "win_rate": p["win_rate"],
            "stops_pct": s["stop_loss_pct"],
            "as_5s": a["adverse_5s_rate"],
            "top2_conc": t["top2_concentration_pct"],
            "ex_top2": t["pnl_ex_top2_pts"],
        }

    print(f"\n{'='*60}")
    print("SUMMARY COMPARISON")
    print(f"{'Thr':>6} {'PnL':>8} {'NTD':>9} {'SR':>6} {'t':>6} {'RT/d':>6} {'Win%':>5} {'SL%':>5} {'AS5s':>5} {'Top2%':>6} {'ExT2':>8}")
    for k, v in all_results.items():
        print(f"{k:>6} {v['pnl_pts']:>+8.0f} {v['pnl_ntd']:>+9.0f} {v['sharpe']:>6.2f} {v['t_stat']:>6.2f} {v['rts_day']:>6.0f} {v['win_rate']:>5.0%} {v['stops_pct']:>5.0f} {v['as_5s']:>5.0%} {v['top2_conc']:>6.0f} {v['ex_top2']:>+8.0f}")


if __name__ == "__main__":
    main()
