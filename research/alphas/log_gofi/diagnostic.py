"""Log-GOFI Gate Zero Diagnostic -- IC comparison: raw OFI vs log-GOFI.

Standalone script:
    python -m research.alphas.log_gofi.diagnostic

Loads TXFD6/TMFD6 L1 .npy files from research/data/raw/, computes:
  1. Raw OFI (price-level-aware L1 depth changes)
  2. Log-GOFI: log(1 + |OFI|) * sign(OFI)
  3. EMA-smoothed variants of both (alpha=0.125, ~8-tick halflife)
  4. Spearman rank IC at 30s / 60s / 300s forward return horizons
  5. Improvement ratio: log_gofi_IC / raw_ofi_IC per horizon

Kill gate: if log_gofi_IC <= raw_ofi_IC at ALL horizons, no value added.

Data format (from ch_batch_export L1):
  Structured numpy array with fields:
    bid_px, ask_px, bid_qty, ask_qty, mid_price, spread_bps, volume, local_ts
  Prices are float64 (already unscaled). local_ts is int64 nanoseconds.
"""

from __future__ import annotations

import math
import sys
from pathlib import Path
from typing import Any

import numpy as np
import structlog

logger = structlog.get_logger("log_gofi.diagnostic")

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
DATA_ROOT = Path(__file__).resolve().parents[2] / "data" / "raw"
SYMBOLS = ("TXFD6", "TMFD6")
HORIZONS_SEC = (30, 60, 300)
EMA_ALPHA = 0.125  # ~8-tick halflife

# Nanosecond conversion
NS_PER_SEC = 1_000_000_000


# ---------------------------------------------------------------------------
# Core transforms
# ---------------------------------------------------------------------------
def compute_ofi_series(data: np.ndarray) -> np.ndarray:
    """Compute price-level-aware OFI from L1 depth changes.

    Returns float64 array of same length as data (first element is 0).
    """
    n = len(data)
    ofi = np.zeros(n, dtype=np.float64)

    bid_px = data["bid_px"]
    ask_px = data["ask_px"]
    bid_qty = data["bid_qty"]
    ask_qty = data["ask_qty"]

    for i in range(1, n):
        # Bid flow
        if bid_px[i] == bid_px[i - 1]:
            b_flow = bid_qty[i] - bid_qty[i - 1]
        elif bid_px[i] > bid_px[i - 1]:
            b_flow = bid_qty[i]
        else:
            b_flow = -bid_qty[i - 1]

        # Ask flow (CKS 2014 convention — matches FeatureEngine._compute_ofi_l1_raw)
        if ask_px[i] > ask_px[i - 1]:
            a_flow = -ask_qty[i - 1]
        elif ask_px[i] == ask_px[i - 1]:
            a_flow = ask_qty[i] - ask_qty[i - 1]
        else:
            a_flow = ask_qty[i]

        ofi[i] = b_flow - a_flow

    return ofi


def apply_log_gofi(ofi: np.ndarray) -> np.ndarray:
    """Apply log(1 + |x|) * sign(x) element-wise."""
    abs_ofi = np.abs(ofi)
    log_abs = np.log1p(abs_ofi)
    return log_abs * np.sign(ofi)


def apply_ema(arr: np.ndarray, alpha: float) -> np.ndarray:
    """Exponential moving average (forward pass)."""
    n = len(arr)
    out = np.zeros(n, dtype=np.float64)
    if n == 0:
        return out
    out[0] = arr[0]
    complement = 1.0 - alpha
    for i in range(1, n):
        out[i] = alpha * arr[i] + complement * out[i - 1]
    return out


def compute_forward_returns(
    mid_price: np.ndarray,
    local_ts: np.ndarray,
    horizon_sec: int,
) -> np.ndarray:
    """Compute forward mid-price returns at a given horizon.

    Uses binary search to find the index closest to t + horizon_ns.
    Returns NaN where forward data is unavailable.
    """
    n = len(mid_price)
    horizon_ns = horizon_sec * NS_PER_SEC
    fwd_ret = np.full(n, np.nan, dtype=np.float64)

    for i in range(n):
        target_ts = local_ts[i] + horizon_ns
        # Binary search for first index >= target_ts
        j = np.searchsorted(local_ts, target_ts, side="left")
        if j < n and mid_price[i] != 0.0:
            fwd_ret[i] = (mid_price[j] - mid_price[i]) / mid_price[i]

    return fwd_ret


def spearman_rank_ic(signal: np.ndarray, returns: np.ndarray) -> tuple[float, int]:
    """Compute Spearman rank IC between signal and forward returns.

    Returns (ic, n_valid).  Uses numpy-only implementation to avoid
    scipy dependency in research scripts.
    """
    mask = np.isfinite(signal) & np.isfinite(returns)
    n_valid = int(np.sum(mask))
    if n_valid < 30:
        return float("nan"), n_valid

    sig = signal[mask]
    ret = returns[mask]

    # Rank-transform
    sig_ranks = _rankdata(sig)
    ret_ranks = _rankdata(ret)

    # Pearson correlation of ranks
    sig_centered = sig_ranks - np.mean(sig_ranks)
    ret_centered = ret_ranks - np.mean(ret_ranks)
    denom = np.sqrt(np.sum(sig_centered**2) * np.sum(ret_centered**2))
    if denom == 0.0:
        return 0.0, n_valid
    ic = float(np.sum(sig_centered * ret_centered) / denom)
    return ic, n_valid


def _rankdata(arr: np.ndarray) -> np.ndarray:
    """Simple rank assignment (average ties)."""
    order = np.argsort(arr, kind="mergesort")
    ranks = np.empty_like(order, dtype=np.float64)
    ranks[order] = np.arange(1, len(arr) + 1, dtype=np.float64)
    return ranks


# ---------------------------------------------------------------------------
# Per-symbol diagnostic
# ---------------------------------------------------------------------------
def run_symbol_diagnostic(symbol: str) -> dict[str, Any]:  # noqa: C901
    """Run IC comparison for one symbol across all available dates."""
    symbol_dir = DATA_ROOT / symbol.lower()
    if not symbol_dir.exists():
        logger.warning("data_dir_not_found", symbol=symbol, path=str(symbol_dir))
        return {}

    npy_files = sorted(symbol_dir.glob(f"{symbol}_*_l1.npy"))
    if not npy_files:
        logger.warning("no_npy_files", symbol=symbol, path=str(symbol_dir))
        return {}

    logger.info("loading_data", symbol=symbol, n_files=len(npy_files))

    # Aggregate results per horizon
    results: dict[str, dict[str, list[float]]] = {}
    for h in HORIZONS_SEC:
        key = f"{h}s"
        results[key] = {
            "raw_ofi_ic": [],
            "log_gofi_ic": [],
            "raw_ema_ic": [],
            "log_ema_ic": [],
        }

    total_rows = 0

    for fpath in npy_files:
        day_label = fpath.stem  # e.g. TXFD6_2026-01-26_l1
        data = np.load(str(fpath), allow_pickle=False)
        n = len(data)
        if n < 100:
            logger.debug("skipping_short_day", file=day_label, rows=n)
            continue

        total_rows += n

        # Compute signals
        raw_ofi = compute_ofi_series(data)
        log_gofi = apply_log_gofi(raw_ofi)
        raw_ema = apply_ema(raw_ofi, EMA_ALPHA)
        log_ema = apply_ema(log_gofi, EMA_ALPHA)
        # EMA first then log transform (future variant, not measured in this pass)

        mid_price = data["mid_price"]
        local_ts = data["local_ts"]

        for h in HORIZONS_SEC:
            key = f"{h}s"
            fwd_ret = compute_forward_returns(mid_price, local_ts, h)

            ic_raw, _ = spearman_rank_ic(raw_ofi, fwd_ret)
            ic_log, _ = spearman_rank_ic(log_gofi, fwd_ret)
            ic_raw_ema, _ = spearman_rank_ic(raw_ema, fwd_ret)
            ic_log_ema, _ = spearman_rank_ic(log_ema, fwd_ret)

            if math.isfinite(ic_raw):
                results[key]["raw_ofi_ic"].append(ic_raw)
            if math.isfinite(ic_log):
                results[key]["log_gofi_ic"].append(ic_log)
            if math.isfinite(ic_raw_ema):
                results[key]["raw_ema_ic"].append(ic_raw_ema)
            if math.isfinite(ic_log_ema):
                results[key]["log_ema_ic"].append(ic_log_ema)

        logger.debug("processed_day", file=day_label, rows=n)

    # Summarize
    summary: dict[str, Any] = {
        "symbol": symbol,
        "n_files": len(npy_files),
        "total_rows": total_rows,
        "horizons": {},
    }

    for h in HORIZONS_SEC:
        key = f"{h}s"
        horizon_data: dict[str, Any] = {}
        for sig_name in ("raw_ofi_ic", "log_gofi_ic", "raw_ema_ic", "log_ema_ic"):
            values = results[key][sig_name]
            if values:
                horizon_data[sig_name] = {
                    "mean": float(np.mean(values)),
                    "median": float(np.median(values)),
                    "std": float(np.std(values)),
                    "n_days": len(values),
                }
            else:
                horizon_data[sig_name] = {"mean": float("nan"), "n_days": 0}

        # Improvement ratios
        raw_mean = horizon_data["raw_ofi_ic"].get("mean", float("nan"))
        log_mean = horizon_data["log_gofi_ic"].get("mean", float("nan"))
        raw_ema_mean = horizon_data["raw_ema_ic"].get("mean", float("nan"))
        log_ema_mean = horizon_data["log_ema_ic"].get("mean", float("nan"))

        if math.isfinite(raw_mean) and abs(raw_mean) > 1e-10:
            horizon_data["improvement_ratio_raw"] = log_mean / raw_mean
        else:
            horizon_data["improvement_ratio_raw"] = float("nan")

        if math.isfinite(raw_ema_mean) and abs(raw_ema_mean) > 1e-10:
            horizon_data["improvement_ratio_ema"] = log_ema_mean / raw_ema_mean
        else:
            horizon_data["improvement_ratio_ema"] = float("nan")

        summary["horizons"][key] = horizon_data

    return summary


# ---------------------------------------------------------------------------
# Kill gate evaluation
# ---------------------------------------------------------------------------
def evaluate_kill_gate(results: dict[str, Any]) -> bool:
    """Return True if log_gofi adds value at ANY horizon for ANY symbol.

    Kill gate: if abs(log_gofi_IC) <= abs(raw_ofi_IC) at ALL horizons, no value.
    Uses absolute IC comparison to handle negative ICs correctly.
    """
    for _symbol, summary in results.items():
        horizons = summary.get("horizons", {})
        for _h_key, h_data in horizons.items():
            raw_ic = h_data.get("raw_ofi_ic", {}).get("mean", float("nan"))
            log_ic = h_data.get("log_gofi_ic", {}).get("mean", float("nan"))
            if math.isfinite(raw_ic) and math.isfinite(log_ic):
                if abs(log_ic) > abs(raw_ic):
                    return True  # log_gofi has stronger IC at this horizon
            raw_ema_ic = h_data.get("raw_ema_ic", {}).get("mean", float("nan"))
            log_ema_ic = h_data.get("log_ema_ic", {}).get("mean", float("nan"))
            if math.isfinite(raw_ema_ic) and math.isfinite(log_ema_ic):
                if abs(log_ema_ic) > abs(raw_ema_ic):
                    return True  # log_ema has stronger IC
    return False


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def main() -> None:
    """Run Gate Zero diagnostic for log_gofi."""
    structlog.configure(
        wrapper_class=structlog.make_filtering_bound_logger(20),  # INFO
    )

    logger.info("log_gofi_diagnostic_start", symbols=SYMBOLS, horizons=HORIZONS_SEC)

    all_results: dict[str, Any] = {}
    for symbol in SYMBOLS:
        summary = run_symbol_diagnostic(symbol)
        if summary:
            all_results[symbol] = summary

    if not all_results:
        logger.error("no_data_processed")
        sys.exit(1)

    # Report
    logger.info("=" * 60)
    logger.info("LOG-GOFI GATE ZERO DIAGNOSTIC RESULTS")
    logger.info("=" * 60)

    for symbol, summary in all_results.items():
        logger.info(
            "symbol_summary",
            symbol=symbol,
            n_files=summary["n_files"],
            total_rows=summary["total_rows"],
        )
        for h_key, h_data in summary["horizons"].items():
            raw_ic = h_data["raw_ofi_ic"].get("mean", float("nan"))
            log_ic = h_data["log_gofi_ic"].get("mean", float("nan"))
            raw_ema_ic = h_data["raw_ema_ic"].get("mean", float("nan"))
            log_ema_ic = h_data["log_ema_ic"].get("mean", float("nan"))
            ratio_raw = h_data.get("improvement_ratio_raw", float("nan"))
            ratio_ema = h_data.get("improvement_ratio_ema", float("nan"))

            logger.info(
                "horizon_ic",
                symbol=symbol,
                horizon=h_key,
                raw_ofi_ic=f"{raw_ic:.6f}",
                log_gofi_ic=f"{log_ic:.6f}",
                improvement_raw=f"{ratio_raw:.3f}",
                raw_ema_ic=f"{raw_ema_ic:.6f}",
                log_ema_ic=f"{log_ema_ic:.6f}",
                improvement_ema=f"{ratio_ema:.3f}",
            )

    # Kill gate
    passes = evaluate_kill_gate(all_results)
    if passes:
        logger.info(
            "KILL_GATE_PASS",
            verdict="log_gofi improves IC at one or more horizons",
        )
    else:
        logger.warning(
            "KILL_GATE_FAIL",
            verdict="log_gofi does NOT improve IC at any horizon -- no value added",
        )

    sys.exit(0 if passes else 1)


if __name__ == "__main__":
    main()
