"""P2-lite: Alpha-Driven Selective Quoting MM Backtest.

Uses OFI alpha to quote only the favorable side, avoiding adverse selection.
Instead of quoting both bid AND ask simultaneously:
  - When alpha predicts UP: only post bid (buy low, expect price to rise)
  - When alpha predicts DOWN: only post ask (sell high, expect price to fall)
  - When alpha is neutral: no quotes

Scenarios:
  A: OFI single-side quoting
  B: OFI single-side + inventory decay (quadratic penalty for aging)
  C: OFI single-side + VPIN filter (skip TOXIC regime)
  D: Multi-alpha composite (OFI + imbalance + spread_change)

Usage:
    uv run python research/tools/backtest_mm_p2_lite.py

Outputs: outputs/team_artifacts/alpha-research/stage4_mm_p2_lite.json
"""

from __future__ import annotations

import json
import math
import sys
import time
from itertools import product
from pathlib import Path
from typing import Any

import numpy as np
import structlog

logger = structlog.get_logger("backtest.mm_p2_lite")

_REPO_ROOT = Path(__file__).resolve().parents[2]
_DEFAULT_DATA = _REPO_ROOT / "research" / "data" / "raw" / "txfd6" / "TXFD6_all_l1.npy"
_OUT_PATH = (
    _REPO_ROOT
    / "outputs"
    / "team_artifacts"
    / "alpha-research"
    / "stage4_mm_p2_lite.json"
)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

TICK_SIZE_POINTS: int = 1
POINT_VALUE_NTD: int = 10  # Mini-TAIEX: 1 point = 10 NTD
RT_COST_NTD: int = 5       # round-trip cost
LATENCY_TICKS: int = 1     # 1 tick ~ 125ms >= 36ms RTT
SAMPLE_INTERVAL: int = 1000
_EPS: float = 1e-12
_SQRT2: float = math.sqrt(2.0)

# VPIN constants
VPIN_BAR_VOLUME_TARGET: int = 500
VPIN_N_BUCKETS: int = 50
VPIN_WARMUP_BARS: int = 60


# ---------------------------------------------------------------------------
# OFI computation (vectorized)
# ---------------------------------------------------------------------------

def _compute_ofi_vectorized(
    bid_px: np.ndarray, ask_px: np.ndarray,
    bid_qty: np.ndarray, ask_qty: np.ndarray,
) -> np.ndarray:
    """Vectorized L1 OFI with Lee-Ready price-level adjustments."""
    n = len(bid_px)
    ofi = np.zeros(n, dtype=np.float64)

    # Price changes
    bid_px_diff = np.diff(bid_px)
    ask_px_diff = np.diff(ask_px)
    bid_qty_diff = np.diff(bid_qty)
    ask_qty_diff = np.diff(ask_qty)

    # Delta bid: price up -> full qty, price down -> -prev_qty, same -> qty diff
    delta_bid = np.where(
        bid_px_diff > 0, bid_qty[1:],
        np.where(bid_px_diff < 0, -bid_qty[:-1], bid_qty_diff),
    )

    # Delta ask: price down -> full qty, price up -> -prev_qty, same -> qty diff
    delta_ask = np.where(
        ask_px_diff < 0, ask_qty[1:],
        np.where(ask_px_diff > 0, -ask_qty[:-1], ask_qty_diff),
    )

    ofi[1:] = delta_bid - delta_ask
    return ofi


def _ema_array(arr: np.ndarray, alpha: float) -> np.ndarray:
    """Compute EMA over array (must be sequential)."""
    n = len(arr)
    out = np.empty(n, dtype=np.float64)
    val = 0.0
    one_minus_a = 1.0 - alpha
    for i in range(n):
        val = alpha * arr[i] + one_minus_a * val
        out[i] = val
    return out


# ---------------------------------------------------------------------------
# VPIN computation (vectorized-friendly state machine)
# ---------------------------------------------------------------------------

def _compute_vpin_regime(
    bid_px: np.ndarray, ask_px: np.ndarray,
    bid_qty: np.ndarray, ask_qty: np.ndarray,
    local_ts: np.ndarray,
) -> np.ndarray:
    """Compute VPIN regime for each tick. Returns int array (0=LOW, 1=ELEV, 2=TOXIC)."""
    n = len(bid_px)
    regime = np.zeros(n, dtype=np.int32)

    # State
    bar_target = VPIN_BAR_VOLUME_TARGET
    acc_vol = 0
    buy_vol = 0
    sell_vol = 0
    bar_open = 0
    bar_close = 0
    prev_bid_d = 0
    prev_ask_d = 0
    init = False

    sigma_sq_ema = 0.0
    sigma_alpha = 0.1
    sigma_init = False

    vpin_ratios = [0.0] * VPIN_N_BUCKETS
    vpin_head = 0
    vpin_count = 0
    vpin_sum = 0.0
    bars_seen = 0

    ema_vpin = 0.0
    ema_a = 0.1175
    ema_init = False

    thr_elev = 0.4
    thr_toxic = 0.7
    calibrated = False
    cal_buf: list[float] = []
    current_regime = 0

    bid_d_arr = np.round(bid_qty).astype(np.int64)
    ask_d_arr = np.round(ask_qty).astype(np.int64)
    mid_x2_arr = (np.round(bid_px) + np.round(ask_px)).astype(np.int64)

    for i in range(n):
        regime[i] = current_regime

        mid_x2 = int(mid_x2_arr[i])
        bid_d = int(bid_d_arr[i])
        ask_d = int(ask_d_arr[i])
        price = mid_x2 // 2

        if not init:
            prev_bid_d = bid_d
            prev_ask_d = ask_d
            init = True
            continue

        db = abs(bid_d - prev_bid_d)
        da = abs(ask_d - prev_ask_d)
        churn = db + da
        bc = max(prev_bid_d - bid_d, 0)
        ac = max(prev_ask_d - ask_d, 0)
        prev_bid_d = bid_d
        prev_ask_d = ask_d

        if churn <= 0:
            continue

        tc = bc + ac
        if tc > 0:
            bf = bc / tc
            bv = int(churn * bf)
        else:
            bv = churn // 2

        if acc_vol == 0:
            bar_open = price
        bar_close = price
        acc_vol += churn
        buy_vol += bv

        if acc_vol < bar_target:
            continue

        # Bar complete
        bars_seen += 1
        total_vol = acc_vol
        bar_buy_vol = buy_vol
        acc_vol = 0
        buy_vol = 0

        dp = float(bar_close - bar_open)
        dp_sq = dp * dp
        if not sigma_init:
            sigma_sq_ema = dp_sq if dp_sq > 0 else 1.0
            sigma_init = True
        else:
            sigma_sq_ema += sigma_alpha * (dp_sq - sigma_sq_ema)
        sigma = math.sqrt(max(sigma_sq_ema, _EPS))
        z = dp / sigma
        buy_frac = 0.5 * math.erfc(-z / _SQRT2)

        if total_vol > 0:
            bv_e = total_vol * buy_frac
            sv_e = total_vol - bv_e
            ratio = abs(bv_e - sv_e) / total_vol
        else:
            ratio = 0.0

        if vpin_count >= VPIN_N_BUCKETS:
            vpin_sum -= vpin_ratios[vpin_head]
        else:
            vpin_count += 1
        vpin_ratios[vpin_head] = ratio
        vpin_sum += ratio
        vpin_head = (vpin_head + 1) % VPIN_N_BUCKETS
        raw_vpin = vpin_sum / max(vpin_count, 1)

        if not ema_init:
            ema_vpin = raw_vpin
            ema_init = True
        else:
            ema_vpin += ema_a * (raw_vpin - ema_vpin)

        if not calibrated:
            cal_buf.append(raw_vpin)
            if bars_seen >= VPIN_WARMUP_BARS and vpin_count >= VPIN_N_BUCKETS and len(cal_buf) >= 20:
                s = sorted(cal_buf)
                sn = len(s)
                p75i = 0.75 * (sn - 1)
                lo = int(p75i)
                hi = min(lo + 1, sn - 1)
                p75 = s[lo] * (1.0 - (p75i - lo)) + s[hi] * (p75i - lo)
                p95i = 0.95 * (sn - 1)
                lo = int(p95i)
                hi = min(lo + 1, sn - 1)
                p95 = s[lo] * (1.0 - (p95i - lo)) + s[hi] * (p95i - lo)
                if p75 >= p95:
                    p95 = p75 + 0.05
                if p75 <= 0.0:
                    p75 = 0.01
                thr_elev = p75
                thr_toxic = p95
                calibrated = True
                cal_buf = []

        v = ema_vpin
        if v >= thr_toxic:
            current_regime = 2
        elif v >= thr_elev:
            if current_regime == 2 and v >= thr_toxic * 0.95:
                pass
            else:
                current_regime = 1
        else:
            if current_regime == 1 and v >= thr_elev * 0.95:
                pass
            elif current_regime == 2:
                current_regime = 1
            else:
                current_regime = 0

        regime[i] = current_regime

    return regime


# ---------------------------------------------------------------------------
# Precomputed data
# ---------------------------------------------------------------------------

def _precompute(
    data: np.ndarray, ema_alphas: list[float],
) -> dict[str, Any]:
    """Precompute all shared arrays."""
    logger.info("precomputing_shared_arrays")
    t0 = time.monotonic()

    bid_px = data["bid_px"].astype(np.float64)
    ask_px = data["ask_px"].astype(np.float64)
    bid_qty = data["bid_qty"].astype(np.float64)
    ask_qty = data["ask_qty"].astype(np.float64)
    local_ts = data["local_ts"]
    n = len(data)

    mid_prices = np.round((bid_px + ask_px) / 2.0).astype(np.int64)
    best_bid_int = np.round(bid_px).astype(np.int64)
    best_ask_int = np.round(ask_px).astype(np.int64)

    # OFI
    ofi_raw = _compute_ofi_vectorized(bid_px, ask_px, bid_qty, ask_qty)

    # OFI EMA per alpha
    ofi_ema_cache: dict[float, np.ndarray] = {}
    for a in ema_alphas:
        ofi_ema_cache[a] = _ema_array(ofi_raw, a)

    # Imbalance
    total_qty = bid_qty + ask_qty
    imbalance_raw = np.where(total_qty > 0, (bid_qty - ask_qty) / total_qty, 0.0)

    # Spread change
    spreads = best_ask_int - best_bid_int
    spread_change = np.zeros(n, dtype=np.float64)
    spread_change[1:] = np.diff(spreads).astype(np.float64)

    elapsed = time.monotonic() - t0
    logger.info("precompute_done", elapsed_s=round(elapsed, 2), n_rows=n)

    return {
        "n": n,
        "mid": mid_prices,
        "best_bid": best_bid_int,
        "best_ask": best_ask_int,
        "bid_px": bid_px,
        "ask_px": ask_px,
        "bid_qty": bid_qty,
        "ask_qty": ask_qty,
        "local_ts": local_ts,
        "ofi_raw": ofi_raw,
        "ofi_ema": ofi_ema_cache,
        "imbalance_raw": imbalance_raw,
        "spread_change": spread_change,
        "spreads": spreads,
    }


# ---------------------------------------------------------------------------
# Fast simulation core (single function, minimal overhead)
# ---------------------------------------------------------------------------

def _sim_core(
    ofi: np.ndarray,           # precomputed OFI EMA
    mid: np.ndarray,           # int64 mid prices
    best_bid: np.ndarray,      # int64
    best_ask: np.ndarray,      # int64
    entry_thr: float,
    exit_thr: float,
    max_hold: int,
    max_pos: int,
    *,
    decay: bool = False,       # Scenario B: inventory decay
    vpin_regime: np.ndarray | None = None,  # Scenario C: VPIN filter
) -> dict[str, Any]:
    """Core simulation loop, optimized for speed.

    Returns metrics dict.
    """
    n = len(ofi)
    warmup = max(50, LATENCY_TICKS + 2)

    # State variables
    pos = 0
    rpnl = 0          # realized PnL in NTD
    peak_eq = 0
    max_dd = 0
    n_entries = 0
    n_exits = 0
    n_time_exits = 0
    n_vpin_blocks = 0
    entry_tick = 0
    entry_px = 0

    # Pending state
    p_side = 0         # 0=none, 1=buy, -1=sell
    p_price = 0
    p_tick = 0
    p_exit = False
    p_exit_tick = 0

    # Round trip tracking (compact)
    rt_pnls: list[int] = []
    rt_holds: list[int] = []

    # Equity sampling
    eq_samples: list[int] = []

    # Local refs for speed
    pv = POINT_VALUE_NTD
    rc = RT_COST_NTD
    lat = LATENCY_TICKS
    si = SAMPLE_INTERVAL

    # Use .item() once to get Python int arrays for tight loop
    # Actually numpy int64 indexing is fine, just avoid method calls
    mid_a = mid
    bb_a = best_bid
    ba_a = best_ask
    ofi_a = ofi
    use_vpin = vpin_regime is not None

    for i in range(warmup, n):
        bb = int(bb_a[i])
        ba = int(ba_a[i])
        mi = int(mid_a[i])
        ov = float(ofi_a[i])

        # --- Fill pending exit ---
        if p_exit:
            if i >= p_exit_tick + lat:
                pnl_pts = (mi - entry_px) * pos
                pnl_ntd = pnl_pts * pv - rc * abs(pos)
                rpnl += pnl_ntd
                rt_pnls.append(pnl_ntd)
                rt_holds.append(i - entry_tick)
                n_exits += abs(pos)
                pos = 0
                p_exit = False
                p_side = 0
            continue  # skip rest while waiting for exit fill

        # --- Fill pending entry ---
        if p_side != 0 and i >= p_tick + lat:
            filled = False
            if p_side == 1 and ba <= p_price:
                filled = True
            elif p_side == -1 and bb >= p_price:
                filled = True

            if filled:
                if p_side == 1 and pos < max_pos:
                    pos += 1
                    entry_tick = i
                    entry_px = p_price
                    n_entries += 1
                elif p_side == -1 and pos > -max_pos:
                    pos -= 1
                    entry_tick = i
                    entry_px = p_price
                    n_entries += 1
            p_side = 0

        # --- Exit logic ---
        if pos != 0:
            held = i - entry_tick

            if held > max_hold:
                p_exit = True
                p_exit_tick = i
                n_time_exits += 1
                continue

            # VPIN: force exit in TOXIC
            if use_vpin and vpin_regime[i] == 2:
                p_exit = True
                p_exit_tick = i
                continue

            # Signal reversal
            if decay:
                # Quadratic decay
                df = 1.0 - (held / max_hold) ** 2
                eff_exit = exit_thr * max(df, 0.05)
            else:
                eff_exit = exit_thr

            if pos > 0 and ov < -eff_exit:
                p_exit = True
                p_exit_tick = i
                continue
            if pos < 0 and ov > eff_exit:
                p_exit = True
                p_exit_tick = i
                continue

        # --- Entry logic (only when flat) ---
        if pos == 0 and p_side == 0:
            if use_vpin and vpin_regime[i] == 2:
                n_vpin_blocks += 1
            elif ov > entry_thr:
                p_side = 1
                p_price = bb
                p_tick = i
            elif ov < -entry_thr:
                p_side = -1
                p_price = ba
                p_tick = i

        # --- Equity sample ---
        if i % si == 0:
            unreal = pos * (mi - entry_px) * pv if pos != 0 else 0
            eq = rpnl + unreal
            eq_samples.append(eq)
            if eq > peak_eq:
                peak_eq = eq
            dd = peak_eq - eq
            if dd > max_dd:
                max_dd = dd

    # Final flatten
    if pos != 0:
        mi = int(mid_a[n - 1])
        pnl_pts = (mi - entry_px) * pos
        pnl_ntd = pnl_pts * pv - rc * abs(pos)
        rpnl += pnl_ntd
        rt_pnls.append(pnl_ntd)
        rt_holds.append(n - 1 - entry_tick)

    return _build_metrics(rpnl, rt_pnls, rt_holds, eq_samples,
                          max_dd, n_entries, n_exits, n_time_exits,
                          n_vpin_blocks, n)


def _sim_core_composite(
    composite: np.ndarray,
    mid: np.ndarray,
    best_bid: np.ndarray,
    best_ask: np.ndarray,
    entry_thr: float,
    exit_thr: float,
    max_hold: int,
    max_pos: int,
    warmup: int,
) -> dict[str, Any]:
    """Composite signal simulation (Scenario D)."""
    n = len(composite)

    pos = 0
    rpnl = 0
    peak_eq = 0
    max_dd = 0
    n_entries = 0
    n_exits = 0
    n_time_exits = 0
    entry_tick = 0
    entry_px = 0

    p_side = 0
    p_price = 0
    p_tick = 0
    p_exit = False
    p_exit_tick = 0

    rt_pnls: list[int] = []
    rt_holds: list[int] = []
    eq_samples: list[int] = []

    pv = POINT_VALUE_NTD
    rc = RT_COST_NTD
    lat = LATENCY_TICKS
    si = SAMPLE_INTERVAL

    # Scale thresholds for z-scored signal
    z_entry = entry_thr * 0.5
    z_exit = exit_thr * 0.5

    for i in range(warmup, n):
        bb = int(best_bid[i])
        ba = int(best_ask[i])
        mi = int(mid[i])
        sig = float(composite[i])

        if p_exit:
            if i >= p_exit_tick + lat:
                pnl_pts = (mi - entry_px) * pos
                pnl_ntd = pnl_pts * pv - rc * abs(pos)
                rpnl += pnl_ntd
                rt_pnls.append(pnl_ntd)
                rt_holds.append(i - entry_tick)
                n_exits += abs(pos)
                pos = 0
                p_exit = False
                p_side = 0
            continue

        if p_side != 0 and i >= p_tick + lat:
            filled = False
            if p_side == 1 and ba <= p_price:
                filled = True
            elif p_side == -1 and bb >= p_price:
                filled = True
            if filled:
                if p_side == 1 and pos < max_pos:
                    pos += 1
                    entry_tick = i
                    entry_px = p_price
                    n_entries += 1
                elif p_side == -1 and pos > -max_pos:
                    pos -= 1
                    entry_tick = i
                    entry_px = p_price
                    n_entries += 1
            p_side = 0

        if pos != 0:
            held = i - entry_tick
            if held > max_hold:
                p_exit = True
                p_exit_tick = i
                n_time_exits += 1
                continue
            if pos > 0 and sig < -z_exit:
                p_exit = True
                p_exit_tick = i
                continue
            if pos < 0 and sig > z_exit:
                p_exit = True
                p_exit_tick = i
                continue

        if pos == 0 and p_side == 0:
            if sig > z_entry:
                p_side = 1
                p_price = bb
                p_tick = i
            elif sig < -z_entry:
                p_side = -1
                p_price = ba
                p_tick = i

        if i % si == 0:
            unreal = pos * (mi - entry_px) * pv if pos != 0 else 0
            eq = rpnl + unreal
            eq_samples.append(eq)
            if eq > peak_eq:
                peak_eq = eq
            dd = peak_eq - eq
            if dd > max_dd:
                max_dd = dd

    if pos != 0:
        mi = int(mid[n - 1])
        pnl_pts = (mi - entry_px) * pos
        pnl_ntd = pnl_pts * pv - rc * abs(pos)
        rpnl += pnl_ntd
        rt_pnls.append(pnl_ntd)
        rt_holds.append(n - 1 - entry_tick)

    return _build_metrics(rpnl, rt_pnls, rt_holds, eq_samples,
                          max_dd, n_entries, n_exits, n_time_exits, 0, n)


# ---------------------------------------------------------------------------
# Metrics builder
# ---------------------------------------------------------------------------

def _build_metrics(
    realized_pnl_ntd: int,
    rt_pnls: list[int],
    rt_holds: list[int],
    equity_samples: list[int],
    max_drawdown_ntd: int,
    n_entries: int,
    n_exits: int,
    n_time_exits: int,
    n_vpin_blocks: int,
    n_ticks: int,
) -> dict[str, Any]:
    """Build standardized metrics dict."""
    n_rt = len(rt_pnls)
    if n_rt > 0:
        wins = sum(1 for p in rt_pnls if p > 0)
        win_rate = wins / n_rt
        mean_pnl = sum(rt_pnls) / n_rt
        mean_hold = sum(rt_holds) / n_rt
    else:
        win_rate = 0.0
        mean_pnl = 0.0
        mean_hold = 0.0

    # Sharpe from equity samples
    sharpe = 0.0
    if len(equity_samples) > 10:
        eq = np.array(equity_samples, dtype=np.float64)
        rets = np.diff(eq)
        if len(rets) > 1:
            mu = float(np.mean(rets))
            std = float(np.std(rets))
            if std > _EPS:
                samples_per_day = (6.5 * 3600) / (SAMPLE_INTERVAL / 3.7)
                sharpe = (mu / std) * math.sqrt(samples_per_day * 252)

    result: dict[str, Any] = {
        "total_pnl_ntd": int(realized_pnl_ntd),
        "sharpe": round(sharpe, 4),
        "max_drawdown_ntd": int(max_drawdown_ntd),
        "n_round_trips": n_rt,
        "n_entries": n_entries,
        "n_exits": n_exits,
        "n_time_exits": n_time_exits,
        "win_rate": round(win_rate, 4),
        "mean_pnl_per_rt_ntd": round(mean_pnl, 2),
        "mean_hold_ticks": round(mean_hold, 2),
        "n_ticks": n_ticks,
    }
    if n_vpin_blocks > 0:
        result["n_vpin_blocks"] = n_vpin_blocks
    return result


# ---------------------------------------------------------------------------
# Scenario wrappers
# ---------------------------------------------------------------------------

def run_scenario_a(
    pc: dict[str, Any],
    entry_threshold: float,
    exit_threshold: float,
    max_hold_ticks: int,
    max_pos: int,
    ema_alpha: float,
) -> dict[str, Any]:
    """Scenario A: OFI single-side quoting."""
    r = _sim_core(
        pc["ofi_ema"][ema_alpha], pc["mid"], pc["best_bid"], pc["best_ask"],
        entry_threshold, exit_threshold, max_hold_ticks, max_pos,
    )
    r["scenario"] = "A_ofi_single_side"
    r["params"] = {
        "entry_threshold": entry_threshold, "exit_threshold": exit_threshold,
        "max_hold_ticks": max_hold_ticks, "max_pos": max_pos,
        "ema_alpha": ema_alpha,
    }
    return r


def run_scenario_b(
    pc: dict[str, Any],
    entry_threshold: float,
    exit_threshold: float,
    max_hold_ticks: int,
    max_pos: int,
    ema_alpha: float,
) -> dict[str, Any]:
    """Scenario B: OFI single-side + inventory decay."""
    r = _sim_core(
        pc["ofi_ema"][ema_alpha], pc["mid"], pc["best_bid"], pc["best_ask"],
        entry_threshold, exit_threshold, max_hold_ticks, max_pos,
        decay=True,
    )
    r["scenario"] = "B_ofi_inventory_decay"
    r["params"] = {
        "entry_threshold": entry_threshold, "exit_threshold": exit_threshold,
        "max_hold_ticks": max_hold_ticks, "max_pos": max_pos,
        "ema_alpha": ema_alpha,
    }
    return r


def run_scenario_c(
    pc: dict[str, Any],
    entry_threshold: float,
    exit_threshold: float,
    max_hold_ticks: int,
    max_pos: int,
    ema_alpha: float,
) -> dict[str, Any]:
    """Scenario C: OFI single-side + VPIN filter."""
    # Compute VPIN regime if not cached
    if "vpin_regime" not in pc:
        logger.info("computing_vpin_regime")
        t0 = time.monotonic()
        pc["vpin_regime"] = _compute_vpin_regime(
            pc["bid_px"], pc["ask_px"],
            pc["bid_qty"], pc["ask_qty"],
            pc["local_ts"],
        )
        logger.info("vpin_regime_done", elapsed_s=round(time.monotonic() - t0, 2))

    r = _sim_core(
        pc["ofi_ema"][ema_alpha], pc["mid"], pc["best_bid"], pc["best_ask"],
        entry_threshold, exit_threshold, max_hold_ticks, max_pos,
        vpin_regime=pc["vpin_regime"],
    )
    r["scenario"] = "C_ofi_vpin_filter"
    r["params"] = {
        "entry_threshold": entry_threshold, "exit_threshold": exit_threshold,
        "max_hold_ticks": max_hold_ticks, "max_pos": max_pos,
        "ema_alpha": ema_alpha,
    }
    return r


def run_scenario_d(
    pc: dict[str, Any],
    entry_threshold: float,
    exit_threshold: float,
    max_hold_ticks: int,
    max_pos: int,
    ema_alpha: float,
) -> dict[str, Any]:
    """Scenario D: Multi-alpha composite (OFI + imbalance + spread_change)."""
    n = pc["n"]
    z_window = 500

    # Compute composite if not cached for this ema_alpha
    cache_key = f"composite_{ema_alpha}"
    if cache_key not in pc:
        ofi_ema = pc["ofi_ema"][ema_alpha]
        imb_ema = _ema_array(pc["imbalance_raw"], ema_alpha)
        spd_ema = _ema_array(pc["spread_change"], ema_alpha)

        # Rolling z-score
        def _zscore(arr: np.ndarray) -> np.ndarray:
            z = np.zeros(n, dtype=np.float64)
            for ii in range(z_window, n):
                w = arr[ii - z_window + 1 : ii + 1]
                mu = float(np.mean(w))
                std = float(np.std(w))
                if std > _EPS:
                    z[ii] = (arr[ii] - mu) / std
            return z

        logger.info("computing_composite_zscore", ema_alpha=ema_alpha)
        t0 = time.monotonic()
        z_ofi = _zscore(ofi_ema)
        z_imb = _zscore(imb_ema)
        z_spd = _zscore(spd_ema)
        pc[cache_key] = 0.5 * z_ofi + 0.3 * z_imb - 0.2 * z_spd
        logger.info("composite_done", elapsed_s=round(time.monotonic() - t0, 2))

    warmup = max(z_window + 10, LATENCY_TICKS + 2)
    r = _sim_core_composite(
        pc[cache_key], pc["mid"], pc["best_bid"], pc["best_ask"],
        entry_threshold, exit_threshold, max_hold_ticks, max_pos, warmup,
    )
    r["scenario"] = "D_multi_alpha_composite"
    r["params"] = {
        "entry_threshold": entry_threshold, "exit_threshold": exit_threshold,
        "max_hold_ticks": max_hold_ticks, "max_pos": max_pos,
        "ema_alpha": ema_alpha,
        "composite_weights": "ofi=0.5, imb=0.3, spd=-0.2",
    }
    return r


# ---------------------------------------------------------------------------
# Sweep
# ---------------------------------------------------------------------------

SWEEP_GRID: dict[str, list[float | int]] = {
    "entry_threshold": [0.5, 1.0, 2.0, 3.0, 5.0],
    "exit_threshold": [0.3, 0.5, 1.0],
    "max_hold_ticks": [5, 10, 20, 50],
    "max_pos": [3, 5],
    "ema_alpha": [0.05, 0.1, 0.2],
}


def _run_sweep(pc: dict[str, Any]) -> list[dict[str, Any]]:
    """Run parameter sweep on Scenario A."""
    combos = [
        (et, ex, int(mh), int(mp), ea)
        for et, ex, mh, mp, ea in product(
            SWEEP_GRID["entry_threshold"],
            SWEEP_GRID["exit_threshold"],
            SWEEP_GRID["max_hold_ticks"],
            SWEEP_GRID["max_pos"],
            SWEEP_GRID["ema_alpha"],
        )
        if et > ex  # prune: entry must exceed exit
    ]

    logger.info("sweep_start", n_combinations=len(combos))
    t0 = time.monotonic()

    results: list[dict[str, Any]] = []
    for idx, (et, ex, mh, mp, ea) in enumerate(combos):
        r = run_scenario_a(pc, et, ex, mh, mp, ea)
        results.append(r)
        if (idx + 1) % 50 == 0:
            elapsed = time.monotonic() - t0
            logger.info("sweep_progress", done=idx + 1, total=len(combos),
                        elapsed_s=round(elapsed, 1))

    elapsed = time.monotonic() - t0
    logger.info("sweep_done", elapsed_s=round(elapsed, 2), n_results=len(results))

    results.sort(key=lambda x: x["sharpe"], reverse=True)
    return results


# ---------------------------------------------------------------------------
# Pretty print
# ---------------------------------------------------------------------------

def _print_table(results: list[dict[str, Any]], title: str) -> None:
    """Print a formatted comparison table."""
    print(f"\n{'=' * 100}")
    print(f"  {title}")
    print(f"{'=' * 100}")
    header = (
        f"{'Scenario':<30s} {'PnL(NTD)':>10s} {'Sharpe':>8s} "
        f"{'MaxDD':>10s} {'#RTs':>6s} {'WinRate':>8s} "
        f"{'PnL/RT':>8s} {'HoldT':>7s}"
    )
    print(header)
    print("-" * 100)
    for r in results:
        scn = r.get("scenario", "?")
        line = (
            f"{scn:<30s} "
            f"{r['total_pnl_ntd']:>10,d} "
            f"{r['sharpe']:>8.2f} "
            f"{r['max_drawdown_ntd']:>10,d} "
            f"{r['n_round_trips']:>6d} "
            f"{r['win_rate']:>7.1%} "
            f"{r['mean_pnl_per_rt_ntd']:>8.1f} "
            f"{r['mean_hold_ticks']:>7.1f}"
        )
        print(line)
    print("-" * 100)


def _print_sweep_top(results: list[dict[str, Any]], top_n: int = 10) -> None:
    """Print top N sweep results."""
    print(f"\n{'=' * 130}")
    print(f"  Top {top_n} Parameter Sweep Results (Scenario A)")
    print(f"{'=' * 130}")
    header = (
        f"{'#':>3s} {'entry_thr':>10s} {'exit_thr':>9s} {'hold':>5s} "
        f"{'pos':>4s} {'ema_a':>6s} {'PnL(NTD)':>10s} {'Sharpe':>8s} "
        f"{'MaxDD':>10s} {'#RTs':>6s} {'WinRate':>8s} {'PnL/RT':>8s}"
    )
    print(header)
    print("-" * 130)
    for i, r in enumerate(results[:top_n]):
        p = r["params"]
        line = (
            f"{i + 1:>3d} "
            f"{p['entry_threshold']:>10.1f} "
            f"{p['exit_threshold']:>9.1f} "
            f"{p['max_hold_ticks']:>5d} "
            f"{p['max_pos']:>4d} "
            f"{p['ema_alpha']:>6.2f} "
            f"{r['total_pnl_ntd']:>10,d} "
            f"{r['sharpe']:>8.2f} "
            f"{r['max_drawdown_ntd']:>10,d} "
            f"{r['n_round_trips']:>6d} "
            f"{r['win_rate']:>7.1%} "
            f"{r['mean_pnl_per_rt_ntd']:>8.1f}"
        )
        print(line)
    print("-" * 130)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    logger.info("loading_data", path=str(_DEFAULT_DATA))
    if not _DEFAULT_DATA.exists():
        logger.error("data_not_found", path=str(_DEFAULT_DATA))
        sys.exit(1)

    data = np.load(str(_DEFAULT_DATA))
    logger.info("data_loaded", rows=len(data))

    all_ema_alphas = sorted(set(SWEEP_GRID["ema_alpha"]))
    pc = _precompute(data, all_ema_alphas)

    # --- Default params for scenario comparison ---
    dp: dict[str, Any] = {
        "entry_threshold": 2.0,
        "exit_threshold": 0.5,
        "max_hold_ticks": 20,
        "max_pos": 5,
        "ema_alpha": 0.1,
    }

    logger.info("running_scenarios", params=dp)

    scenario_results: list[dict[str, Any]] = []

    r_a = run_scenario_a(pc, **dp)
    scenario_results.append(r_a)
    logger.info("scenario_a_done", pnl=r_a["total_pnl_ntd"], sharpe=r_a["sharpe"])

    r_b = run_scenario_b(pc, **dp)
    scenario_results.append(r_b)
    logger.info("scenario_b_done", pnl=r_b["total_pnl_ntd"], sharpe=r_b["sharpe"])

    r_c = run_scenario_c(pc, **dp)
    scenario_results.append(r_c)
    logger.info("scenario_c_done", pnl=r_c["total_pnl_ntd"], sharpe=r_c["sharpe"])

    r_d = run_scenario_d(pc, **dp)
    scenario_results.append(r_d)
    logger.info("scenario_d_done", pnl=r_d["total_pnl_ntd"], sharpe=r_d["sharpe"])

    _print_table(scenario_results, "P2-lite Scenario Comparison (default params)")

    # --- Parameter sweep on Scenario A ---
    sweep_results = _run_sweep(pc)

    _print_sweep_top(sweep_results, top_n=10)

    # Bottom 5
    print(f"\n  Bottom 5 (worst Sharpe):")
    print("-" * 130)
    for r in sweep_results[-5:]:
        p = r["params"]
        line = (
            f"{'':>3s} "
            f"{p['entry_threshold']:>10.1f} "
            f"{p['exit_threshold']:>9.1f} "
            f"{p['max_hold_ticks']:>5d} "
            f"{p['max_pos']:>4d} "
            f"{p['ema_alpha']:>6.2f} "
            f"{r['total_pnl_ntd']:>10,d} "
            f"{r['sharpe']:>8.2f} "
            f"{r['max_drawdown_ntd']:>10,d} "
            f"{r['n_round_trips']:>6d} "
            f"{r['win_rate']:>7.1%} "
            f"{r['mean_pnl_per_rt_ntd']:>8.1f}"
        )
        print(line)
    print("-" * 130)

    # --- Save results ---
    output = {
        "backtest": "P2-lite: Alpha-Driven Selective Quoting",
        "data": "TXFD6_all_l1.npy (1.78M rows, 4 days)",
        "constants": {
            "tick_size_points": TICK_SIZE_POINTS,
            "point_value_ntd": POINT_VALUE_NTD,
            "rt_cost_ntd": RT_COST_NTD,
            "latency_ticks": LATENCY_TICKS,
        },
        "default_params": dp,
        "scenario_comparison": scenario_results,
        "sweep_grid": {k: [float(v) if isinstance(v, (int, float)) else v for v in vs]
                       for k, vs in SWEEP_GRID.items()},
        "sweep_n_combinations": len(sweep_results),
        "sweep_top_10": sweep_results[:10],
        "sweep_bottom_5": sweep_results[-5:],
    }

    _OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(_OUT_PATH, "w") as f:
        json.dump(output, f, indent=2, default=str)

    logger.info("results_saved", path=str(_OUT_PATH))
    print(f"\nResults saved to: {_OUT_PATH}")


if __name__ == "__main__":
    main()
