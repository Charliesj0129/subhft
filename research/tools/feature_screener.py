"""Feature screener: ranks LOB features by IC/Sharpe for alpha research."""
from __future__ import annotations

import argparse
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import numpy as np
import structlog

logger = structlog.get_logger(__name__)


def _ensure_project_root_on_path() -> None:
    root = Path(__file__).resolve().parents[2]
    if (root / "research").exists():
        root_str = str(root)
        if root_str not in sys.path:
            sys.path.insert(0, root_str)


_ensure_project_root_on_path()

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
_FORWARD_HORIZON: int = 5   # ticks ahead for forward-return computation
_MIN_OBS: int = 50           # minimum usable observations after warmup/NaN drop


# ---------------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------------

@dataclass(frozen=True, slots=True)
class FeatureResult:
    feature_id: str
    ic: float
    sharpe: float
    turnover: float
    score: float    # |ic| * |sharpe|
    n_obs: int


@dataclass(frozen=True, slots=True)
class InteractionResult:
    feature_a: str
    feature_b: str
    interaction: str  # "product" | "ratio"
    ic: float
    n_obs: int


# ---------------------------------------------------------------------------
# Data loading
# ---------------------------------------------------------------------------

def _load_data(path: str) -> np.ndarray:
    """Load .npy or .npz to a float64 2-D array."""
    p = Path(path)
    if not p.exists():
        raise FileNotFoundError(f"Data file not found: {path}")
    if p.suffix == ".npz":
        npz = np.load(p, allow_pickle=False)
        arr = npz[list(npz.files)[0]].astype(np.float64)
    elif p.suffix == ".npy":
        raw = np.load(p, allow_pickle=True)
        if raw.dtype.names is not None:
            arr = np.column_stack([raw[f].astype(np.float64) for f in raw.dtype.names])
        else:
            arr = raw.astype(np.float64)
    else:
        raise ValueError(f"Unsupported extension: {p.suffix}. Use .npy or .npz")
    return arr.reshape(-1, 1) if arr.ndim == 1 else arr


def _stack_data(data_paths: list[str]) -> np.ndarray:
    """Load and row-stack arrays from multiple paths, skipping bad files."""
    arrays: list[np.ndarray] = []
    for path in data_paths:
        try:
            arrays.append(_load_data(path))
        except (FileNotFoundError, ValueError, KeyError) as exc:
            logger.warning("skipping_data_file", path=path, reason=str(exc))
    if not arrays:
        raise RuntimeError("No valid data files could be loaded.")
    n_cols = arrays[0].shape[1]
    compatible = [a for a in arrays if a.shape[1] == n_cols]
    if not compatible:
        raise RuntimeError("No compatible arrays after column-count filtering.")
    if len(compatible) < len(arrays):
        logger.warning("dropped_incompatible_files", expected_cols=n_cols,
                       dropped=len(arrays) - len(compatible))
    return np.vstack(compatible)


# ---------------------------------------------------------------------------
# Signal metrics
# ---------------------------------------------------------------------------

def _forward_returns(prices: np.ndarray, horizon: int = _FORWARD_HORIZON) -> np.ndarray:
    fwd = np.full(len(prices), np.nan)
    base = prices[:-horizon].astype(np.float64)
    safe = prices[horizon:].astype(np.float64)
    mask = base != 0.0
    fwd[:-horizon][mask] = np.log(safe[mask] / base[mask])
    return fwd


def _ic(signal: np.ndarray, returns: np.ndarray) -> float:
    mask = np.isfinite(signal) & np.isfinite(returns)
    if mask.sum() < _MIN_OBS:
        return float("nan")
    s, r = signal[mask], returns[mask]
    if s.std() < 1e-12 or r.std() < 1e-12:
        return 0.0
    return float(np.corrcoef(s, r)[0, 1])


def _sharpe(signal: np.ndarray, returns: np.ndarray) -> float:
    mask = np.isfinite(signal) & np.isfinite(returns)
    if mask.sum() < _MIN_OBS:
        return float("nan")
    pnl = np.sign(signal[mask]) * returns[mask]
    std = pnl.std()
    if std < 1e-12:
        return 0.0
    return float(pnl.mean() / std * np.sqrt(len(pnl)))


def _turnover(signal: np.ndarray) -> float:
    s = signal[np.isfinite(signal)]
    if len(s) < _MIN_OBS:
        return float("nan")
    return float(np.abs(np.diff(np.sign(s))).mean())


def _price_col(arr: np.ndarray, n_features: int) -> int:
    """Heuristic: use column index 2 (mid_price_x2) within feature columns."""
    if arr.shape[1] > n_features:
        extras = np.arange(n_features, arr.shape[1])
        return int(extras[np.array([arr[:, c].var() for c in extras]).argmax()])
    return min(2, arr.shape[1] - 1)


# ---------------------------------------------------------------------------
# Feature ID list
# ---------------------------------------------------------------------------

def _feature_ids() -> list[str]:
    try:
        from src.hft_platform.feature.registry import build_default_lob_feature_set_v1
        return list(build_default_lob_feature_set_v1().feature_ids)
    except ImportError:
        logger.warning("feature_registry_import_failed", reason="using hardcoded list")
        return [
            "best_bid", "best_ask", "mid_price_x2", "spread_scaled",
            "bid_depth", "ask_depth", "depth_imbalance_ppm", "microprice_x2",
            "l1_bid_qty", "l1_ask_qty", "l1_imbalance_ppm",
            "ofi_l1_raw", "ofi_l1_cum", "ofi_l1_ema8",
            "spread_ema8_scaled", "depth_imbalance_ema8_ppm",
        ]


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def screen_features(
    data_paths: list[str],
    latency_profile: str = "shioaji_sim_p95_v2026-03-04",
) -> list[dict[str, Any]]:
    """Screen all LOB features by IC and Sharpe.

    Loads data from *data_paths*, computes forward returns, then for each
    feature column computes IC, Sharpe, and turnover.

    Returns:
        List of dicts (feature_id, ic, sharpe, turnover, score, n_obs,
        latency_profile) sorted by score descending.
    """
    logger.info("screen_features_start", n_paths=len(data_paths),
                latency_profile=latency_profile)
    arr = _stack_data(data_paths)
    fids = _feature_ids()
    n_feat = min(len(fids), arr.shape[1])
    if arr.shape[1] < len(fids):
        logger.warning("fewer_columns_than_features", n_cols=arr.shape[1],
                       n_features=len(fids), note="screening available columns only")
    fids = fids[:n_feat]

    pc = _price_col(arr, n_feat)
    fwd = _forward_returns(arr[:, pc])

    results: list[FeatureResult] = []
    for idx, fid in enumerate(fids):
        sig = arr[:, idx]
        ic_v = _ic(sig, fwd)
        sh_v = _sharpe(sig, fwd)
        to_v = _turnover(sig)
        n = int((np.isfinite(sig) & np.isfinite(fwd)).sum())
        if np.isnan(ic_v) or np.isnan(sh_v):
            logger.warning("feature_insufficient_data", feature_id=fid,
                           n_obs=n, min_required=_MIN_OBS)
            score = float("nan")
        else:
            score = abs(ic_v) * abs(sh_v)
        results.append(FeatureResult(fid, ic_v, sh_v, to_v, score, n))

    results.sort(key=lambda r: (not np.isfinite(r.score),
                                -r.score if np.isfinite(r.score) else 0.0))
    logger.info("screen_features_complete", n_results=len(results))
    return [
        {"feature_id": r.feature_id, "ic": r.ic, "sharpe": r.sharpe,
         "turnover": r.turnover, "score": r.score, "n_obs": r.n_obs,
         "latency_profile": latency_profile}
        for r in results
    ]


def screen_interactions(
    data_paths: list[str],
    top_k: int = 5,
) -> list[dict[str, Any]]:
    """Test pairwise feature interactions (product and ratio) for top-K features.

    Returns:
        List of dicts (feature_a, feature_b, interaction, ic, n_obs)
        sorted by |ic| descending.
    """
    logger.info("screen_interactions_start", top_k=top_k)
    ranked = [r for r in screen_features(data_paths) if np.isfinite(r["score"])]
    top = ranked[:top_k]
    if len(top) < 2:
        logger.warning("not_enough_features_for_interactions", available=len(top))
        return []

    arr = _stack_data(data_paths)
    fids = _feature_ids()
    n_feat = min(len(fids), arr.shape[1])
    fids = fids[:n_feat]
    id2col = {fid: i for i, fid in enumerate(fids)}
    fwd = _forward_returns(arr[:, _price_col(arr, n_feat)])

    interactions: list[InteractionResult] = []
    top_ids = [r["feature_id"] for r in top]
    for i in range(len(top_ids)):
        for j in range(i + 1, len(top_ids)):
            a, b = top_ids[i], top_ids[j]
            ca, cb = id2col.get(a), id2col.get(b)
            if ca is None or cb is None:
                continue
            sa, sb = arr[:, ca], arr[:, cb]

            # Product
            prod = sa * sb
            ic_p = _ic(prod, fwd)
            n_p = int((np.isfinite(prod) & np.isfinite(fwd)).sum())
            interactions.append(InteractionResult(a, b, "product", ic_p, n_p))

            # Ratio
            denom = sb.copy()
            denom[np.abs(denom) < 1e-9] = np.nan
            ratio = sa / denom
            ic_r = _ic(ratio, fwd)
            n_r = int((np.isfinite(ratio) & np.isfinite(fwd)).sum())
            interactions.append(InteractionResult(a, b, "ratio", ic_r, n_r))

    interactions.sort(key=lambda r: (not np.isfinite(r.ic),
                                     -abs(r.ic) if np.isfinite(r.ic) else 0.0))
    logger.info("screen_interactions_complete", n_interactions=len(interactions))
    return [{"feature_a": r.feature_a, "feature_b": r.feature_b,
             "interaction": r.interaction, "ic": r.ic, "n_obs": r.n_obs}
            for r in interactions]


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _fmt(v: float, w: int = 8, p: int = 4) -> str:
    return f"{'NaN':>{w}}" if not np.isfinite(v) else f"{v:>{w}.{p}f}"


def _print_feature_table(results: list[dict[str, Any]], top_n: int) -> None:
    hdr = f"{'Rank':<5}  {'Feature ID':<28}  {'IC':>8}  {'Sharpe':>8}  {'Turnover':>8}  {'Score':>8}  {'N':>6}"
    sep = "-" * len(hdr)
    print(sep)
    print(hdr)
    print(sep)
    for rank, row in enumerate(results[:top_n], 1):
        print(f"{rank:<5}  {row['feature_id']:<28}  {_fmt(row['ic'])}  "
              f"{_fmt(row['sharpe'])}  {_fmt(row['turnover'])}  "
              f"{_fmt(row['score'])}  {row['n_obs']:>6}")
    print(sep)


def _print_interaction_table(results: list[dict[str, Any]], top_n: int) -> None:
    hdr = f"{'Rank':<5}  {'Feature A':<28}  {'x':<3}  {'Feature B':<28}  {'Type':<8}  {'IC':>8}  {'N':>6}"
    sep = "-" * len(hdr)
    print(sep)
    print(hdr)
    print(sep)
    for rank, row in enumerate(results[:top_n], 1):
        op = "*" if row["interaction"] == "product" else "/"
        print(f"{rank:<5}  {row['feature_a']:<28}  {op:<3}  {row['feature_b']:<28}  "
              f"{row['interaction']:<8}  {_fmt(row['ic'])}  {row['n_obs']:>6}")
    print(sep)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="feature_screener",
        description="Screen LOB features by IC/Sharpe ranking",
    )
    parser.add_argument("--data", nargs="+", required=True, metavar="PATH",
                        help="Path(s) to .npy/.npz data files")
    parser.add_argument("--top", type=int, default=10, metavar="N",
                        help="Top N features to display (default: 10)")
    parser.add_argument("--interactions", action="store_true",
                        help="Also run pairwise interaction screening")
    parser.add_argument("--top-k-interactions", dest="top_k", type=int,
                        default=5, metavar="K",
                        help="Top-K features for interaction pairs (default: 5)")
    parser.add_argument("--latency-profile", default="shioaji_sim_p95_v2026-03-04",
                        metavar="ID", help="Latency profile ID")
    args = parser.parse_args(argv)

    print(f"\nFeature Screener  |  latency_profile={args.latency_profile}")
    print(f"Data files: {', '.join(args.data)}\n")

    try:
        results = screen_features(args.data, latency_profile=args.latency_profile)
    except (RuntimeError, FileNotFoundError) as exc:
        logger.error("screen_features_failed", error=str(exc))
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1

    print(f"=== Feature Rankings (top {args.top}) ===")
    _print_feature_table(results, args.top)

    if args.interactions:
        print(f"\n=== Pairwise Interactions (top-{args.top_k} features, top {args.top}) ===")
        try:
            iresults = screen_interactions(args.data, top_k=args.top_k)
        except (RuntimeError, FileNotFoundError) as exc:
            logger.error("screen_interactions_failed", error=str(exc))
            print(f"ERROR in interactions: {exc}", file=sys.stderr)
            return 1
        _print_interaction_table(iresults, args.top)

    return 0


if __name__ == "__main__":
    sys.exit(main())
