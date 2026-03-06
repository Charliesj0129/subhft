from __future__ import annotations

import argparse
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import numpy as np
import polars as pl
from scipy.interpolate import PchipInterpolator

_METHODS: tuple[str, ...] = ("linear", "pchip", "kalman", "ar1", "harmonic")


@dataclass(slots=True)
class RepairConfig:
    input_paths: tuple[Path, ...]
    output_path: Path
    report_path: Path
    target_ms: int = 1_000
    cv_ratio: float = 0.1
    harmonics: int = 6
    seed: int = 42


def _as_float_array(series: pl.Series) -> np.ndarray:
    return series.cast(pl.Float64).to_numpy()


def _ensure_parent(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)


def _nanmean_fallback(y: np.ndarray, fallback: float = 0.0) -> float:
    if y.size == 0:
        return float(fallback)
    with np.errstate(all="ignore"):
        m = float(np.nanmean(y))
    if np.isnan(m):
        return float(fallback)
    return m


def _linear_fill(y: np.ndarray, obs_mask: np.ndarray) -> np.ndarray:
    n = y.size
    out = np.asarray(y, dtype=np.float64).copy()
    if n == 0:
        return out
    idx = np.arange(n, dtype=np.int64)
    valid = np.where(obs_mask)[0]
    if valid.size == 0:
        out[:] = 0.0
        return out
    if valid.size == 1:
        out[:] = float(y[valid[0]])
        return out
    out[:] = np.interp(idx, valid, y[valid])
    return out


def _pchip_fill(y: np.ndarray, obs_mask: np.ndarray) -> np.ndarray:
    valid = np.where(obs_mask)[0]
    if valid.size < 3:
        return _linear_fill(y, obs_mask)
    x = np.arange(y.size, dtype=np.float64)
    model = PchipInterpolator(valid.astype(np.float64), y[valid].astype(np.float64), extrapolate=True)
    out = model(x)
    return np.asarray(out, dtype=np.float64)


def _kalman_local_level_fill(y: np.ndarray, obs_mask: np.ndarray) -> np.ndarray:
    n = y.size
    if n == 0:
        return np.asarray([], dtype=np.float64)

    obs = y[obs_mask]
    if obs.size <= 1:
        return _linear_fill(y, obs_mask)

    scale = float(np.nanstd(obs))
    if not np.isfinite(scale) or scale <= 0.0:
        scale = 1.0
    q = max(1e-6, 0.01 * scale * scale)
    r = max(1e-6, 0.05 * scale * scale)

    x_filt = np.zeros(n, dtype=np.float64)
    p_filt = np.zeros(n, dtype=np.float64)

    x_prev = float(obs[0])
    p_prev = 1e6

    for t in range(n):
        x_pred = x_prev
        p_pred = p_prev + q
        if obs_mask[t]:
            innovation = y[t] - x_pred
            k = p_pred / (p_pred + r)
            x_curr = x_pred + k * innovation
            p_curr = (1.0 - k) * p_pred
        else:
            x_curr = x_pred
            p_curr = p_pred
        x_filt[t] = x_curr
        p_filt[t] = max(1e-9, p_curr)
        x_prev = x_curr
        p_prev = p_curr

    x_smooth = x_filt.copy()
    for t in range(n - 2, -1, -1):
        p_pred_next = p_filt[t] + q
        c = p_filt[t] / max(p_pred_next, 1e-9)
        x_smooth[t] = x_filt[t] + c * (x_smooth[t + 1] - x_filt[t])

    return x_smooth


def _ar1_fill(y: np.ndarray, obs_mask: np.ndarray) -> np.ndarray:
    baseline = _linear_fill(y, obs_mask)
    obs_idx = np.where(obs_mask)[0]
    if obs_idx.size < 4:
        return baseline

    prev_vals: list[float] = []
    next_vals: list[float] = []
    for i in range(obs_idx.size - 1):
        a = int(obs_idx[i])
        b = int(obs_idx[i + 1])
        if b - a == 1:
            prev_vals.append(float(y[a]))
            next_vals.append(float(y[b]))

    if len(prev_vals) < 3:
        return baseline

    x_prev = np.asarray(prev_vals, dtype=np.float64)
    x_next = np.asarray(next_vals, dtype=np.float64)
    x_mat = np.column_stack([np.ones_like(x_prev), x_prev])
    coef, *_ = np.linalg.lstsq(x_mat, x_next, rcond=None)
    c, phi = float(coef[0]), float(coef[1])

    out = baseline.copy()
    last_val = _nanmean_fallback(y, fallback=baseline[0] if baseline.size else 0.0)
    for i in range(out.size):
        if obs_mask[i]:
            last_val = float(y[i])
            continue
        pred = c + phi * last_val
        out[i] = 0.7 * pred + 0.3 * out[i]
        last_val = float(out[i])
    return out


def _harmonic_fill(y: np.ndarray, obs_mask: np.ndarray, harmonics: int = 6) -> np.ndarray:
    n = y.size
    if n == 0:
        return np.asarray([], dtype=np.float64)
    valid = np.where(obs_mask)[0]
    if valid.size < 4:
        return _linear_fill(y, obs_mask)

    k = int(max(1, min(harmonics, max(1, n // 20))))
    x = np.arange(n, dtype=np.float64)

    cols = [np.ones(n, dtype=np.float64)]
    for j in range(1, k + 1):
        ang = 2.0 * np.pi * float(j) * x / float(max(n, 1))
        cols.append(np.sin(ang))
        cols.append(np.cos(ang))
    mat = np.column_stack(cols)

    mat_obs = mat[valid]
    y_obs = y[valid]
    reg = 1e-3
    lhs = mat_obs.T @ mat_obs + reg * np.eye(mat_obs.shape[1], dtype=np.float64)
    rhs = mat_obs.T @ y_obs
    coef = np.linalg.solve(lhs, rhs)
    out = mat @ coef
    return np.asarray(out, dtype=np.float64)


def _apply_method(name: str, y: np.ndarray, obs_mask: np.ndarray, harmonics: int) -> np.ndarray:
    if name == "linear":
        return _linear_fill(y, obs_mask)
    if name == "pchip":
        return _pchip_fill(y, obs_mask)
    if name == "kalman":
        return _kalman_local_level_fill(y, obs_mask)
    if name == "ar1":
        return _ar1_fill(y, obs_mask)
    if name == "harmonic":
        return _harmonic_fill(y, obs_mask, harmonics=harmonics)
    raise ValueError(f"unknown_method:{name}")


def _method_weights(
    y: np.ndarray,
    obs_mask: np.ndarray,
    *,
    harmonics: int,
    cv_ratio: float,
    seed: int,
) -> tuple[dict[str, float], dict[str, float]]:
    obs_idx = np.where(obs_mask)[0]
    if obs_idx.size < 12:
        w = 1.0 / float(len(_METHODS))
        return ({m: w for m in _METHODS}, {m: float("nan") for m in _METHODS})

    rng = np.random.default_rng(seed)
    candidates = obs_idx[(obs_idx > 1) & (obs_idx < y.size - 2)]
    if candidates.size < 5:
        candidates = obs_idx
    val_n = int(max(5, min(candidates.size, round(candidates.size * max(0.05, cv_ratio)))))
    val_idx = np.sort(rng.choice(candidates, size=val_n, replace=False))

    train_mask = obs_mask.copy()
    train_mask[val_idx] = False
    y_train = y.copy()
    y_train[val_idx] = np.nan

    errors: dict[str, float] = {}
    for method in _METHODS:
        pred = _apply_method(method, y_train, train_mask, harmonics=harmonics)
        mae = float(np.mean(np.abs(pred[val_idx] - y[val_idx])))
        if not np.isfinite(mae):
            mae = 1e9
        errors[method] = max(mae, 1e-9)

    inv = np.asarray([1.0 / errors[m] for m in _METHODS], dtype=np.float64)
    inv_sum = float(inv.sum())
    if inv_sum <= 0.0 or not np.isfinite(inv_sum):
        w = np.full(len(_METHODS), 1.0 / len(_METHODS), dtype=np.float64)
    else:
        w = inv / inv_sum
    weights = {m: float(w[i]) for i, m in enumerate(_METHODS)}
    return weights, errors


def _ensemble_fill_series(
    y: np.ndarray,
    *,
    nonnegative: bool,
    harmonics: int,
    cv_ratio: float,
    seed: int,
) -> tuple[np.ndarray, np.ndarray, dict[str, float], dict[str, float]]:
    y = np.asarray(y, dtype=np.float64)
    obs_mask = np.isfinite(y)

    if obs_mask.sum() == 0:
        filled = np.zeros_like(y)
        uncertainty = np.zeros_like(y)
        uniform = {m: 1.0 / len(_METHODS) for m in _METHODS}
        scores = {m: float("nan") for m in _METHODS}
        return filled, uncertainty, uniform, scores

    target = y.copy()
    if nonnegative:
        target = np.log1p(np.clip(target, a_min=0.0, a_max=None))

    weights, scores = _method_weights(
        target,
        obs_mask,
        harmonics=harmonics,
        cv_ratio=cv_ratio,
        seed=seed,
    )

    preds = []
    for method in _METHODS:
        pred = _apply_method(method, target, obs_mask, harmonics=harmonics)
        preds.append(pred)
    pred_mat = np.vstack(preds)

    w = np.asarray([weights[m] for m in _METHODS], dtype=np.float64)
    ensemble = np.average(pred_mat, axis=0, weights=w)
    uncertainty = pred_mat.std(axis=0)

    out = target.copy()
    miss = ~obs_mask
    out[miss] = ensemble[miss]

    if nonnegative:
        out = np.expm1(out)
        out = np.clip(out, a_min=0.0, a_max=None)

    return out, uncertainty, weights, scores


def _load_and_prepare(paths: tuple[Path, ...]) -> pl.DataFrame:
    if not paths:
        raise ValueError("no_input_paths")

    frames: list[pl.DataFrame] = []
    for path in paths:
        if not path.exists():
            raise FileNotFoundError(f"input_not_found:{path}")
        frame = pl.read_parquet(path)
        frames.append(frame)

    df = pl.concat(frames, how="diagonal_relaxed")

    required = {"symbol", "exchange", "exch_ts"}
    missing = sorted(required - set(df.columns))
    if missing:
        raise ValueError(f"missing_required_columns:{missing}")

    has_price = "price_scaled" in df.columns
    has_volume = "volume" in df.columns
    if not has_price:
        df = df.with_columns(pl.lit(None).cast(pl.Int64).alias("price_scaled"))
    if not has_volume:
        df = df.with_columns(pl.lit(0).cast(pl.Int64).alias("volume"))

    if "bids_price" in df.columns and "asks_price" in df.columns:
        best_bid = pl.col("bids_price").list.get(0, null_on_oob=True).cast(pl.Float64, strict=False)
        best_ask = pl.col("asks_price").list.get(0, null_on_oob=True).cast(pl.Float64, strict=False)
        derived_price = (
            pl.when(pl.col("price_scaled") > 0)
            .then(pl.col("price_scaled").cast(pl.Float64))
            .otherwise((best_bid + best_ask) / 2.0)
        )
    else:
        derived_price = pl.col("price_scaled").cast(pl.Float64)

    out = (
        df.with_columns(
            derived_price.alias("effective_price"),
            pl.col("volume").cast(pl.Float64, strict=False).fill_nan(0.0).fill_null(0.0).alias("effective_volume"),
            pl.col("symbol").cast(pl.String),
            pl.col("exchange").cast(pl.String),
            pl.col("exch_ts").cast(pl.Int64),
        )
        .filter(pl.col("effective_price").is_not_null() & (pl.col("effective_price") > 0))
        .select("symbol", "exchange", "exch_ts", "effective_price", "effective_volume")
    )
    return out


def _resample_ohlcv(df: pl.DataFrame, *, step_ns: int) -> pl.DataFrame:
    bars = (
        df.with_columns(((pl.col("exch_ts") // step_ns) * step_ns).alias("bar_ts"))
        .sort(["symbol", "exchange", "bar_ts", "exch_ts"])
        .group_by(["symbol", "exchange", "bar_ts"], maintain_order=True)
        .agg(
            pl.col("effective_price").first().alias("open_scaled"),
            pl.col("effective_price").max().alias("high_scaled"),
            pl.col("effective_price").min().alias("low_scaled"),
            pl.col("effective_price").last().alias("close_scaled"),
            pl.col("effective_volume").sum().alias("volume"),
            pl.len().alias("event_count"),
        )
        .sort(["symbol", "exchange", "bar_ts"])
    )
    return bars


def _complete_time_grid(bars: pl.DataFrame, *, step_ns: int) -> pl.DataFrame:
    parts: list[pl.DataFrame] = []
    by_group = bars.partition_by(["symbol", "exchange"], as_dict=True)
    for key, grp in by_group.items():
        symbol, exchange = key
        mn = int(grp["bar_ts"].min())
        mx = int(grp["bar_ts"].max())
        if mx < mn:
            continue
        ts = np.arange(mn, mx + step_ns, step_ns, dtype=np.int64)
        grid = pl.DataFrame(
            {
                "symbol": np.full(ts.size, symbol, dtype=object),
                "exchange": np.full(ts.size, exchange, dtype=object),
                "bar_ts": ts,
            }
        )
        joined = grid.join(grp, on=["symbol", "exchange", "bar_ts"], how="left")
        parts.append(joined)

    if not parts:
        return pl.DataFrame(
            schema={
                "symbol": pl.String,
                "exchange": pl.String,
                "bar_ts": pl.Int64,
                "open_scaled": pl.Float64,
                "high_scaled": pl.Float64,
                "low_scaled": pl.Float64,
                "close_scaled": pl.Float64,
                "volume": pl.Float64,
                "event_count": pl.Int64,
            }
        )

    return pl.concat(parts, how="vertical_relaxed").sort(["symbol", "exchange", "bar_ts"])


def _repair_group(grp: pl.DataFrame, cfg: RepairConfig) -> tuple[pl.DataFrame, dict[str, Any]]:
    close_raw = (
        _as_float_array(grp["close_scaled"]) if "close_scaled" in grp.columns else np.array([], dtype=np.float64)
    )
    volume_raw = _as_float_array(grp["volume"]) if "volume" in grp.columns else np.array([], dtype=np.float64)

    close_filled, close_unc, close_w, close_scores = _ensemble_fill_series(
        close_raw,
        nonnegative=True,
        harmonics=cfg.harmonics,
        cv_ratio=cfg.cv_ratio,
        seed=cfg.seed,
    )
    vol_filled, vol_unc, vol_w, vol_scores = _ensemble_fill_series(
        volume_raw,
        nonnegative=True,
        harmonics=cfg.harmonics,
        cv_ratio=cfg.cv_ratio,
        seed=cfg.seed + 7,
    )

    observed_close = np.isfinite(close_raw)
    is_imputed = ~observed_close

    open_raw = _as_float_array(grp["open_scaled"]) if "open_scaled" in grp.columns else np.full(close_raw.size, np.nan)
    high_raw = _as_float_array(grp["high_scaled"]) if "high_scaled" in grp.columns else np.full(close_raw.size, np.nan)
    low_raw = _as_float_array(grp["low_scaled"]) if "low_scaled" in grp.columns else np.full(close_raw.size, np.nan)

    open_out = open_raw.copy()
    high_out = high_raw.copy()
    low_out = low_raw.copy()
    close_out = close_raw.copy()
    vol_out = volume_raw.copy()

    close_out[is_imputed] = close_filled[is_imputed]
    vol_out[~np.isfinite(vol_out)] = 0.0
    vol_out[is_imputed] = vol_filled[is_imputed]

    for i in range(close_out.size):
        if not is_imputed[i]:
            continue
        prev_close = close_out[i - 1] if i > 0 else close_out[i]
        open_out[i] = prev_close
        high_out[i] = max(open_out[i], close_out[i])
        low_out[i] = min(open_out[i], close_out[i])

    # Ensure observed rows are complete too.
    for arr in (open_out, high_out, low_out):
        missing = ~np.isfinite(arr)
        arr[missing] = close_out[missing]

    out = grp.with_columns(
        pl.Series("open_scaled", np.rint(np.clip(open_out, a_min=0.0, a_max=None)).astype(np.int64)),
        pl.Series("high_scaled", np.rint(np.clip(high_out, a_min=0.0, a_max=None)).astype(np.int64)),
        pl.Series("low_scaled", np.rint(np.clip(low_out, a_min=0.0, a_max=None)).astype(np.int64)),
        pl.Series("close_scaled", np.rint(np.clip(close_out, a_min=0.0, a_max=None)).astype(np.int64)),
        pl.Series("volume", np.rint(np.clip(vol_out, a_min=0.0, a_max=None)).astype(np.int64)),
        pl.Series("is_imputed", is_imputed),
        pl.Series("impute_uncertainty_close", close_unc.astype(np.float64)),
        pl.Series("impute_uncertainty_volume", vol_unc.astype(np.float64)),
    )

    symbol = str(grp["symbol"][0]) if grp.height else ""
    exchange = str(grp["exchange"][0]) if grp.height else ""
    report = {
        "symbol": symbol,
        "exchange": exchange,
        "bars_total": int(grp.height),
        "bars_imputed": int(is_imputed.sum()),
        "missing_ratio_before": float(is_imputed.mean()) if grp.height else 0.0,
        "methods_considered": list(_METHODS),
        "close_method_weights": close_w,
        "close_method_mae": close_scores,
        "volume_method_weights": vol_w,
        "volume_method_mae": vol_scores,
    }
    return out, report


def run(cfg: RepairConfig) -> tuple[Path, Path]:
    step_ns = int(max(1, cfg.target_ms) * 1_000_000)

    raw = _load_and_prepare(cfg.input_paths)
    bars = _resample_ohlcv(raw, step_ns=step_ns)
    completed = _complete_time_grid(bars, step_ns=step_ns)

    repaired_parts: list[pl.DataFrame] = []
    reports: list[dict[str, Any]] = []
    for grp in completed.partition_by(["symbol", "exchange"], as_dict=False):
        repaired, report = _repair_group(grp, cfg)
        repaired_parts.append(repaired)
        reports.append(report)

    repaired_df = pl.concat(repaired_parts, how="vertical_relaxed").sort(["symbol", "exchange", "bar_ts"])

    _ensure_parent(cfg.output_path)
    repaired_df.write_parquet(cfg.output_path)

    summary = {
        "input_paths": [str(p) for p in cfg.input_paths],
        "output_path": str(cfg.output_path),
        "target_ms": int(cfg.target_ms),
        "step_ns": step_ns,
        "rows_output": int(repaired_df.height),
        "groups": reports,
    }
    _ensure_parent(cfg.report_path)
    cfg.report_path.write_text(json.dumps(summary, indent=2, ensure_ascii=False), encoding="utf-8")

    return cfg.output_path, cfg.report_path


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Repair fragmented historical market data and resample to complete OHLCV bars. "
            "Uses 5 mathematical models: linear, PCHIP, Kalman local-level, AR(1), harmonic regression."
        )
    )
    parser.add_argument(
        "--input",
        dest="inputs",
        action="append",
        required=True,
        help="Input parquet/parquet.part path (repeatable)",
    )
    parser.add_argument("--out", required=True, help="Output repaired parquet path")
    parser.add_argument("--report-out", default=None, help="Output JSON report path")
    parser.add_argument("--target-ms", type=int, default=1000, help="Resample interval in milliseconds")
    parser.add_argument("--cv-ratio", type=float, default=0.1, help="Validation ratio used for model weighting")
    parser.add_argument("--harmonics", type=int, default=6, help="Max Fourier harmonics for harmonic regression")
    parser.add_argument("--seed", type=int, default=42)
    return parser


def main() -> int:
    args = build_parser().parse_args()
    input_paths = tuple(Path(p).resolve() for p in args.inputs)
    out_path = Path(args.out).resolve()
    report_path = Path(args.report_out).resolve() if args.report_out else out_path.with_suffix(".repair_report.json")

    cfg = RepairConfig(
        input_paths=input_paths,
        output_path=out_path,
        report_path=report_path,
        target_ms=int(args.target_ms),
        cv_ratio=float(args.cv_ratio),
        harmonics=int(args.harmonics),
        seed=int(args.seed),
    )
    out, report = run(cfg)
    print(f"[history_repair] repaired parquet: {out}")
    print(f"[history_repair] report: {report}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
