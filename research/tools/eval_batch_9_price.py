"""Batch 9 — Price & Micro alpha evaluation.

Evaluates 4 alphas against real TWSE tick data:
  - microprice_reversion
  - price_level_revert
  - alpha_mid_price_v1
  - shap_microstructure

Usage:
    uv run python research/tools/eval_batch_9_price.py
"""
from __future__ import annotations

import json
import math
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import numpy as np

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------
ROOT = Path(__file__).resolve().parent.parent.parent
# Worktrees may not contain raw data; fall back to main repo.
_MAIN_REPO = Path("/home/charlie/hft_platform")
DATA_DIR = ROOT / "research" / "data" / "raw"
if not (DATA_DIR / "txfc6" / "TXFC6_all_l1.npy").exists():
    DATA_DIR = _MAIN_REPO / "research" / "data" / "raw"

PRIMARY_DATA = DATA_DIR / "txfc6" / "TXFC6_all_l1.npy"
CROSS_DATA = {
    "2330": DATA_DIR / "2330" / "2330_all_l1.npy",
    "2317": DATA_DIR / "2317" / "2317_all_l1.npy",
    "MXFC6": DATA_DIR / "mxfc6" / "MXFC6_all_l1.npy",
}

SCORECARD_DIR = ROOT / "research" / "alphas"

# ---------------------------------------------------------------------------
# EMA helper
# ---------------------------------------------------------------------------
_EMA_ALPHA_8 = 1.0 - math.exp(-1.0 / 8.0)


def _ema_vec(arr: np.ndarray, alpha: float) -> np.ndarray:
    out = np.zeros_like(arr, dtype=np.float64)
    out[0] = arr[0]
    for i in range(1, len(arr)):
        out[i] = out[i - 1] + alpha * (arr[i] - out[i - 1])
    return out


# ---------------------------------------------------------------------------
# Data loading and field enrichment
# ---------------------------------------------------------------------------
def load_and_enrich(path: Path, max_rows: int = 0) -> dict[str, np.ndarray]:
    """Load an _all_l1.npy structured array and derive all needed fields."""
    raw = np.load(str(path), allow_pickle=True)
    if max_rows > 0:
        raw = raw[:max_rows]
    n = len(raw)

    bid_px = raw["bid_px"].astype(np.float64)
    ask_px = raw["ask_px"].astype(np.float64)
    bid_qty = raw["bid_qty"].astype(np.float64)
    ask_qty = raw["ask_qty"].astype(np.float64)
    mid_price = raw["mid_price"].astype(np.float64)

    # Scaled int fields (x10000 convention but these are already in index-point
    # units for futures; we use x2 for mid_price_x2 which is 2*mid in scaled)
    mid_price_x2 = (2.0 * mid_price).astype(np.float64)
    spread_scaled = (ask_px - bid_px).astype(np.float64)

    # Microprice (volume-weighted mid)
    total_qty = bid_qty + ask_qty
    safe_total = np.where(total_qty > 0, total_qty, 1.0)
    microprice = (bid_px * ask_qty + ask_px * bid_qty) / safe_total
    microprice_x2 = (2.0 * microprice).astype(np.float64)

    # L1 imbalance in PPM
    l1_imbalance_ppm = np.where(
        total_qty > 0,
        ((bid_qty - ask_qty) / total_qty * 1_000_000).astype(np.float64),
        0.0,
    )

    # OFI L1 raw (delta bid_qty - delta ask_qty)
    ofi_l1_raw = np.zeros(n, dtype=np.float64)
    ofi_l1_raw[1:] = np.diff(bid_qty) - np.diff(ask_qty)

    fields: dict[str, np.ndarray] = {
        "bid_px": bid_px,
        "ask_px": ask_px,
        "bid_qty": bid_qty,
        "ask_qty": ask_qty,
        "mid_price": mid_price,
        "mid_price_x2": mid_price_x2,
        "spread_scaled": spread_scaled,
        "microprice_x2": microprice_x2,
        "l1_imbalance_ppm": l1_imbalance_ppm,
        "depth_imbalance_ppm": l1_imbalance_ppm,
        "ofi_l1_raw": ofi_l1_raw,
        "l1_bid_qty": bid_qty,
        "l1_ask_qty": ask_qty,
    }

    # EMA enrichments
    fields["ofi_l1_ema8"] = _ema_vec(fields["ofi_l1_raw"], _EMA_ALPHA_8)
    fields["spread_ema8_scaled"] = _ema_vec(fields["spread_scaled"], _EMA_ALPHA_8)
    fields["depth_imbalance_ema8_ppm"] = _ema_vec(fields["l1_imbalance_ppm"], _EMA_ALPHA_8)
    fields["bid_depth"] = bid_qty
    fields["ask_depth"] = ask_qty
    fields["current_return"] = np.concatenate(
        [[0.0], np.diff(np.log(np.maximum(mid_price, 1e-12)))]
    )
    fields["price"] = mid_price

    return fields


# ---------------------------------------------------------------------------
# Generic alpha runner
# ---------------------------------------------------------------------------
def run_alpha(alpha: Any, fields: dict[str, np.ndarray], n: int) -> np.ndarray:
    data_field_names = alpha.manifest.data_fields
    out = np.zeros(n, dtype=np.float64)
    alpha.reset()
    for i in range(n):
        args = [fields[f][i] for f in data_field_names]
        out[i] = alpha.update(*args)
    return out


# ---------------------------------------------------------------------------
# Metrics
# ---------------------------------------------------------------------------
def compute_forward_returns(mid_price: np.ndarray, horizon: int = 20) -> np.ndarray:
    """Forward log-return at given horizon (vectorized)."""
    n = len(mid_price)
    fwd = np.zeros(n, dtype=np.float64)
    safe_mid = np.maximum(mid_price, 1e-12)
    fwd[: n - horizon] = np.log(safe_mid[horizon:] / safe_mid[: n - horizon])
    return fwd


def compute_ic(signal: np.ndarray, fwd_ret: np.ndarray, warmup: int = 200) -> float:
    """Rank IC (Spearman) between signal and forward returns."""
    from scipy.stats import spearmanr

    s = signal[warmup:]
    f = fwd_ret[warmup:]
    # Trim last horizon ticks where fwd_ret is 0
    valid = f != 0.0
    if valid.sum() < 100:
        return 0.0
    corr, _ = spearmanr(s[valid], f[valid])
    return float(corr) if np.isfinite(corr) else 0.0


def compute_sharpe(signal: np.ndarray, fwd_ret: np.ndarray, warmup: int = 200) -> float:
    """Signal-weighted return Sharpe (annualized assuming 5h/day, 250 days)."""
    s = signal[warmup:]
    f = fwd_ret[warmup:]
    pnl = s * f
    if len(pnl) < 100 or np.std(pnl) < 1e-15:
        return 0.0
    # Per-tick Sharpe, annualize with sqrt(ticks_per_year)
    # ~6M ticks/day for futures, 250 days
    ticks_per_year = 6_000_000 * 250
    raw_sharpe = np.mean(pnl) / np.std(pnl)
    return float(raw_sharpe * math.sqrt(ticks_per_year))


def compute_max_drawdown(signal: np.ndarray, fwd_ret: np.ndarray, warmup: int = 200) -> float:
    """Max drawdown of cumulative PnL."""
    s = signal[warmup:]
    f = fwd_ret[warmup:]
    cum_pnl = np.cumsum(s * f)
    if len(cum_pnl) == 0:
        return 0.0
    running_max = np.maximum.accumulate(cum_pnl)
    drawdown = running_max - cum_pnl
    total_range = cum_pnl[-1] - cum_pnl[0] if abs(cum_pnl[-1] - cum_pnl[0]) > 1e-15 else 1.0
    return float(np.max(drawdown) / abs(total_range)) if np.max(drawdown) > 0 else 0.0


def compute_turnover(signal: np.ndarray, warmup: int = 200) -> float:
    """Average absolute signal change (proxy for turnover)."""
    s = signal[warmup:]
    if len(s) < 2:
        return 0.0
    return float(np.mean(np.abs(np.diff(s))))


# ---------------------------------------------------------------------------
# Alpha instantiation
# ---------------------------------------------------------------------------
def load_alphas() -> list[tuple[str, Any]]:
    """Load all 4 alpha instances."""
    sys.path.insert(0, str(ROOT))
    alphas = []

    from research.alphas.microprice_reversion.impl import ALPHA_CLASS as MR
    alphas.append(("microprice_reversion", MR()))

    from research.alphas.price_level_revert.impl import ALPHA_CLASS as PLR
    alphas.append(("price_level_revert", PLR()))

    from research.alphas.alpha_mid_price_v1.impl import ALPHA_CLASS as AMP
    alphas.append(("alpha_mid_price_v1", AMP()))

    try:
        from research.alphas.shap_microstructure.impl import ALPHA_CLASS as SM
        alphas.append(("shap_microstructure", SM()))
    except ImportError as e:
        print(f"[WARN] shap_microstructure import failed: {e}")

    return alphas


# ---------------------------------------------------------------------------
# Tier classification
# ---------------------------------------------------------------------------
def classify_tier(ic_oos: float, sharpe: float) -> str:
    if ic_oos > 0.02 and sharpe > 2.0:
        return "Star"
    if ic_oos > 0.005 or sharpe > 0.5:
        return "Promising"
    if ic_oos > 0.002 or sharpe > 0.2:
        return "Marginal"
    return "Failed"


# ---------------------------------------------------------------------------
# Scorecard dataclass
# ---------------------------------------------------------------------------
@dataclass
class EvalResult:
    alpha_id: str
    ic_is: float = 0.0
    ic_oos: float = 0.0
    sharpe_is: float = 0.0
    sharpe_oos: float = 0.0
    max_drawdown: float = 0.0
    turnover: float = 0.0
    tier: str = "Failed"
    cross_val: dict[str, dict[str, float]] = field(default_factory=dict)
    error: str | None = None


# ---------------------------------------------------------------------------
# Main evaluation
# ---------------------------------------------------------------------------
def evaluate_alpha(
    alpha_id: str,
    alpha: Any,
    fields: dict[str, np.ndarray],
    oos_split: float = 0.7,
) -> EvalResult:
    """Run signal, compute IS/OOS metrics, classify tier."""
    n = len(fields["mid_price"])
    result = EvalResult(alpha_id=alpha_id)

    try:
        signal = run_alpha(alpha, fields, n)
    except Exception as e:
        result.error = str(e)
        return result

    fwd_ret = compute_forward_returns(fields["mid_price"], horizon=20)

    split_idx = int(n * oos_split)

    # IS
    result.ic_is = compute_ic(signal[:split_idx], fwd_ret[:split_idx])
    result.sharpe_is = compute_sharpe(signal[:split_idx], fwd_ret[:split_idx])

    # OOS
    result.ic_oos = compute_ic(signal[split_idx:], fwd_ret[split_idx:], warmup=0)
    result.sharpe_oos = compute_sharpe(signal[split_idx:], fwd_ret[split_idx:], warmup=0)

    # Full-sample
    result.max_drawdown = compute_max_drawdown(signal, fwd_ret)
    result.turnover = compute_turnover(signal)

    result.tier = classify_tier(result.ic_oos, result.sharpe_oos)
    return result


def cross_validate(
    alpha_id: str,
    alpha_cls: type,
    cross_datasets: dict[str, Path],
) -> dict[str, dict[str, float]]:
    """Run IC/Sharpe on cross-validation datasets."""
    results: dict[str, dict[str, float]] = {}
    for name, path in cross_datasets.items():
        if not path.exists():
            results[name] = {"error": -999.0}
            continue
        try:
            fields = load_and_enrich(path)
            alpha = alpha_cls()
            n = len(fields["mid_price"])
            signal = run_alpha(alpha, fields, n)
            fwd_ret = compute_forward_returns(fields["mid_price"], horizon=20)
            ic = compute_ic(signal, fwd_ret)
            sharpe = compute_sharpe(signal, fwd_ret)
            results[name] = {"ic": round(ic, 6), "sharpe": round(sharpe, 2)}
        except Exception as e:
            results[name] = {"error": -999.0, "msg": str(e)[:200]}
    return results


def write_scorecard(result: EvalResult, alpha_dir: Path) -> None:
    """Write scorecard.json for the alpha."""
    scorecard = {
        "alpha_id": result.alpha_id,
        "sharpe_is": round(result.sharpe_is, 4),
        "sharpe_oos": round(result.sharpe_oos, 4),
        "ic_mean": round(result.ic_oos, 6),
        "ic_is": round(result.ic_is, 6),
        "max_drawdown": round(result.max_drawdown, 6),
        "turnover": round(result.turnover, 8),
        "tier": result.tier,
        "data_source": "TXFC6_all_l1.npy",
        "cross_validation": result.cross_val,
        "error": result.error,
    }
    out_path = alpha_dir / result.alpha_id / "scorecard.json"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with open(out_path, "w") as f:
        json.dump(scorecard, f, indent=2, default=str)
    print(f"  Scorecard written: {out_path}")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------
def main() -> None:
    print("=" * 72)
    print("Batch 9 — Price & Micro Alpha Evaluation")
    print("=" * 72)

    if not PRIMARY_DATA.exists():
        print(f"[ERROR] Primary data not found: {PRIMARY_DATA}")
        sys.exit(1)

    print(f"\nLoading primary data: {PRIMARY_DATA.name}")
    fields = load_and_enrich(PRIMARY_DATA)
    n = len(fields["mid_price"])
    print(f"  Ticks loaded: {n:,}")

    alphas = load_alphas()
    print(f"  Alphas loaded: {len(alphas)}")

    # Map alpha_id -> class for cross-validation
    alpha_classes: dict[str, type] = {}
    for aid, a in alphas:
        alpha_classes[aid] = type(a)

    results: list[EvalResult] = []

    for alpha_id, alpha in alphas:
        print(f"\n--- {alpha_id} ---")
        print(f"  data_fields: {alpha.manifest.data_fields}")

        result = evaluate_alpha(alpha_id, alpha, fields)
        if result.error:
            print(f"  [ERROR] {result.error}")
        else:
            print(f"  IC_IS={result.ic_is:.6f}  IC_OOS={result.ic_oos:.6f}")
            print(f"  Sharpe_IS={result.sharpe_is:.2f}  Sharpe_OOS={result.sharpe_oos:.2f}")
            print(f"  MaxDD={result.max_drawdown:.6f}  Turnover={result.turnover:.8f}")
            print(f"  Tier: {result.tier}")

        # Cross-validate promising+ alphas
        if result.tier in ("Star", "Promising", "Marginal"):
            print("  Running cross-validation...")
            avail_cross = {
                k: v for k, v in CROSS_DATA.items() if v.exists()
            }
            if avail_cross:
                result.cross_val = cross_validate(
                    alpha_id, alpha_classes[alpha_id], avail_cross
                )
                for ds, cv in result.cross_val.items():
                    print(f"    {ds}: {cv}")

        results.append(result)
        write_scorecard(result, SCORECARD_DIR)

    # Summary table
    print("\n" + "=" * 72)
    print("SUMMARY")
    print("=" * 72)
    print(f"{'Alpha':<25} {'IC_OOS':>10} {'Sharpe_OOS':>12} {'Tier':>12}")
    print("-" * 60)
    for r in results:
        if r.error:
            print(f"{r.alpha_id:<25} {'ERROR':>10} {'':>12} {'Failed':>12}")
        else:
            print(f"{r.alpha_id:<25} {r.ic_oos:>10.6f} {r.sharpe_oos:>12.2f} {r.tier:>12}")
    print("=" * 72)


if __name__ == "__main__":
    main()
