"""Large-scale OFI (Order Flow Imbalance) alpha exploration on real L1 data.

Vectorized computation of 15 OFI alpha signals across all symbols,
with forward-return IC, autocorrelation, and cross-symbol consistency metrics.

Usage::

    python research/tools/ofi_alpha_explorer.py \
        --data-dir research/data/raw \
        --out research/results/ofi_exploration.json
"""
from __future__ import annotations

import argparse
import json
import time
from pathlib import Path
from typing import Any

import numpy as np
from scipy.signal import lfilter
from structlog import get_logger

logger = get_logger("ofi_explorer")

# ---------------------------------------------------------------------------
# EMA constants (match existing codebase)
# ---------------------------------------------------------------------------
_EMA_ALPHA_4 = 0.2212
_EMA_ALPHA_8 = 0.1175
_EMA_ALPHA_16 = 0.0606
_EMA_ALPHA_32 = 0.0308
_EMA_ALPHA_64 = 0.0155

_EPS = 1e-8


def _ema(x: np.ndarray, alpha: float) -> np.ndarray:
    """Vectorized EMA using scipy.signal.lfilter. O(n)."""
    if len(x) == 0:
        return x.copy()
    b = np.array([alpha], dtype=np.float64)
    a = np.array([1.0, -(1.0 - alpha)], dtype=np.float64)
    zi = np.array([x[0] * (1.0 - alpha)], dtype=np.float64)
    out, _ = lfilter(b, a, x, zi=zi)
    return np.asarray(out, dtype=np.float64)


# ---------------------------------------------------------------------------
# Synthetic data generation (when no real data available)
# ---------------------------------------------------------------------------

def _generate_synthetic_l1(
    n: int = 100_000,
    seed: int = 42,
) -> np.ndarray:
    """Generate synthetic L1 data with realistic OFI dynamics.

    Uses OU process for mid-price, Hawkes-like volume clustering,
    and correlated bid/ask queue dynamics.
    """
    rng = np.random.default_rng(seed)

    # Mid-price: OU process
    mid = np.empty(n, dtype=np.float64)
    mid[0] = 100_000.0  # scaled x10000
    theta, mu, sigma = 0.001, 100_000.0, 5.0
    for i in range(1, n):
        mid[i] = mid[i - 1] + theta * (mu - mid[i - 1]) + sigma * rng.standard_normal()

    # Spread: mean-reverting with occasional widening
    spread_base = 10.0  # bps
    spread_noise = rng.exponential(2.0, n)
    spread = spread_base + spread_noise
    spread = np.maximum(spread, 1.0)

    # Bid/ask prices
    bid_px = mid - spread / 2.0
    ask_px = mid + spread / 2.0

    # Queue sizes: correlated OU with regime shifts
    bid_qty = np.empty(n, dtype=np.float64)
    ask_qty = np.empty(n, dtype=np.float64)
    bid_qty[0] = 500.0
    ask_qty[0] = 500.0

    regime = 0  # 0=balanced, 1=bid_pressure, 2=ask_pressure
    for i in range(1, n):
        # Regime transitions
        if rng.random() < 0.001:
            regime = rng.integers(0, 3)

        bid_drift = 0.0
        ask_drift = 0.0
        if regime == 1:
            bid_drift = 2.0
            ask_drift = -1.0
        elif regime == 2:
            bid_drift = -1.0
            ask_drift = 2.0

        bid_qty[i] = max(
            10.0,
            bid_qty[i - 1] + 0.01 * (500.0 - bid_qty[i - 1])
            + bid_drift + 20.0 * rng.standard_normal(),
        )
        ask_qty[i] = max(
            10.0,
            ask_qty[i - 1] + 0.01 * (500.0 - ask_qty[i - 1])
            + ask_drift + 20.0 * rng.standard_normal(),
        )

    # Volume: Hawkes-like clustering
    volume = np.abs(rng.standard_normal(n)) * 10.0 + 1.0

    # Local timestamps (2ms cadence like TWSE)
    local_ts = np.arange(n, dtype=np.int64) * 2_000_000  # ns

    dtype = np.dtype([
        ("bid_qty", "f8"), ("ask_qty", "f8"),
        ("bid_px", "f8"), ("ask_px", "f8"),
        ("mid_price", "f8"), ("spread_bps", "f8"),
        ("volume", "f8"), ("local_ts", "i8"),
    ])
    data = np.empty(n, dtype=dtype)
    data["bid_qty"] = bid_qty
    data["ask_qty"] = ask_qty
    data["bid_px"] = bid_px
    data["ask_px"] = ask_px
    data["mid_price"] = mid
    data["spread_bps"] = spread
    data["volume"] = volume
    data["local_ts"] = local_ts
    return data


# ---------------------------------------------------------------------------
# 15 OFI alpha formulas
# ---------------------------------------------------------------------------

def _ofi(bid_qty: np.ndarray, ask_qty: np.ndarray) -> np.ndarray:
    """Core OFI = diff(bid_qty) - diff(ask_qty)."""
    return np.diff(bid_qty, prepend=bid_qty[0]) - np.diff(ask_qty, prepend=ask_qty[0])


def _d_mid(mid: np.ndarray) -> np.ndarray:
    """diff(mid, prepend=mid[0])."""
    return np.diff(mid, prepend=mid[0])


def alpha_ofi_raw(bid_qty: np.ndarray, ask_qty: np.ndarray, **_: Any) -> np.ndarray:
    """EMA8(OFI). Simple smoothed order flow imbalance."""
    ofi = _ofi(bid_qty, ask_qty)
    return _ema(ofi, _EMA_ALPHA_8)


def alpha_ofi_regime(bid_qty: np.ndarray, ask_qty: np.ndarray, **_: Any) -> np.ndarray:
    """EMA8(OFI) * clip(EMA16(|OFI|)/max(EMA64(|OFI|),eps), 0.5, 2.0).
    Volatility-regime-adaptive OFI."""
    ofi = _ofi(bid_qty, ask_qty)
    ofi_ema8 = _ema(ofi, _EMA_ALPHA_8)
    vol16 = _ema(np.abs(ofi), _EMA_ALPHA_16)
    base64 = _ema(np.abs(ofi), _EMA_ALPHA_64) + _EPS
    rf = np.clip(vol16 / base64, 0.5, 2.0)
    return ofi_ema8 * rf


def alpha_ofi_acceleration(bid_qty: np.ndarray, ask_qty: np.ndarray, **_: Any) -> np.ndarray:
    """EMA4(OFI) - EMA8(OFI). OFI momentum/acceleration."""
    ofi = _ofi(bid_qty, ask_qty)
    return _ema(ofi, _EMA_ALPHA_4) - _ema(ofi, _EMA_ALPHA_8)


def alpha_cum_ofi_revert(bid_qty: np.ndarray, ask_qty: np.ndarray, **_: Any) -> np.ndarray:
    """clip(-(cumsum(OFI)-EMA64(cumsum(OFI)))/max(EMA64(|cumsum(OFI)|),eps), -2, 2).
    Mean-reversion of cumulative OFI."""
    ofi = _ofi(bid_qty, ask_qty)
    cum_ofi = np.cumsum(ofi)
    ema64_cum = _ema(cum_ofi, _EMA_ALPHA_64)
    ema64_abs_cum = _ema(np.abs(cum_ofi), _EMA_ALPHA_64) + _EPS
    return np.clip(-(cum_ofi - ema64_cum) / ema64_abs_cum, -2.0, 2.0)


def alpha_hawkes_ofi_impact(bid_qty: np.ndarray, ask_qty: np.ndarray, **_: Any) -> np.ndarray:
    """clip(EMA8(OFI)*EMA4(|OFI|)/max(EMA32(|OFI|),eps), -2, 2).
    Self-exciting OFI impact."""
    ofi = _ofi(bid_qty, ask_qty)
    ofi_ema8 = _ema(ofi, _EMA_ALPHA_8)
    abs_ofi = np.abs(ofi)
    fast_vol = _ema(abs_ofi, _EMA_ALPHA_4)
    slow_vol = _ema(abs_ofi, _EMA_ALPHA_32) + _EPS
    return np.clip(ofi_ema8 * fast_vol / slow_vol, -2.0, 2.0)


def alpha_ofi_signed_depth(bid_qty: np.ndarray, ask_qty: np.ndarray, **_: Any) -> np.ndarray:
    """clip(OFI/max(bid_qty+ask_qty,eps), -1, 1).
    OFI normalized by total depth."""
    ofi = _ofi(bid_qty, ask_qty)
    total_depth = bid_qty + ask_qty + _EPS
    return np.clip(ofi / total_depth, -1.0, 1.0)


def alpha_ofi_asymmetry(bid_qty: np.ndarray, ask_qty: np.ndarray, **_: Any) -> np.ndarray:
    """clip((EMA16(max(OFI,0)^2)-EMA16(max(-OFI,0)^2))/max(sum_sq,eps), -1, 1).
    Second-moment OFI decomposition."""
    ofi = _ofi(bid_qty, ask_qty)
    pos_sq = np.maximum(ofi, 0.0) ** 2
    neg_sq = np.maximum(-ofi, 0.0) ** 2
    ema_pos = _ema(pos_sq, _EMA_ALPHA_16)
    ema_neg = _ema(neg_sq, _EMA_ALPHA_16)
    sum_sq = ema_pos + ema_neg + _EPS
    return np.clip((ema_pos - ema_neg) / sum_sq, -1.0, 1.0)


def alpha_ofi_spread_interaction(
    bid_qty: np.ndarray, ask_qty: np.ndarray, spread: np.ndarray, **_: Any
) -> np.ndarray:
    """clip(EMA8(OFI)*spread/max(EMA64(spread),1), -2, 2).
    OFI amplified by spread excess."""
    ofi = _ofi(bid_qty, ask_qty)
    ofi_ema8 = _ema(ofi, _EMA_ALPHA_8)
    spread_ema64 = np.maximum(_ema(spread, _EMA_ALPHA_64), 1.0)
    return np.clip(ofi_ema8 * spread / spread_ema64, -2.0, 2.0)


def alpha_ofi_momentum_divergence(
    bid_qty: np.ndarray, ask_qty: np.ndarray, spread: np.ndarray, **_: Any
) -> np.ndarray:
    """clip((EMA4(OFI)-EMA32(OFI))*clip(vol_ratio,0.5,2), -2, 2).
    OFI multi-scale divergence with volatility gating."""
    ofi = _ofi(bid_qty, ask_qty)
    fast = _ema(ofi, _EMA_ALPHA_4)
    slow = _ema(ofi, _EMA_ALPHA_32)
    abs_ofi = np.abs(ofi)
    vol_fast = _ema(abs_ofi, _EMA_ALPHA_4) + _EPS
    vol_slow = _ema(abs_ofi, _EMA_ALPHA_32) + _EPS
    vol_ratio = np.clip(vol_fast / vol_slow, 0.5, 2.0)
    return np.clip((fast - slow) * vol_ratio, -2.0, 2.0)


def alpha_ofi_price_residual(
    bid_qty: np.ndarray, ask_qty: np.ndarray, mid: np.ndarray, **_: Any
) -> np.ndarray:
    """clip(EMA8(OFI - beta*d_mid), -2, 2) where beta=cov/var via EMA.
    OFI orthogonalized against price changes."""
    ofi = _ofi(bid_qty, ask_qty)
    dm = _d_mid(mid)
    # Rolling regression: beta = EMA(ofi*dm) / EMA(dm^2)
    cov = _ema(ofi * dm, _EMA_ALPHA_32)
    var = _ema(dm ** 2, _EMA_ALPHA_32) + _EPS
    beta = cov / var
    residual = ofi - beta * dm
    return np.clip(_ema(residual, _EMA_ALPHA_8), -2.0, 2.0)


def alpha_ofi_persistence(bid_qty: np.ndarray, ask_qty: np.ndarray, **_: Any) -> np.ndarray:
    """clip(EMA4(OFI)*EMA32(OFI)/max(EMA16(OFI^2),eps), -1, 1).
    Fast/slow OFI agreement normalized by variance."""
    ofi = _ofi(bid_qty, ask_qty)
    fast = _ema(ofi, _EMA_ALPHA_4)
    slow = _ema(ofi, _EMA_ALPHA_32)
    var16 = _ema(ofi ** 2, _EMA_ALPHA_16) + _EPS
    return np.clip(fast * slow / var16, -1.0, 1.0)


def alpha_ofi_regime_switch(bid_qty: np.ndarray, ask_qty: np.ndarray, **_: Any) -> np.ndarray:
    """|EMA4(|OFI|)/max(EMA32(|OFI|),eps) - 1|.
    Unsigned regime-change detector."""
    ofi = _ofi(bid_qty, ask_qty)
    abs_ofi = np.abs(ofi)
    fast = _ema(abs_ofi, _EMA_ALPHA_4)
    slow = _ema(abs_ofi, _EMA_ALPHA_32) + _EPS
    return np.abs(fast / slow - 1.0)


def alpha_market_resistance(
    bid_qty: np.ndarray, ask_qty: np.ndarray, mid: np.ndarray, **_: Any
) -> np.ndarray:
    """clip(EMA16(OFI*sign(d_mid))/max(EMA16(|OFI|),eps), -1, 1).
    OFI-price alignment: positive = OFI drives price, negative = resistance."""
    ofi = _ofi(bid_qty, ask_qty)
    dm = _d_mid(mid)
    aligned = ofi * np.sign(dm)
    ema_aligned = _ema(aligned, _EMA_ALPHA_16)
    ema_abs = _ema(np.abs(ofi), _EMA_ALPHA_16) + _EPS
    return np.clip(ema_aligned / ema_abs, -1.0, 1.0)


def alpha_rough_vol_ofi(bid_qty: np.ndarray, ask_qty: np.ndarray, **_: Any) -> np.ndarray:
    """clip(EMA8(OFI)*(1 - EMA64(|OFI|)/max(EMA16(|OFI|),eps)), -2, 2).
    OFI attenuated in smooth-vol regimes, amplified in rough-vol."""
    ofi = _ofi(bid_qty, ask_qty)
    ofi_ema8 = _ema(ofi, _EMA_ALPHA_8)
    abs_ofi = np.abs(ofi)
    slow_vol = _ema(abs_ofi, _EMA_ALPHA_64)
    fast_vol = _ema(abs_ofi, _EMA_ALPHA_16) + _EPS
    roughness = 1.0 - slow_vol / fast_vol
    return np.clip(ofi_ema8 * roughness, -2.0, 2.0)


def alpha_ofi_entropy_proxy(bid_qty: np.ndarray, ask_qty: np.ndarray, **_: Any) -> np.ndarray:
    """-EMA16(|OFI_norm| * log(|OFI_norm| + eps)).
    Entropy-like measure of OFI dispersion. Low entropy = concentrated flow."""
    ofi = _ofi(bid_qty, ask_qty)
    abs_ofi = np.abs(ofi)
    # Normalize by rolling max to get [0,1]-ish range
    ofi_norm = abs_ofi / (_ema(abs_ofi, _EMA_ALPHA_64) + _EPS)
    ofi_norm = np.minimum(ofi_norm, 10.0)  # cap to avoid log explosion
    entropy_term = ofi_norm * np.log(ofi_norm + _EPS)
    return -_ema(entropy_term, _EMA_ALPHA_16)


# ---------------------------------------------------------------------------
# Alpha registry
# ---------------------------------------------------------------------------
ALPHA_REGISTRY: dict[str, tuple[callable, str]] = {
    "ofi_raw":                 (alpha_ofi_raw, "EMA8(OFI)"),
    "ofi_regime":              (alpha_ofi_regime, "OFI x vol-regime factor"),
    "ofi_acceleration":        (alpha_ofi_acceleration, "EMA4-EMA8 OFI momentum"),
    "cum_ofi_revert":          (alpha_cum_ofi_revert, "cumOFI mean-reversion"),
    "hawkes_ofi_impact":       (alpha_hawkes_ofi_impact, "self-exciting OFI impact"),
    "ofi_signed_depth":        (alpha_ofi_signed_depth, "OFI / total depth"),
    "ofi_asymmetry":           (alpha_ofi_asymmetry, "2nd-moment OFI decomp"),
    "ofi_spread_interaction":  (alpha_ofi_spread_interaction, "OFI x spread excess"),
    "ofi_momentum_divergence": (alpha_ofi_momentum_divergence, "multi-scale OFI div"),
    "ofi_price_residual":      (alpha_ofi_price_residual, "OFI orthog to price"),
    "ofi_persistence":         (alpha_ofi_persistence, "fast/slow OFI agreement"),
    "ofi_regime_switch":       (alpha_ofi_regime_switch, "vol regime change detect"),
    "market_resistance":       (alpha_market_resistance, "OFI-price alignment"),
    "rough_vol_ofi":           (alpha_rough_vol_ofi, "OFI x vol roughness"),
    "ofi_entropy_proxy":       (alpha_ofi_entropy_proxy, "OFI flow concentration"),
}


# ---------------------------------------------------------------------------
# Metrics computation
# ---------------------------------------------------------------------------

def _compute_forward_returns(mid: np.ndarray, horizons: list[int]) -> dict[int, np.ndarray]:
    """Compute forward returns at multiple horizons (in ticks)."""
    ret = {}
    for h in horizons:
        fwd = np.empty_like(mid)
        fwd[:-h] = (mid[h:] - mid[:-h]) / (mid[:-h] + _EPS)
        fwd[-h:] = 0.0
        ret[h] = fwd
    return ret


def _compute_ic(signal: np.ndarray, fwd_ret: np.ndarray, n_chunks: int = 20) -> tuple[float, float]:
    """Compute mean IC and IC stability (IC_mean / IC_std).

    IC is rank correlation computed on non-overlapping chunks.
    """
    valid = np.isfinite(signal) & np.isfinite(fwd_ret) & (signal != 0.0)
    sig_v = signal[valid]
    ret_v = fwd_ret[valid]

    if len(sig_v) < 1000:
        return 0.0, 0.0

    chunk_size = len(sig_v) // n_chunks
    if chunk_size < 50:
        return 0.0, 0.0

    ics = []
    for i in range(n_chunks):
        s = sig_v[i * chunk_size : (i + 1) * chunk_size]
        r = ret_v[i * chunk_size : (i + 1) * chunk_size]
        rank_s = np.argsort(np.argsort(s)).astype(np.float64)
        rank_r = np.argsort(np.argsort(r)).astype(np.float64)
        rank_s -= rank_s.mean()
        rank_r -= rank_r.mean()
        denom = np.sqrt((rank_s ** 2).sum() * (rank_r ** 2).sum())
        if denom < _EPS:
            ics.append(0.0)
        else:
            ics.append(float((rank_s * rank_r).sum() / denom))

    ic_arr = np.array(ics)
    ic_mean = float(ic_arr.mean())
    ic_std = float(ic_arr.std()) + _EPS
    return ic_mean, ic_mean / ic_std


def _compute_autocorr(signal: np.ndarray, lag: int = 1) -> float:
    """Lag-1 autocorrelation."""
    valid = np.isfinite(signal) & (signal != 0.0)
    s = signal[valid]
    if len(s) < lag + 100:
        return 0.0
    s = s - s.mean()
    c0 = np.dot(s, s)
    if c0 < _EPS:
        return 0.0
    c1 = np.dot(s[:-lag], s[lag:])
    return float(c1 / c0)


def _compute_turnover(signal: np.ndarray) -> float:
    """Signal turnover: mean |delta_signal| / mean |signal|."""
    valid = np.isfinite(signal) & (signal != 0.0)
    s = signal[valid]
    if len(s) < 100:
        return 0.0
    ds = np.abs(np.diff(s))
    return float(ds.mean() / (np.abs(s).mean() + _EPS))


def _compute_hit_rate(signal: np.ndarray, fwd_ret: np.ndarray) -> float:
    """Fraction of ticks where sign(signal) == sign(forward_return)."""
    valid = np.isfinite(signal) & np.isfinite(fwd_ret) & (signal != 0.0) & (fwd_ret != 0.0)
    if valid.sum() < 100:
        return 0.5
    return float((np.sign(signal[valid]) == np.sign(fwd_ret[valid])).mean())


# ---------------------------------------------------------------------------
# Per-symbol exploration
# ---------------------------------------------------------------------------

def explore_symbol(
    data_path: str,
    horizons: list[int] | None = None,
) -> dict[str, Any]:
    """Run all OFI alphas on one symbol's L1 data."""
    if horizons is None:
        horizons = [50, 200, 1000, 5000]

    data = np.load(data_path)
    n = len(data)
    if n < 2000:
        return {}

    bid_qty = data["bid_qty"].astype(np.float64)
    ask_qty = data["ask_qty"].astype(np.float64)
    mid = data["mid_price"].astype(np.float64)
    spread = data["spread_bps"].astype(np.float64)

    fwd_rets = _compute_forward_returns(mid, horizons)

    results: dict[str, Any] = {}
    for alpha_id, (alpha_fn, desc) in ALPHA_REGISTRY.items():
        try:
            signal = alpha_fn(bid_qty=bid_qty, ask_qty=ask_qty, spread=spread, mid=mid)
        except Exception as e:
            logger.warning("alpha_failed", alpha=alpha_id, error=str(e))
            continue

        warmup = 500
        sig_w = signal[warmup:]

        alpha_result: dict[str, Any] = {
            "description": desc,
            "n_rows": n,
            "signal_mean": float(np.nanmean(sig_w)),
            "signal_std": float(np.nanstd(sig_w)),
            "signal_abs_mean": float(np.nanmean(np.abs(sig_w))),
            "acf_1": _compute_autocorr(sig_w, 1),
            "turnover": _compute_turnover(sig_w),
            "horizons": {},
        }

        for h in horizons:
            fwd = fwd_rets[h][warmup:]
            ic_mean, ic_ir = _compute_ic(sig_w, fwd)
            hit = _compute_hit_rate(sig_w, fwd)
            alpha_result["horizons"][str(h)] = {
                "ic_mean": ic_mean,
                "ic_ir": ic_ir,
                "hit_rate": hit,
            }

        results[alpha_id] = alpha_result

    return results


# ---------------------------------------------------------------------------
# Batch exploration across all symbols
# ---------------------------------------------------------------------------

def run_exploration(
    data_dir: str,
    horizons: list[int] | None = None,
    out_path: str | None = None,
) -> dict[str, Any]:
    """Run OFI alpha exploration across all symbols in data_dir.

    If no real L1 data is found, generates synthetic data for 3 symbols
    to validate all alpha computations.
    """
    base = Path(data_dir)
    if horizons is None:
        horizons = [50, 200, 1000, 5000]

    # Find all concatenated L1 files
    all_files: list[tuple[str, str]] = []
    if base.exists():
        for sym_dir in sorted(base.iterdir()):
            if not sym_dir.is_dir():
                continue
            sym = sym_dir.name.upper()
            concat_f = sym_dir / f"{sym}_all_l1.npy"
            if concat_f.exists():
                all_files.append((sym, str(concat_f)))
            else:
                daily = sorted(sym_dir.glob(f"{sym}_*_l1.npy"))
                daily = [f for f in daily if "all" not in f.name]
                if daily:
                    largest = max(daily, key=lambda f: f.stat().st_size)
                    all_files.append((sym, str(largest)))

    # Also check for processed data files
    processed_dir = Path(data_dir).parent / "processed"
    if processed_dir.exists():
        for sub in sorted(processed_dir.iterdir()):
            if not sub.is_dir():
                continue
            for npy in sorted(sub.glob("*.npy")):
                if npy.suffix == ".npy" and ".meta" not in npy.name:
                    sym = sub.name.upper()
                    all_files.append((sym, str(npy)))

    # Generate synthetic data if no real data found
    synth_dir: Path | None = None
    if not all_files:
        logger.info("no_real_data_found", data_dir=data_dir, action="generating_synthetic")
        synth_dir = Path(data_dir).parent / "interim" / "_ofi_synth"
        synth_dir.mkdir(parents=True, exist_ok=True)
        for i, (sym, seed) in enumerate([("SYNTH_A", 42), ("SYNTH_B", 123), ("SYNTH_C", 7)]):
            n_ticks = 50_000 + i * 25_000
            data = _generate_synthetic_l1(n=n_ticks, seed=seed)
            fpath = synth_dir / f"{sym}_synth_l1.npy"
            np.save(str(fpath), data)
            all_files.append((sym, str(fpath)))

    logger.info("found_symbols", count=len(all_files))

    per_symbol: dict[str, dict] = {}
    t0 = time.monotonic()

    for sym, fpath in all_files:
        logger.info("exploring", symbol=sym, path=fpath)
        t_sym = time.monotonic()
        sym_results = explore_symbol(fpath, horizons)
        elapsed = time.monotonic() - t_sym
        per_symbol[sym] = sym_results
        n = np.load(fpath).shape[0]
        logger.info("explored", symbol=sym, rows=n, alphas=len(sym_results), elapsed_s=f"{elapsed:.1f}")

    total_elapsed = time.monotonic() - t0
    logger.info("exploration_complete", symbols=len(per_symbol), elapsed_s=f"{total_elapsed:.1f}")

    # Clean up synthetic data
    if synth_dir is not None:
        import shutil
        shutil.rmtree(synth_dir, ignore_errors=True)

    # ---------------------------------------------------------------------------
    # Cross-symbol aggregation
    # ---------------------------------------------------------------------------
    alpha_ids = list(ALPHA_REGISTRY.keys())
    leaderboard: list[dict[str, Any]] = []

    for alpha_id in alpha_ids:
        agg: dict[str, Any] = {"alpha_id": alpha_id, "description": ALPHA_REGISTRY[alpha_id][1]}

        for h in horizons:
            h_key = str(h)
            ics = []
            irs = []
            hits = []
            for sym, sym_res in per_symbol.items():
                if alpha_id in sym_res and h_key in sym_res[alpha_id].get("horizons", {}):
                    m = sym_res[alpha_id]["horizons"][h_key]
                    ics.append(m["ic_mean"])
                    irs.append(m["ic_ir"])
                    hits.append(m["hit_rate"])

            if ics:
                ic_arr = np.array(ics)
                agg[f"h{h}_ic_mean"] = float(ic_arr.mean())
                agg[f"h{h}_ic_std"] = float(ic_arr.std())
                agg[f"h{h}_ic_ir"] = float(ic_arr.mean() / (ic_arr.std() + _EPS))
                agg[f"h{h}_hit_mean"] = float(np.mean(hits))
                agg[f"h{h}_syms_positive"] = int((ic_arr > 0).sum())
                agg[f"h{h}_syms_total"] = len(ics)
                best_idx = int(np.argmax(np.abs(ic_arr)))
                sym_keys = list(per_symbol.keys())
                agg[f"h{h}_best_sym"] = sym_keys[best_idx] if best_idx < len(sym_keys) else ""
                agg[f"h{h}_best_ic"] = float(ic_arr[best_idx])

        acfs = []
        turnovers = []
        for sym, sym_res in per_symbol.items():
            if alpha_id in sym_res:
                acfs.append(sym_res[alpha_id]["acf_1"])
                turnovers.append(sym_res[alpha_id]["turnover"])
        if acfs:
            agg["acf_1_mean"] = float(np.mean(acfs))
            agg["turnover_mean"] = float(np.mean(turnovers))

        leaderboard.append(agg)

    leaderboard.sort(key=lambda x: abs(x.get("h1000_ic_mean", 0)), reverse=True)

    output = {
        "timestamp": time.strftime("%Y-%m-%d %H:%M:%S"),
        "total_symbols": len(per_symbol),
        "total_alphas": len(alpha_ids),
        "horizons": horizons,
        "elapsed_s": total_elapsed,
        "leaderboard": leaderboard,
        "per_symbol": per_symbol,
    }

    if out_path:
        out_p = Path(out_path)
        out_p.parent.mkdir(parents=True, exist_ok=True)
        out_p.write_text(json.dumps(output, indent=2, default=str), encoding="utf-8")
        logger.info("results_saved", path=out_path)

    return output


def print_leaderboard(output: dict[str, Any]) -> None:
    """Pretty-print the alpha leaderboard."""
    lb = output["leaderboard"]
    horizons = output["horizons"]

    print(f"\n{'='*120}")
    print(f"OFI ALPHA LEADERBOARD -- {output['total_symbols']} symbols, {output['total_alphas']} alphas")
    print(f"{'='*120}")

    best_h = 1000

    print(f"\n{'Alpha':<30} {'IC@{}'.format(best_h):>10} {'IC_IR':>8} {'Hit%':>7} "
          f"{'ACF-1':>7} {'Turn':>7} {'Syms+':>7} {'Best':>8} {'BestIC':>8}  Description")
    print("-" * 120)

    for entry in lb:
        h_key = f"h{best_h}"
        ic = entry.get(f"{h_key}_ic_mean", 0)
        ir = entry.get(f"{h_key}_ic_ir", 0)
        hit = entry.get(f"{h_key}_hit_mean", 0.5)
        acf = entry.get("acf_1_mean", 0)
        turn = entry.get("turnover_mean", 0)
        pos = entry.get(f"{h_key}_syms_positive", 0)
        tot = entry.get(f"{h_key}_syms_total", 0)
        best_sym = entry.get(f"{h_key}_best_sym", "")
        best_ic = entry.get(f"{h_key}_best_ic", 0)

        star = "*" if abs(ic) > 0.02 and ir > 1.5 else " "

        print(f"{star}{entry['alpha_id']:<29} {ic:>+10.5f} {ir:>8.2f} {hit*100:>6.1f}% "
              f"{acf:>7.3f} {turn:>7.3f} {pos:>3}/{tot:<3} {best_sym:>8} {best_ic:>+8.4f}  {entry['description']}")

    print(f"\n{'='*120}")
    print("TOP 5 -- Multi-Horizon IC Profile")
    print(f"{'='*120}")
    top5 = lb[:5]
    print(f"{'Alpha':<30}", end="")
    for h in horizons:
        print(f" {'IC@'+str(h):>10}", end="")
    print()
    print("-" * (30 + 11 * len(horizons)))
    for entry in top5:
        print(f"{entry['alpha_id']:<30}", end="")
        for h in horizons:
            ic = entry.get(f"h{h}_ic_mean", 0)
            print(f" {ic:>+10.5f}", end="")
        print()


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main() -> int:
    parser = argparse.ArgumentParser(description="Large-scale OFI alpha exploration")
    parser.add_argument("--data-dir", default="research/data/raw", help="Directory with per-symbol L1 data")
    parser.add_argument("--out", default="research/results/ofi_exploration.json", help="Output JSON")
    parser.add_argument("--horizons", default="50,200,1000,5000", help="Forward return horizons (ticks)")
    args = parser.parse_args()

    horizons = [int(h) for h in args.horizons.split(",")]
    output = run_exploration(data_dir=args.data_dir, horizons=horizons, out_path=args.out)
    print_leaderboard(output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
