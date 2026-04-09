"""Stage 4b: Supplementary statistics for mlofi_gradient and book_convexity.

Addresses Challenger findings:
1. Block bootstrap IC with autocorrelation correction
2. In-sample / Out-of-sample split (7 train / 3 test days)
3. mlofi_gradient component isolation (gradient-only vs convexity-only)
4. book_convexity corrected IC assessment

Usage:
    uv run python -m research.tools.signal_replay_supplementary \
        --symbols 2330,2317,TXFD6 \
        --data-dir research/data/l5/ \
        --out outputs/team_artifacts/alpha-research/
"""

from __future__ import annotations

import argparse
import json
import math
import sys
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import numpy as np
import structlog
from scipy import stats as scipy_stats

_HERE = Path(__file__).resolve()
_RESEARCH_ROOT = _HERE.parent.parent
_PROJECT_ROOT = _RESEARCH_ROOT.parent
for _p in (_PROJECT_ROOT, _RESEARCH_ROOT):
    _s = str(_p)
    if _s not in sys.path:
        sys.path.insert(0, _s)

from research.alphas.book_convexity.impl import BookConvexityAlpha
from research.alphas.mlofi_gradient.impl import (
    MlofiGradientAlpha,
    _CONVEXITY_DENOM,
    _CONVEXITY_WEIGHT,
    _CONVEXITY_WEIGHTS,
    _EMA_FAST,
    _EMA_OUTPUT,
    _GRADIENT_DENOM,
    _GRADIENT_WEIGHTS,
    _N_LEVELS,
    _SIGNAL_CLIP,
    _WARMUP_TICKS as _MG_WARMUP,
)
from research.alphas.book_convexity.impl import _WARMUP_TICKS as _BC_WARMUP

logger = structlog.get_logger(__name__)

_HORIZONS_MS = [100, 500, 1_000, 5_000, 30_000]
_HORIZONS_NS = [h * 1_000_000 for h in _HORIZONS_MS]
_HORIZON_LABELS = ["100ms", "500ms", "1s", "5s", "30s"]
_LATENCY_NS = 36_000_000
_DAY_GAP_NS = 4 * 3_600_000_000_000


def _split_days(timestamps: np.ndarray) -> list[tuple[int, int]]:
    if len(timestamps) == 0:
        return []
    gaps = np.diff(timestamps)
    boundaries = np.where(gaps > _DAY_GAP_NS)[0] + 1
    starts = np.concatenate([[0], boundaries])
    ends = np.concatenate([boundaries, [len(timestamps)]])
    return [(int(s), int(e)) for s, e in zip(starts, ends)]


def _compute_forward_returns_segment(
    timestamps: np.ndarray, mid: np.ndarray, start: int, end: int,
) -> dict[str, np.ndarray]:
    n = end - start
    results: dict[str, np.ndarray] = {}
    for horizon_ns, label in zip(_HORIZONS_NS, _HORIZON_LABELS):
        fwd = np.full(n, np.nan, dtype=np.float64)
        target_offset = _LATENCY_NS + horizon_ns
        j = 0
        for i in range(n):
            gi = start + i
            target_ts = timestamps[gi] + target_offset
            while start + j < end and timestamps[start + j] < target_ts:
                j += 1
            if start + j < end and mid[gi] > 0:
                fwd[i] = (mid[start + j] - mid[gi]) / mid[gi]
        results[label] = fwd
    return results


def _rank_ic(s: np.ndarray, r: np.ndarray) -> float:
    valid = np.isfinite(s) & np.isfinite(r) & (s != 0.0)
    if valid.sum() < 30:
        return float("nan")
    c, _ = scipy_stats.spearmanr(s[valid], r[valid])
    return float(c)


# ============================================================
# 1. Block bootstrap IC with autocorrelation correction
# ============================================================

def _block_bootstrap_ic(
    signals: np.ndarray,
    returns: np.ndarray,
    n_bootstrap: int = 500,
    block_size: int = 200,
    rng_seed: int = 42,
) -> dict[str, float]:
    """Block bootstrap for IC standard error, correcting for autocorrelation.

    Uses non-overlapping blocks of `block_size` ticks to preserve serial dependence.
    """
    valid = np.isfinite(signals) & np.isfinite(returns) & (signals != 0.0)
    s = signals[valid]
    r = returns[valid]
    n = len(s)

    if n < block_size * 3:
        return {"ic": _rank_ic(signals, returns), "ic_se": float("nan"), "ic_tstat_corrected": float("nan"), "n_blocks": 0}

    rng = np.random.default_rng(rng_seed)
    n_blocks = n // block_size
    ic_samples: list[float] = []

    for _ in range(n_bootstrap):
        # Sample n_blocks blocks with replacement
        block_starts = rng.integers(0, n - block_size, size=n_blocks)
        s_boot = np.concatenate([s[bs:bs + block_size] for bs in block_starts])
        r_boot = np.concatenate([r[bs:bs + block_size] for bs in block_starts])
        if len(s_boot) > 30:
            c, _ = scipy_stats.spearmanr(s_boot, r_boot)
            if np.isfinite(c):
                ic_samples.append(float(c))

    if len(ic_samples) < 10:
        return {"ic": _rank_ic(signals, returns), "ic_se": float("nan"), "ic_tstat_corrected": float("nan"), "n_blocks": 0}

    ic_point = _rank_ic(signals, returns)
    ic_se = float(np.std(ic_samples))
    ic_tstat = ic_point / ic_se if ic_se > 0 else float("nan")

    return {
        "ic": ic_point,
        "ic_se": ic_se,
        "ic_tstat_corrected": ic_tstat,
        "n_blocks": n_blocks,
        "bootstrap_samples": len(ic_samples),
    }


# ============================================================
# 2. IS/OOS split
# ============================================================

def _is_oos_ic(
    signals: np.ndarray,
    fwd_returns: dict[str, np.ndarray],
    day_segments: list[tuple[int, int]],
    warmup: int,
    is_days: int = 7,
) -> dict[str, dict[str, float]]:
    """Split by days: first is_days = IS, rest = OOS."""
    n_days = len(day_segments)
    if n_days < is_days + 1:
        is_days = max(1, n_days - 1)

    is_segs = day_segments[:is_days]
    oos_segs = day_segments[is_days:]

    def _ic_for_segs(segs: list[tuple[int, int]]) -> dict[str, float]:
        result: dict[str, float] = {}
        for label in _HORIZON_LABELS:
            all_s: list[np.ndarray] = []
            all_r: list[np.ndarray] = []
            for ds, de in segs:
                eff = ds + warmup
                if eff >= de:
                    continue
                all_s.append(signals[eff:de])
                all_r.append(fwd_returns[label][eff:de])
            if all_s:
                s = np.concatenate(all_s)
                r = np.concatenate(all_r)
                result[label] = _rank_ic(s, r)
            else:
                result[label] = float("nan")
        return result

    return {
        "in_sample": _ic_for_segs(is_segs),
        "out_of_sample": _ic_for_segs(oos_segs),
        "is_days": len(is_segs),
        "oos_days": len(oos_segs),
    }


# ============================================================
# 3. Component isolation for mlofi_gradient
# ============================================================

class _GradientOnlyAlpha:
    """mlofi_gradient with ONLY the gradient component (convexity weight = 0)."""

    __slots__ = (
        "_prev_bid_qty", "_prev_ask_qty", "_cur_bid_qty", "_cur_ask_qty",
        "_mlofi_ema", "_gradient_ema", "_signal", "_initialized", "_tick_count",
    )

    def __init__(self) -> None:
        self._prev_bid_qty = np.zeros(_N_LEVELS, dtype=np.float64)
        self._prev_ask_qty = np.zeros(_N_LEVELS, dtype=np.float64)
        self._cur_bid_qty = np.zeros(_N_LEVELS, dtype=np.float64)
        self._cur_ask_qty = np.zeros(_N_LEVELS, dtype=np.float64)
        self._mlofi_ema = np.zeros(_N_LEVELS, dtype=np.float64)
        self._gradient_ema: float = 0.0
        self._signal: float = 0.0
        self._initialized: bool = False
        self._tick_count: int = 0

    def update(self, *args: float, **kwargs: object) -> float:
        cur_bid = self._cur_bid_qty
        cur_ask = self._cur_ask_qty
        cur_bid[:] = 0.0
        cur_ask[:] = 0.0
        bids = np.asarray(kwargs["bids"], dtype=np.float64).reshape(-1, 2)
        asks = np.asarray(kwargs["asks"], dtype=np.float64).reshape(-1, 2)
        n_bid = min(bids.shape[0], _N_LEVELS)
        n_ask = min(asks.shape[0], _N_LEVELS)
        cur_bid[:n_bid] = bids[:n_bid, 1]
        cur_ask[:n_ask] = asks[:n_ask, 1]
        self._tick_count += 1
        delta_bid = cur_bid - self._prev_bid_qty
        delta_ask = cur_ask - self._prev_ask_qty
        mlofi = delta_bid - delta_ask
        np.copyto(self._prev_bid_qty, cur_bid)
        np.copyto(self._prev_ask_qty, cur_ask)
        if not self._initialized:
            np.copyto(self._mlofi_ema, mlofi)
            self._initialized = True
        else:
            self._mlofi_ema += _EMA_FAST * (mlofi - self._mlofi_ema)
        # GRADIENT ONLY — no convexity
        gradient = float(np.dot(_GRADIENT_WEIGHTS, self._mlofi_ema)) / _GRADIENT_DENOM
        self._gradient_ema += _EMA_OUTPUT * (gradient - self._gradient_ema)
        if self._tick_count < _MG_WARMUP:
            self._signal = 0.0
        else:
            self._signal = max(-_SIGNAL_CLIP, min(_SIGNAL_CLIP, self._gradient_ema))
        return self._signal


class _ConvexityOnlyAlpha:
    """mlofi_gradient with ONLY the convexity component (gradient weight = 0)."""

    __slots__ = (
        "_prev_bid_qty", "_prev_ask_qty", "_cur_bid_qty", "_cur_ask_qty",
        "_mlofi_ema", "_conv_ema", "_signal", "_initialized", "_tick_count",
    )

    def __init__(self) -> None:
        self._prev_bid_qty = np.zeros(_N_LEVELS, dtype=np.float64)
        self._prev_ask_qty = np.zeros(_N_LEVELS, dtype=np.float64)
        self._cur_bid_qty = np.zeros(_N_LEVELS, dtype=np.float64)
        self._cur_ask_qty = np.zeros(_N_LEVELS, dtype=np.float64)
        self._mlofi_ema = np.zeros(_N_LEVELS, dtype=np.float64)
        self._conv_ema: float = 0.0
        self._signal: float = 0.0
        self._initialized: bool = False
        self._tick_count: int = 0

    def update(self, *args: float, **kwargs: object) -> float:
        cur_bid = self._cur_bid_qty
        cur_ask = self._cur_ask_qty
        cur_bid[:] = 0.0
        cur_ask[:] = 0.0
        bids = np.asarray(kwargs["bids"], dtype=np.float64).reshape(-1, 2)
        asks = np.asarray(kwargs["asks"], dtype=np.float64).reshape(-1, 2)
        n_bid = min(bids.shape[0], _N_LEVELS)
        n_ask = min(asks.shape[0], _N_LEVELS)
        cur_bid[:n_bid] = bids[:n_bid, 1]
        cur_ask[:n_ask] = asks[:n_ask, 1]
        self._tick_count += 1
        delta_bid = cur_bid - self._prev_bid_qty
        delta_ask = cur_ask - self._prev_ask_qty
        mlofi = delta_bid - delta_ask
        np.copyto(self._prev_bid_qty, cur_bid)
        np.copyto(self._prev_ask_qty, cur_ask)
        if not self._initialized:
            np.copyto(self._mlofi_ema, mlofi)
            self._initialized = True
        else:
            self._mlofi_ema += _EMA_FAST * (mlofi - self._mlofi_ema)
        # CONVEXITY ONLY — no gradient
        convexity = float(np.dot(_CONVEXITY_WEIGHTS, self._mlofi_ema)) / _CONVEXITY_DENOM
        self._conv_ema += _EMA_OUTPUT * (convexity - self._conv_ema)
        if self._tick_count < _MG_WARMUP:
            self._signal = 0.0
        else:
            self._signal = max(-_SIGNAL_CLIP, min(_SIGNAL_CLIP, self._conv_ema))
        return self._signal


# ============================================================
# Replay helpers
# ============================================================

def _replay_alpha(alpha: Any, data: np.ndarray) -> np.ndarray:
    n = len(data)
    signals = np.empty(n, dtype=np.float64)
    for i in range(n):
        bids = np.column_stack([data[i]["bids_price"], data[i]["bids_vol"]]).astype(np.float64)
        asks = np.column_stack([data[i]["asks_price"], data[i]["asks_vol"]]).astype(np.float64)
        signals[i] = alpha.update(bids=bids, asks=asks)
    return signals


def _compute_fwd_returns(timestamps: np.ndarray, mid: np.ndarray, day_segs: list[tuple[int, int]]) -> dict[str, np.ndarray]:
    n = len(timestamps)
    results: dict[str, np.ndarray] = {}
    for horizon_ns, label in zip(_HORIZONS_NS, _HORIZON_LABELS):
        fwd = np.full(n, np.nan, dtype=np.float64)
        target_offset = _LATENCY_NS + horizon_ns
        for ds, de in day_segs:
            j = ds
            for i in range(ds, de):
                target_ts = timestamps[i] + target_offset
                while j < de and timestamps[j] < target_ts:
                    j += 1
                if j < de and mid[i] > 0:
                    fwd[i] = (mid[j] - mid[i]) / mid[i]
        results[label] = fwd
    return results


# ============================================================
# Main
# ============================================================

def run_supplementary(
    symbols: list[str], data_dir: Path, output_dir: Path,
) -> dict[str, Any]:
    output_dir.mkdir(parents=True, exist_ok=True)
    report: dict[str, Any] = {
        "created_at": datetime.now(UTC).isoformat(),
        "analyses": {},
    }

    for sym in symbols:
        npy_path = data_dir / f"{sym}_l5.npy"
        if not npy_path.exists():
            continue

        data = np.load(str(npy_path))
        timestamps = data["timestamp_ns"]
        bp_l1 = data["bids_price"][:, 0].astype(np.float64)
        ap_l1 = data["asks_price"][:, 0].astype(np.float64)
        mid = (bp_l1 + ap_l1) / 2.0
        day_segs = _split_days(timestamps)
        fwd_returns = _compute_fwd_returns(timestamps, mid, day_segs)

        sym_report: dict[str, Any] = {"n_rows": len(data), "n_days": len(day_segs)}
        log = logger.bind(symbol=sym, n_days=len(day_segs))

        # --- Replay all alpha variants ---
        alpha_variants: dict[str, tuple[Any, int]] = {
            "mlofi_gradient": (MlofiGradientAlpha(), _MG_WARMUP),
            "book_convexity": (BookConvexityAlpha(), _BC_WARMUP),
            "gradient_only": (_GradientOnlyAlpha(), _MG_WARMUP),
            "convexity_only": (_ConvexityOnlyAlpha(), _MG_WARMUP),
        }

        signals_cache: dict[str, np.ndarray] = {}
        for vname, (alpha, warmup) in alpha_variants.items():
            log.info("replaying", variant=vname)
            t0 = time.perf_counter()
            signals = _replay_alpha(alpha, data)
            elapsed = time.perf_counter() - t0
            signals_cache[vname] = signals
            log.info("replay_done", variant=vname, time_s=f"{elapsed:.1f}")

        # --- Analysis 1: Block bootstrap IC ---
        log.info("analysis_1_block_bootstrap")
        bootstrap_results: dict[str, dict[str, Any]] = {}
        for aname in ["mlofi_gradient", "book_convexity"]:
            warmup = _MG_WARMUP if aname == "mlofi_gradient" else _BC_WARMUP
            s = signals_cache[aname][warmup:]
            bsr: dict[str, Any] = {}
            for label in _HORIZON_LABELS:
                r = fwd_returns[label][warmup:]
                bsr[label] = _block_bootstrap_ic(s, r, n_bootstrap=500, block_size=200)
            bootstrap_results[aname] = bsr
        sym_report["block_bootstrap_ic"] = bootstrap_results

        # --- Analysis 2: IS/OOS split ---
        log.info("analysis_2_is_oos")
        isoos_results: dict[str, Any] = {}
        for aname in ["mlofi_gradient", "book_convexity"]:
            warmup = _MG_WARMUP if aname == "mlofi_gradient" else _BC_WARMUP
            isoos_results[aname] = _is_oos_ic(
                signals_cache[aname], fwd_returns, day_segs, warmup, is_days=7,
            )
        sym_report["is_oos_split"] = isoos_results

        # --- Analysis 3: Component isolation ---
        log.info("analysis_3_component_isolation")
        component_ic: dict[str, dict[str, float]] = {}
        for vname in ["mlofi_gradient", "gradient_only", "convexity_only"]:
            s = signals_cache[vname][_MG_WARMUP:]
            vic: dict[str, float] = {}
            for label in _HORIZON_LABELS:
                r = fwd_returns[label][_MG_WARMUP:]
                vic[label] = _rank_ic(s, r)
            component_ic[vname] = vic
        sym_report["component_isolation"] = component_ic

        # --- Analysis 4: book_convexity corrected assessment ---
        # Already covered by block_bootstrap_ic above

        report["analyses"][sym] = sym_report

    # Save JSON
    json_path = output_dir / "stage4b_supplementary_data.json"
    json_path.write_text(json.dumps(report, indent=2, default=str), encoding="utf-8")
    logger.info("json_saved", path=str(json_path))

    # Print summary
    _print_summary(report)

    return report


def _print_summary(report: dict[str, Any]) -> None:
    print("\n" + "=" * 90)
    print("STAGE 4B: SUPPLEMENTARY STATISTICS")
    print("=" * 90)

    for sym, sr in report["analyses"].items():
        print(f"\n{'='*40} {sym} ({sr['n_rows']:,} rows, {sr['n_days']} days) {'='*40}")

        # 1. Block bootstrap
        print("\n--- 1. Block Bootstrap IC (autocorrelation-corrected) ---")
        print(f"{'Alpha':>20s}  ", end="")
        for label in _HORIZON_LABELS:
            print(f"{'IC_' + label:>12s}", end="")
        print(f"  {'t-corr':>8s}")

        for aname in ["mlofi_gradient", "book_convexity"]:
            bs = sr["block_bootstrap_ic"][aname]
            print(f"{aname:>20s}  ", end="")
            for label in _HORIZON_LABELS:
                d = bs[label]
                ic = d["ic"]
                tstat = d["ic_tstat_corrected"]
                sig = "*" if abs(tstat) > 2.0 else " "
                print(f"{ic:>+10.4f}{sig} ", end="")
            # Show t-stat for 1s horizon
            t1s = bs["1s"]["ic_tstat_corrected"]
            print(f"  {t1s:>+8.2f}")

        # 2. IS/OOS
        print("\n--- 2. In-Sample / Out-of-Sample Split ---")
        for aname in ["mlofi_gradient", "book_convexity"]:
            isoos = sr["is_oos_split"][aname]
            is_d = isoos["is_days"]
            oos_d = isoos["oos_days"]
            print(f"  {aname} (IS={is_d}d, OOS={oos_d}d):")
            print(f"    {'':>6s}", end="")
            for label in _HORIZON_LABELS:
                print(f"{'IC_' + label:>10s}", end="")
            print()
            for split in ["in_sample", "out_of_sample"]:
                tag = "IS " if split == "in_sample" else "OOS"
                print(f"    {tag:>6s}", end="")
                for label in _HORIZON_LABELS:
                    ic = isoos[split][label]
                    print(f"{ic:>+10.4f}", end="")
                print()

        # 3. Component isolation
        print("\n--- 3. Component Isolation (mlofi_gradient) ---")
        print(f"{'Component':>20s}  ", end="")
        for label in _HORIZON_LABELS:
            print(f"{'IC_' + label:>10s}", end="")
        print()
        for vname in ["mlofi_gradient", "gradient_only", "convexity_only"]:
            comp = sr["component_isolation"][vname]
            print(f"{vname:>20s}  ", end="")
            for label in _HORIZON_LABELS:
                print(f"{comp[label]:>+10.4f}", end="")
            print()


def main() -> int:
    parser = argparse.ArgumentParser(description="Stage 4b supplementary analysis.")
    parser.add_argument("--symbols", default="2330,2317,TXFD6")
    parser.add_argument("--data-dir", default="research/data/l5/")
    parser.add_argument("--out", default="outputs/team_artifacts/alpha-research/")
    args = parser.parse_args()

    symbols = [s.strip() for s in args.symbols.split(",") if s.strip()]
    run_supplementary(symbols=symbols, data_dir=Path(args.data_dir), output_dir=Path(args.out))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
