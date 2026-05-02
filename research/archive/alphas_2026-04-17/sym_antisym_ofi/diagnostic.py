"""Gate Zero diagnostic: Symmetric / Antisymmetric OFI decomposition.

Computes sym (b_flow + a_flow) and antisym (b_flow - a_flow) OFI from L1
depth changes, then measures:

1. IC (Spearman rank correlation) with future returns at 30s/60s/300s horizons.
2. Comparison: sym IC vs antisym IC vs raw OFI IC.
3. Orthogonality: correlation(sym, antisym) — target < 0.3.
4. Kill gate: IC < 0.02 at 60s for BOTH components => KILL.

Data source: L1 .npy files from ``research/data/raw/{txfd6,tmfd6}/``.
These are structured arrays with fields:
    (bid_px, ask_px, bid_qty, ask_qty, mid_price, spread_bps, volume, local_ts)

Usage::

    python -m research.alphas.sym_antisym_ofi.diagnostic
    python -m research.alphas.sym_antisym_ofi.diagnostic --symbols TXFD6
    python -m research.alphas.sym_antisym_ofi.diagnostic --symbols TMFD6 --horizons 30,60

Offline research script — float permitted per Architecture Rule 11.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Any

import numpy as np
import structlog
from scipy import stats as scipy_stats

logger = structlog.get_logger("sym_antisym_ofi.diagnostic")

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
BASE_DIR = Path(__file__).resolve().parent.parent.parent.parent
DATA_DIR = BASE_DIR / "research" / "data" / "raw"

DEFAULT_SYMBOLS: tuple[str, ...] = ("TXFD6", "TMFD6")
DEFAULT_HORIZONS_S: tuple[int, ...] = (30, 60, 300)
RESAMPLE_FREQ_S: int = 1  # 1-second bars for IC measurement

# Taiwan day session in UTC: 09:00-13:30 TWN = 01:00-05:30 UTC
DAY_START_UTC_H: int = 1
DAY_START_UTC_M: int = 0
DAY_END_UTC_H: int = 5
DAY_END_UTC_M: int = 30

# Kill gate threshold
IC_KILL_THRESHOLD: float = 0.02

# EMA smoothing for signal
EMA_ALPHA_8: float = 1.0 - np.exp(-1.0 / 8.0)


# ---------------------------------------------------------------------------
# OFI computation (vectorized)
# ---------------------------------------------------------------------------


def compute_b_flow(
    best_bid: np.ndarray,
    bid_qty: np.ndarray,
    prev_best_bid: np.ndarray,
    prev_bid_qty: np.ndarray,
) -> np.ndarray:
    """Vectorized bid-side flow per CKS 2014 definition.

    All inputs are 1-D arrays of the same length.
    Returns b_flow array.
    """
    n = len(best_bid)
    b_flow = np.zeros(n, dtype=np.float64)

    up = best_bid > prev_best_bid
    eq = best_bid == prev_best_bid
    dn = ~up & ~eq

    b_flow[up] = bid_qty[up]
    b_flow[eq] = bid_qty[eq] - prev_bid_qty[eq]
    b_flow[dn] = -prev_bid_qty[dn]

    return b_flow


def compute_a_flow(
    best_ask: np.ndarray,
    ask_qty: np.ndarray,
    prev_best_ask: np.ndarray,
    prev_ask_qty: np.ndarray,
) -> np.ndarray:
    """Vectorized ask-side flow per CKS 2014 definition.

    All inputs are 1-D arrays of the same length.
    Returns a_flow array.
    """
    n = len(best_ask)
    a_flow = np.zeros(n, dtype=np.float64)

    up = best_ask > prev_best_ask
    eq = best_ask == prev_best_ask
    dn = ~up & ~eq

    a_flow[up] = -prev_ask_qty[up]
    a_flow[eq] = ask_qty[eq] - prev_ask_qty[eq]
    a_flow[dn] = ask_qty[dn]

    return a_flow


def ema_vectorized(values: np.ndarray, alpha: float) -> np.ndarray:
    """Compute EMA over a 1-D array.  O(N) single pass.

    Uses the recurrence: ema[t] = alpha * x[t] + (1-alpha) * ema[t-1].
    """
    n = len(values)
    out = np.empty(n, dtype=np.float64)
    if n == 0:
        return out
    out[0] = values[0]
    complement = 1.0 - alpha
    for i in range(1, n):
        out[i] = alpha * values[i] + complement * out[i - 1]
    return out


# ---------------------------------------------------------------------------
# Data loading
# ---------------------------------------------------------------------------


def discover_dates(symbol_dir: Path, symbol: str) -> list[str]:
    """Find all available date strings for a symbol's L1 data."""
    prefix = f"{symbol}_"
    suffix = "_l1.npy"
    dates: list[str] = []
    for f in sorted(symbol_dir.iterdir()):
        name = f.name
        if name.startswith(prefix) and name.endswith(suffix):
            date_str = name[len(prefix) : -len(suffix)]
            dates.append(date_str)
    return dates


def load_l1_day(symbol_dir: Path, symbol: str, date_str: str) -> np.ndarray | None:
    """Load a single day's L1 .npy file.

    Returns structured array with fields:
        (bid_px, ask_px, bid_qty, ask_qty, mid_price, spread_bps, volume, local_ts)
    or None if file does not exist.
    """
    fname = symbol_dir / f"{symbol}_{date_str}_l1.npy"
    if not fname.exists():
        logger.warning("file_not_found", path=str(fname))
        return None
    return np.load(fname)


def filter_day_session_mask(local_ts_ns: np.ndarray, date_str: str) -> np.ndarray:
    """Return boolean mask for Taiwan day session (09:00-13:30 TWN).

    local_ts_ns is int64 nanosecond timestamps.
    """
    import datetime as dt

    base_date = dt.datetime.strptime(date_str, "%Y-%m-%d").replace(
        tzinfo=dt.timezone.utc
    )
    start_ns = int(
        base_date.replace(
            hour=DAY_START_UTC_H, minute=DAY_START_UTC_M, second=0
        ).timestamp()
        * 1_000_000_000
    )
    end_ns = int(
        base_date.replace(
            hour=DAY_END_UTC_H, minute=DAY_END_UTC_M, second=0
        ).timestamp()
        * 1_000_000_000
    )
    return (local_ts_ns >= start_ns) & (local_ts_ns <= end_ns)


def resample_to_1s_bars(
    data: np.ndarray,
) -> dict[str, np.ndarray]:
    """Resample tick-level L1 data to 1-second bars (last observation).

    Returns dict with keys: bid_px, ask_px, bid_qty, ask_qty, mid_price,
    local_ts, bar_idx (for alignment).
    """
    ts_ns = data["local_ts"]
    if len(ts_ns) == 0:
        return {
            "bid_px": np.array([], dtype=np.float64),
            "ask_px": np.array([], dtype=np.float64),
            "bid_qty": np.array([], dtype=np.float64),
            "ask_qty": np.array([], dtype=np.float64),
            "mid_price": np.array([], dtype=np.float64),
            "local_ts": np.array([], dtype=np.int64),
        }

    # Assign each tick to a 1-second bin
    ts_s = ts_ns // 1_000_000_000
    unique_s, inverse = np.unique(ts_s, return_inverse=True)
    n_bars = len(unique_s)

    # For each bar, take the last tick
    last_idx = np.empty(n_bars, dtype=np.int64)
    for i in range(len(inverse)):
        last_idx[inverse[i]] = i

    return {
        "bid_px": data["bid_px"][last_idx],
        "ask_px": data["ask_px"][last_idx],
        "bid_qty": data["bid_qty"][last_idx],
        "ask_qty": data["ask_qty"][last_idx],
        "mid_price": data["mid_price"][last_idx],
        "local_ts": ts_ns[last_idx],
    }


# ---------------------------------------------------------------------------
# IC computation
# ---------------------------------------------------------------------------


def compute_forward_returns(mid_price: np.ndarray, horizon_bars: int) -> np.ndarray:
    """Compute forward log-returns at given horizon (in 1s bars).

    Returns array of same length with NaN at the tail.
    """
    n = len(mid_price)
    fwd_ret = np.full(n, np.nan, dtype=np.float64)
    if horizon_bars >= n:
        return fwd_ret
    valid = mid_price[:-horizon_bars] > 0
    future = mid_price[horizon_bars:]
    mask = valid & (future > 0)
    indices = np.where(mask)[0]
    fwd_ret[indices] = np.log(future[indices] / mid_price[indices])
    return fwd_ret


def rank_ic(signal: np.ndarray, forward_ret: np.ndarray) -> tuple[float, float]:
    """Compute Spearman rank IC between signal and forward returns.

    Returns (ic, p_value).  Drops NaN entries from both arrays.
    """
    valid = np.isfinite(signal) & np.isfinite(forward_ret)
    s = signal[valid]
    r = forward_ret[valid]
    if len(s) < 30:
        return 0.0, 1.0
    corr, pval = scipy_stats.spearmanr(s, r)
    return float(corr), float(pval)


def detrended_ic(
    signal: np.ndarray, forward_ret: np.ndarray, detrend_window: int = 300
) -> tuple[float, float]:
    """Compute detrended Spearman rank IC (mandatory per R18 feedback).

    Removes local trend (rolling mean over detrend_window bars) from signal
    before measuring IC.  This catches EMA-smoothed signals that merely
    track price direction rather than predicting it.

    Parameters
    ----------
    signal : np.ndarray
        Signal values (1-D).
    forward_ret : np.ndarray
        Forward return values (1-D, may contain NaN).
    detrend_window : int
        Window for local trend removal in bars (default 300 = 5 min at 1s).

    Returns (ic, p_value).
    """
    n = len(signal)
    if n < detrend_window + 30:
        return 0.0, 1.0
    # Rolling mean for detrending
    cumsum = np.cumsum(np.nan_to_num(signal, nan=0.0))
    rolling_mean = np.full(n, np.nan, dtype=np.float64)
    rolling_mean[detrend_window:] = (
        cumsum[detrend_window:] - cumsum[:-detrend_window]
    ) / detrend_window
    detrended = signal - rolling_mean
    return rank_ic(detrended, forward_ret)


# ---------------------------------------------------------------------------
# Main diagnostic
# ---------------------------------------------------------------------------


def run_diagnostic(
    symbols: tuple[str, ...] = DEFAULT_SYMBOLS,
    horizons_s: tuple[int, ...] = DEFAULT_HORIZONS_S,
) -> dict[str, Any]:
    """Run Gate Zero diagnostic for sym/antisym OFI decomposition.

    Returns results dict with IC tables and orthogonality metrics.
    """
    results: dict[str, Any] = {}

    for symbol in symbols:
        symbol_upper = symbol.upper()
        symbol_lower = symbol.lower()
        symbol_dir = DATA_DIR / symbol_lower
        if not symbol_dir.exists():
            logger.warning("symbol_dir_not_found", symbol=symbol, path=str(symbol_dir))
            continue

        dates = discover_dates(symbol_dir, symbol_upper)
        if not dates:
            logger.warning("no_data_files", symbol=symbol)
            continue

        logger.info("processing_symbol", symbol=symbol, n_dates=len(dates))

        # Accumulate per-day signals and returns for pooled IC
        all_sym_signals: list[np.ndarray] = []
        all_antisym_signals: list[np.ndarray] = []
        all_raw_ofi_signals: list[np.ndarray] = []
        all_fwd_returns: dict[int, list[np.ndarray]] = {h: [] for h in horizons_s}

        for date_str in dates:
            raw = load_l1_day(symbol_dir, symbol_upper, date_str)
            if raw is None or len(raw) < 100:
                continue

            # Filter to day session
            mask = filter_day_session_mask(raw["local_ts"], date_str)
            day_data = raw[mask]
            if len(day_data) < 100:
                logger.debug(
                    "day_session_too_short", date=date_str, n_ticks=len(day_data)
                )
                continue

            # Resample to 1s bars
            bars = resample_to_1s_bars(day_data)
            n_bars = len(bars["mid_price"])
            if n_bars < 60:
                continue

            # Compute b_flow and a_flow from 1s-bar last observations
            bid_px = bars["bid_px"]
            ask_px = bars["ask_px"]
            bid_qty = bars["bid_qty"]
            ask_qty = bars["ask_qty"]

            b_flow = compute_b_flow(
                best_bid=bid_px[1:],
                bid_qty=bid_qty[1:],
                prev_best_bid=bid_px[:-1],
                prev_bid_qty=bid_qty[:-1],
            )
            a_flow = compute_a_flow(
                best_ask=ask_px[1:],
                ask_qty=ask_qty[1:],
                prev_best_ask=ask_px[:-1],
                prev_ask_qty=ask_qty[:-1],
            )

            # Decomposition
            sym_raw = b_flow + a_flow  # symmetric: same-direction depth change
            antisym_raw = b_flow - a_flow  # antisymmetric: standard OFI
            # EMA smoothing
            sym_ema = ema_vectorized(sym_raw, EMA_ALPHA_8)
            antisym_ema = ema_vectorized(antisym_raw, EMA_ALPHA_8)

            # Align: signals are for bars[1:], mid_price for forward returns
            mid_price_aligned = bars["mid_price"][1:]

            all_sym_signals.append(sym_ema)
            all_antisym_signals.append(antisym_ema)
            all_raw_ofi_signals.append(antisym_ema)  # raw OFI = antisymmetric

            for h in horizons_s:
                fwd_ret = compute_forward_returns(mid_price_aligned, h)
                all_fwd_returns[h].append(fwd_ret)

        if not all_sym_signals:
            logger.warning("no_valid_days", symbol=symbol)
            results[symbol] = {"status": "NO_DATA"}
            continue

        # Pool across days
        sym_pooled = np.concatenate(all_sym_signals)
        antisym_pooled = np.concatenate(all_antisym_signals)
        raw_ofi_pooled = np.concatenate(all_raw_ofi_signals)

        # --- Orthogonality: correlation(sym, antisym) ---
        valid_both = np.isfinite(sym_pooled) & np.isfinite(antisym_pooled)
        if valid_both.sum() > 30:
            ortho_corr, ortho_pval = scipy_stats.spearmanr(
                sym_pooled[valid_both], antisym_pooled[valid_both]
            )
        else:
            ortho_corr, ortho_pval = 0.0, 1.0

        logger.info(
            "orthogonality",
            symbol=symbol,
            corr_sym_antisym=round(float(ortho_corr), 4),
            p_value=round(float(ortho_pval), 4),
            n_obs=int(valid_both.sum()),
        )

        # --- IC at each horizon ---
        ic_table: dict[int, dict[str, tuple[float, float]]] = {}
        for h in horizons_s:
            fwd_pooled = np.concatenate(all_fwd_returns[h])

            sym_ic, sym_p = rank_ic(sym_pooled, fwd_pooled)
            antisym_ic, antisym_p = rank_ic(antisym_pooled, fwd_pooled)
            raw_ic, raw_p = rank_ic(raw_ofi_pooled, fwd_pooled)

            # Detrended IC (mandatory per R18 feedback — catches trend contamination)
            sym_dic, sym_dp = detrended_ic(sym_pooled, fwd_pooled)
            antisym_dic, antisym_dp = detrended_ic(antisym_pooled, fwd_pooled)
            raw_dic, raw_dp = detrended_ic(raw_ofi_pooled, fwd_pooled)

            ic_table[h] = {
                "sym": (sym_ic, sym_p),
                "antisym": (antisym_ic, antisym_p),
                "raw_ofi": (raw_ic, raw_p),
                "sym_detrended": (sym_dic, sym_dp),
                "antisym_detrended": (antisym_dic, antisym_dp),
                "raw_ofi_detrended": (raw_dic, raw_dp),
            }

            logger.info(
                "ic_result",
                symbol=symbol,
                horizon_s=h,
                sym_ic=round(sym_ic, 4),
                sym_p=round(sym_p, 4),
                antisym_ic=round(antisym_ic, 4),
                antisym_p=round(antisym_p, 4),
                raw_ofi_ic=round(raw_ic, 4),
                raw_ofi_p=round(raw_p, 4),
                sym_detrended_ic=round(sym_dic, 4),
                antisym_detrended_ic=round(antisym_dic, 4),
                raw_ofi_detrended_ic=round(raw_dic, 4),
            )

        # --- Kill gate evaluation ---
        ic_60s = ic_table.get(60)
        if ic_60s is not None:
            sym_ic_60 = abs(ic_60s["sym"][0])
            antisym_ic_60 = abs(ic_60s["antisym"][0])
            both_below_threshold = (
                sym_ic_60 < IC_KILL_THRESHOLD and antisym_ic_60 < IC_KILL_THRESHOLD
            )
            gate_result = "KILL" if both_below_threshold else "PASS"
        else:
            # No 60s horizon requested — evaluate at first available
            gate_result = "INCONCLUSIVE"
            sym_ic_60 = 0.0
            antisym_ic_60 = 0.0

        logger.info(
            "kill_gate",
            symbol=symbol,
            gate_result=gate_result,
            sym_ic_60s=round(sym_ic_60, 4),
            antisym_ic_60s=round(antisym_ic_60, 4),
            threshold=IC_KILL_THRESHOLD,
        )

        results[symbol] = {
            "n_days": len(all_sym_signals),
            "n_obs_pooled": len(sym_pooled),
            "orthogonality": {
                "corr_sym_antisym": round(float(ortho_corr), 4),
                "p_value": round(float(ortho_pval), 4),
            },
            "ic_table": {
                h: {
                    comp: {"ic": round(vals[0], 4), "p": round(vals[1], 4)}
                    for comp, vals in comps.items()
                }
                for h, comps in ic_table.items()
            },
            "kill_gate": gate_result,
        }

    # --- Summary ---
    logger.info("diagnostic_complete", results=results)
    return results


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Gate Zero diagnostic: Sym/Antisym OFI decomposition"
    )
    parser.add_argument(
        "--symbols",
        type=str,
        default=",".join(DEFAULT_SYMBOLS),
        help="Comma-separated symbol list (default: TXFD6,TMFD6)",
    )
    parser.add_argument(
        "--horizons",
        type=str,
        default=",".join(str(h) for h in DEFAULT_HORIZONS_S),
        help="Comma-separated forward-return horizons in seconds (default: 30,60,300)",
    )
    args = parser.parse_args()

    symbols = tuple(s.strip().upper() for s in args.symbols.split(","))
    horizons_s = tuple(int(h.strip()) for h in args.horizons.split(","))

    results = run_diagnostic(symbols=symbols, horizons_s=horizons_s)

    # Print summary table
    print("\n" + "=" * 72)
    print("SYM/ANTISYM OFI DECOMPOSITION — GATE ZERO DIAGNOSTIC")
    print("=" * 72)

    for symbol, res in results.items():
        if res.get("status") == "NO_DATA":
            print(f"\n{symbol}: NO DATA")
            continue

        print(f"\n--- {symbol} ({res['n_days']} days, {res['n_obs_pooled']} obs) ---")
        print(
            f"  Orthogonality: corr(sym, antisym) = "
            f"{res['orthogonality']['corr_sym_antisym']:.4f} "
            f"(p={res['orthogonality']['p_value']:.4f})"
        )

        print(f"\n  {'Horizon':>8s}  {'sym IC':>10s}  {'antisym IC':>12s}  {'raw OFI IC':>12s}")
        print(f"  {'------':>8s}  {'------':>10s}  {'----------':>12s}  {'----------':>12s}")
        for h, comps in res["ic_table"].items():
            sym_str = f"{comps['sym']['ic']:+.4f} (p={comps['sym']['p']:.3f})"
            anti_str = f"{comps['antisym']['ic']:+.4f} (p={comps['antisym']['p']:.3f})"
            raw_str = f"{comps['raw_ofi']['ic']:+.4f} (p={comps['raw_ofi']['p']:.3f})"
            print(f"  {h:>6d}s  {sym_str:>20s}  {anti_str:>20s}  {raw_str:>20s}")

        print(f"\n  Kill Gate (60s): {res['kill_gate']}")

    print("\n" + "=" * 72)

    # Exit with non-zero if all symbols killed
    all_killed = all(
        r.get("kill_gate") == "KILL" or r.get("status") == "NO_DATA"
        for r in results.values()
    )
    if all_killed:
        print("\nVERDICT: ALL SYMBOLS KILLED — direction is dead.")
        sys.exit(1)
    else:
        print("\nVERDICT: At least one symbol passed kill gate.")
        sys.exit(0)


if __name__ == "__main__":
    main()
