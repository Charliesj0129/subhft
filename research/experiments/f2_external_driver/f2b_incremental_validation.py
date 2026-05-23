"""F2-B incremental external-driver validation with mandatory P2 baseline.

The unit of evidence is not candidate EV. It is:

    incremental_lift = candidate_EV - P2_only_EV

Controls included:

* shifted-day TXF control
* within-day permutation null
* random sign permutation null
* alternate splits
* parameter neighborhood grid
* day-by-day incremental lift
* directional mechanism bucket tables
"""
from __future__ import annotations

import argparse
import csv
import json
import logging
from dataclasses import dataclass
from pathlib import Path

import numpy as np

from research.experiments.f2_external_driver.p2_gated_txf_tmf import (
    NS_PER_MS,
    _fmt,
    _p2_score,
    _pair_days,
    _selection_metrics,
    _side_arrays,
)
from research.experiments.p2_exec_predictor.composite_ev import _load_model
from research.experiments.p2_exec_predictor.markout_regression import DEFAULT_RT_COST_PT
from research.experiments.p2_exec_predictor.train_eval import (
    MIN_FILLS_PER_DAY,
    DayPanel,
    load_days,
)

log = logging.getLogger(__name__)

DEFAULT_HORIZONS_MS: tuple[int, ...] = (2000, 3000, 5000, 8000, 10000)
DEFAULT_LAGS_MS: tuple[int, ...] = (250, 500, 750, 1000)
DEFAULT_DRIVER_PCTS: tuple[float, ...] = (0.20, 0.30, 0.40)
DEFAULT_BEST_HORIZON_MS = 5000
DEFAULT_BEST_LAG_MS = 500
DEFAULT_BEST_DRIVER_PCT = 0.30
DEFAULT_P2_GATE_PCT = 0.10
DEFAULT_N_PERMUTATIONS = 100


@dataclass(frozen=True, slots=True)
class F2BConfig:
    horizons_ms: tuple[int, ...] = DEFAULT_HORIZONS_MS
    lags_ms: tuple[int, ...] = DEFAULT_LAGS_MS
    driver_pcts: tuple[float, ...] = DEFAULT_DRIVER_PCTS
    best_horizon_ms: int = DEFAULT_BEST_HORIZON_MS
    best_lag_ms: int = DEFAULT_BEST_LAG_MS
    best_driver_pct: float = DEFAULT_BEST_DRIVER_PCT
    p2_gate_pct: float = DEFAULT_P2_GATE_PCT
    rt_cost_pt: float = DEFAULT_RT_COST_PT
    n_permutations: int = DEFAULT_N_PERMUTATIONS
    seed: int = 20260511


@dataclass(slots=True)
class PreparedDay:
    date: str
    tmf: DayPanel
    txf: DayPanel
    buy_score_by_horizon: dict[int, np.ndarray]
    sell_score_by_horizon: dict[int, np.ndarray]
    buy_finite_by_horizon: dict[int, np.ndarray]
    sell_finite_by_horizon: dict[int, np.ndarray]


@dataclass(frozen=True, slots=True)
class SplitSpec:
    name: str
    train_idx: tuple[int, ...]
    test_idx: tuple[int, ...]


def _parse_int_tuple(text: str) -> tuple[int, ...]:
    return tuple(int(x.strip()) for x in text.split(",") if x.strip())


def _parse_float_tuple(text: str) -> tuple[float, ...]:
    return tuple(float(x.strip()) for x in text.split(",") if x.strip())


def _available_horizons(days: list[DayPanel], p2_model_dir: Path, requested: tuple[int, ...]) -> tuple[int, ...]:
    if not days:
        return ()
    cols = days[0].cols
    models_dir = p2_model_dir / "models"
    out = []
    for h_ms in requested:
        required_cols = (
            f"filled_buy_h{h_ms}",
            f"filled_sell_h{h_ms}",
            f"markout_buy_h{h_ms}",
            f"markout_sell_h{h_ms}",
        )
        required_models = (
            models_dir / f"buy_h{h_ms}_fill.json",
            models_dir / f"sell_h{h_ms}_fill.json",
        )
        if all(c in cols for c in required_cols) and all(p.exists() for p in required_models):
            out.append(h_ms)
    return tuple(out)


def _load_fill_models(p2_model_dir: Path, horizons_ms: tuple[int, ...]) -> dict[tuple[int, int], object]:
    models_dir = p2_model_dir / "models"
    models: dict[tuple[int, int], object] = {}
    for h_ms in horizons_ms:
        models[(h_ms, 1)] = _load_model(models_dir, f"buy_h{h_ms}_fill")
        models[(h_ms, -1)] = _load_model(models_dir, f"sell_h{h_ms}_fill")
    return models


def _prepare_days(
    paired_days,
    models: dict[tuple[int, int], object],
    horizons_ms: tuple[int, ...],
) -> list[PreparedDay]:
    prepared: list[PreparedDay] = []
    for day in paired_days:
        buy_score_by_horizon: dict[int, np.ndarray] = {}
        sell_score_by_horizon: dict[int, np.ndarray] = {}
        buy_finite_by_horizon: dict[int, np.ndarray] = {}
        sell_finite_by_horizon: dict[int, np.ndarray] = {}
        for h_ms in horizons_ms:
            buy_score, buy_finite = _p2_score(day.tmf.cols, 1, models[(h_ms, 1)])
            sell_score, sell_finite = _p2_score(day.tmf.cols, -1, models[(h_ms, -1)])
            buy_score_by_horizon[h_ms] = buy_score
            sell_score_by_horizon[h_ms] = sell_score
            buy_finite_by_horizon[h_ms] = buy_finite
            sell_finite_by_horizon[h_ms] = sell_finite
        prepared.append(
            PreparedDay(
                date=day.date,
                tmf=day.tmf,
                txf=day.txf,
                buy_score_by_horizon=buy_score_by_horizon,
                sell_score_by_horizon=sell_score_by_horizon,
                buy_finite_by_horizon=buy_finite_by_horizon,
                sell_finite_by_horizon=sell_finite_by_horizon,
            )
        )
    return prepared


def _split_specs(n_days: int) -> list[SplitSpec]:
    n_train_70 = max(2, int(n_days * 0.7))
    n_half = max(2, n_days // 2)
    even_idx = tuple(i for i in range(n_days) if i % 2 == 0)
    odd_idx = tuple(i for i in range(n_days) if i % 2 == 1)
    return [
        SplitSpec(
            name="chrono_70_30",
            train_idx=tuple(range(n_train_70)),
            test_idx=tuple(range(n_train_70, n_days)),
        ),
        SplitSpec(
            name="front_half_back_half",
            train_idx=tuple(range(n_half)),
            test_idx=tuple(range(n_half, n_days)),
        ),
        SplitSpec(name="even_train_odd_test", train_idx=even_idx, test_idx=odd_idx),
        SplitSpec(name="odd_train_even_test", train_idx=odd_idx, test_idx=even_idx),
    ]


def _select(days: list[PreparedDay], idx: tuple[int, ...]) -> list[PreparedDay]:
    return [days[i] for i in idx]


def _intraday_driver_delta(tmf_day: PreparedDay, txf_day: PreparedDay, lag_ms: int) -> np.ndarray:
    tmf_t = np.asarray(tmf_day.tmf.cols["t_ns"], dtype=np.int64)
    txf_t = np.asarray(txf_day.txf.cols["t_ns"], dtype=np.int64)
    txf_mid = np.asarray(txf_day.txf.cols["mid_px"], dtype=np.float64)

    tmf_offset = tmf_t - tmf_t[0]
    txf_offset = txf_t - txf_t[0]
    current_idx = np.searchsorted(txf_offset, tmf_offset, side="right") - 1
    past_idx = np.searchsorted(txf_offset, tmf_offset - lag_ms * NS_PER_MS, side="right") - 1
    valid = (
        (current_idx >= 0)
        & (past_idx >= 0)
        & (current_idx < txf_mid.size)
        & (past_idx < txf_mid.size)
    )
    delta = np.full(tmf_t.size, np.nan, dtype=np.float64)
    delta[valid] = txf_mid[current_idx[valid]] - txf_mid[past_idx[valid]]
    return delta


def _real_delta_map(days: list[PreparedDay], lag_ms: int) -> dict[str, np.ndarray]:
    return {d.date: _intraday_driver_delta(d, d, lag_ms) for d in days}


def _shifted_delta_map(
    days: list[PreparedDay],
    lag_ms: int,
    shift: int,
) -> tuple[list[PreparedDay], dict[str, np.ndarray]]:
    out_days: list[PreparedDay] = []
    deltas: dict[str, np.ndarray] = {}
    for i, day in enumerate(days):
        j = i + shift
        if j < 0 or j >= len(days):
            continue
        out_days.append(day)
        deltas[day.date] = _intraday_driver_delta(day, days[j], lag_ms)
    return out_days, deltas


def _driver_threshold_from_map(
    train_days: list[PreparedDay],
    delta_map: dict[str, np.ndarray],
    driver_pct: float,
) -> float:
    parts = []
    for day in train_days:
        delta = delta_map[day.date]
        keep = np.isfinite(delta) & (delta != 0.0)
        if keep.any():
            parts.append(np.abs(delta[keep]))
    if not parts:
        raise RuntimeError("no valid driver deltas")
    return float(np.quantile(np.concatenate(parts), 1.0 - driver_pct))


def _p2_thresholds(
    train_days: list[PreparedDay],
    horizon_ms: int,
    gate_pct: float,
) -> tuple[float, float]:
    thresholds = []
    for side in (1, -1):
        scores = []
        for day in train_days:
            side_word = "buy" if side > 0 else "sell"
            fill = np.asarray(day.tmf.cols[f"filled_{side_word}_h{horizon_ms}"], dtype=np.int8)
            if side > 0:
                score = day.buy_score_by_horizon[horizon_ms]
                finite = day.buy_finite_by_horizon[horizon_ms]
            else:
                score = day.sell_score_by_horizon[horizon_ms]
                finite = day.sell_finite_by_horizon[horizon_ms]
            keep = finite & (fill != -1) & np.isfinite(score)
            if keep.any():
                scores.append(score[keep])
        if not scores:
            raise RuntimeError(f"no P2 scores for horizon={horizon_ms} side={side}")
        thresholds.append(float(np.quantile(np.concatenate(scores), 1.0 - gate_pct)))
    return thresholds[0], thresholds[1]


def _raw_ev(fill: np.ndarray, markout: np.ndarray) -> float:
    if fill.size == 0:
        return float("nan")
    fills = (fill == 1) & np.isfinite(markout)
    if not fills.any():
        return 0.0
    return float(fills.sum() / fill.size) * float(markout[fills].mean())


def _p2_only_metrics(
    days: list[PreparedDay],
    horizon_ms: int,
    buy_threshold: float,
    sell_threshold: float,
    rt_cost_pt: float,
) -> tuple[dict[str, float | int], list[dict[str, float | int | str]]]:
    fill_parts = []
    mark_parts = []
    spread_parts = []
    selected_parts = []
    daily = []
    for day in days:
        buy_fill, sell_fill, buy_mark, sell_mark, spread = _side_arrays(day, horizon_ms)
        buy_selected = (
            day.buy_finite_by_horizon[horizon_ms]
            & (day.buy_score_by_horizon[horizon_ms] >= buy_threshold)
            & (buy_fill != -1)
        )
        sell_selected = (
            day.sell_finite_by_horizon[horizon_ms]
            & (day.sell_score_by_horizon[horizon_ms] >= sell_threshold)
            & (sell_fill != -1)
        )
        day_fill = np.concatenate([buy_fill, sell_fill])
        day_mark = np.concatenate([buy_mark, sell_mark])
        day_spread = np.concatenate([spread, spread])
        day_selected = np.concatenate([buy_selected, sell_selected])
        fill_parts.append(day_fill)
        mark_parts.append(day_mark)
        spread_parts.append(day_spread)
        selected_parts.append(day_selected)
        day_m = _selection_metrics(day_fill, day_mark, day_spread, day_selected, rt_cost_pt)
        daily.append({"date": day.date, **day_m})
    overall = _selection_metrics(
        np.concatenate(fill_parts),
        np.concatenate(mark_parts),
        np.concatenate(spread_parts),
        np.concatenate(selected_parts),
        rt_cost_pt,
    )
    return overall, daily


def _directional_mid_return(day: PreparedDay, horizon_ms: int, long: np.ndarray, short: np.ndarray) -> np.ndarray:
    mid = np.asarray(day.tmf.cols["mid_px"], dtype=np.float64)
    out = np.full(mid.size, np.nan, dtype=np.float64)
    if mid.size < 2:
        return out
    median_step_ns = float(np.nanmedian(np.diff(np.asarray(day.tmf.cols["t_ns"], dtype=np.int64))))
    if not np.isfinite(median_step_ns) or median_step_ns <= 0:
        return out
    h_steps = max(1, int(round((horizon_ms * NS_PER_MS) / median_step_ns)))
    if h_steps >= mid.size:
        return out
    fwd = mid[h_steps:] - mid[:-h_steps]
    out[:-h_steps] = np.where(long[:-h_steps], fwd, np.where(short[:-h_steps], -fwd, np.nan))
    return out


def _candidate_metrics(
    days: list[PreparedDay],
    horizon_ms: int,
    lag_ms: int,
    driver_threshold: float,
    buy_threshold: float,
    sell_threshold: float,
    p2_daily_by_date: dict[str, dict],
    p2_overall: dict[str, float | int],
    delta_map: dict[str, np.ndarray],
    rt_cost_pt: float,
) -> tuple[dict, list[dict]]:
    fill_parts = []
    mark_parts = []
    spread_parts = []
    selected_parts = []
    active_parts = []
    daily = []
    for day in days:
        delta = delta_map[day.date]
        long = delta >= driver_threshold
        short = delta <= -driver_threshold
        active = (long | short) & np.isfinite(delta)

        buy_fill, sell_fill, buy_mark, sell_mark, spread = _side_arrays(day, horizon_ms)
        fill = np.where(long, buy_fill, sell_fill)
        mark = np.where(long, buy_mark, sell_mark)
        valid = np.where(long, buy_fill != -1, sell_fill != -1)
        buy_pass = (
            day.buy_finite_by_horizon[horizon_ms]
            & (day.buy_score_by_horizon[horizon_ms] >= buy_threshold)
        )
        sell_pass = (
            day.sell_finite_by_horizon[horizon_ms]
            & (day.sell_score_by_horizon[horizon_ms] >= sell_threshold)
        )
        selected = active & valid & np.where(long, buy_pass, sell_pass)
        active_valid = active & valid

        fill_parts.append(fill)
        mark_parts.append(mark)
        spread_parts.append(spread)
        selected_parts.append(selected)
        active_parts.append(active_valid)

        candidate_raw = _raw_ev(fill[selected], mark[selected])
        active_raw = _raw_ev(fill[active_valid], mark[active_valid])
        p2_day = p2_daily_by_date[day.date]
        p2_raw = float(p2_day["raw_ev_pt"])
        daily.append(
            {
                "date": day.date,
                "horizon_ms": horizon_ms,
                "lag_ms": lag_ms,
                "driver_threshold_pt": driver_threshold,
                "candidate_n": int(selected.sum()),
                "active_n": int(active_valid.sum()),
                "candidate_raw_ev_pt": candidate_raw,
                "candidate_net_ev_pt": candidate_raw - rt_cost_pt if np.isfinite(candidate_raw) else float("nan"),
                "active_raw_ev_pt": active_raw,
                "p2_only_n": int(p2_day["n"]),
                "p2_only_raw_ev_pt": p2_raw,
                "p2_only_net_ev_pt": float(p2_day["net_ev_pt"]),
                "incremental_lift_raw_ev_pt": float(candidate_raw - p2_raw)
                if np.isfinite(candidate_raw) and np.isfinite(p2_raw)
                else float("nan"),
                "long_n": int((long & active_valid).sum()),
                "short_n": int((short & active_valid).sum()),
            }
        )

    fill_all = np.concatenate(fill_parts)
    mark_all = np.concatenate(mark_parts)
    spread_all = np.concatenate(spread_parts)
    selected_all = np.concatenate(selected_parts)
    active_all = np.concatenate(active_parts)
    candidate = _selection_metrics(fill_all, mark_all, spread_all, selected_all, rt_cost_pt)
    active = _selection_metrics(fill_all, mark_all, spread_all, active_all, rt_cost_pt)
    lift = float(candidate["raw_ev_pt"] - p2_overall["raw_ev_pt"])
    daily_lifts = np.asarray(
        [r["incremental_lift_raw_ev_pt"] for r in daily if np.isfinite(r["incremental_lift_raw_ev_pt"])],
        dtype=np.float64,
    )
    positive = daily_lifts[daily_lifts > 0]
    pos_sum = float(positive.sum())
    top1_share = float(np.max(positive) / pos_sum) if pos_sum > 0 and positive.size else float("nan")
    top3_share = float(np.sort(positive)[-3:].sum() / pos_sum) if pos_sum > 0 and positive.size else float("nan")
    abs_sum = float(np.abs(daily_lifts).sum()) if daily_lifts.size else 0.0
    single_abs = float(np.max(np.abs(daily_lifts)) / abs_sum) if abs_sum > 0 else float("nan")
    summary = {
        "horizon_ms": horizon_ms,
        "lag_ms": lag_ms,
        "driver_threshold_pt": driver_threshold,
        "candidate": candidate,
        "active": active,
        "p2_only": p2_overall,
        "incremental_lift_raw_ev_pt": lift,
        "incremental_lift_net_ev_pt": lift,
        "gate_pass_rate_within_active": float(candidate["n"] / active["n"]) if active["n"] else float("nan"),
        "daily_lift": {
            "n_days": int(daily_lifts.size),
            "positive_lift_days_pct": float((daily_lifts > 0).mean()) if daily_lifts.size else float("nan"),
            "median_daily_lift_raw_ev_pt": float(np.median(daily_lifts)) if daily_lifts.size else float("nan"),
            "worst_day_lift_raw_ev_pt": float(np.min(daily_lifts)) if daily_lifts.size else float("nan"),
            "top_1_day_lift_share": top1_share,
            "top_3_day_lift_share": top3_share,
            "single_day_abs_lift_share": single_abs,
        },
    }
    return summary, daily


def _bootstrap_ci(values: np.ndarray, rng: np.random.Generator, n_boot: int = 2000) -> tuple[float, float]:
    values = values[np.isfinite(values)]
    if values.size < 2:
        return float("nan"), float("nan")
    draws = rng.choice(values, size=(n_boot, values.size), replace=True).mean(axis=1)
    lo, hi = np.quantile(draws, [0.025, 0.975])
    return float(lo), float(hi)


def _row_from_candidate(split_name: str, driver_pct: float, result: dict) -> dict:
    return {
        "split": split_name,
        "horizon_ms": result["horizon_ms"],
        "lag_ms": result["lag_ms"],
        "driver_pct": driver_pct,
        "driver_threshold_pt": result["driver_threshold_pt"],
        "candidate_n": result["candidate"]["n"],
        "candidate_raw_ev_pt": result["candidate"]["raw_ev_pt"],
        "candidate_net_ev_pt": result["candidate"]["net_ev_pt"],
        "p2_only_n": result["p2_only"]["n"],
        "p2_only_raw_ev_pt": result["p2_only"]["raw_ev_pt"],
        "p2_only_net_ev_pt": result["p2_only"]["net_ev_pt"],
        "incremental_lift_raw_ev_pt": result["incremental_lift_raw_ev_pt"],
        "positive_lift_days_pct": result["daily_lift"]["positive_lift_days_pct"],
        "median_daily_lift_raw_ev_pt": result["daily_lift"]["median_daily_lift_raw_ev_pt"],
        "top_1_day_lift_share": result["daily_lift"]["top_1_day_lift_share"],
        "top_3_day_lift_share": result["daily_lift"]["top_3_day_lift_share"],
        "single_day_abs_lift_share": result["daily_lift"]["single_day_abs_lift_share"],
        "gate_pass_rate_within_active": result["gate_pass_rate_within_active"],
    }


def _write_csv(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if not rows:
        path.write_text("")
        return
    fieldnames = list(rows[0].keys())
    with path.open("w", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)


def _permutation_controls(
    test_days: list[PreparedDay],
    horizon_ms: int,
    lag_ms: int,
    driver_threshold: float,
    buy_threshold: float,
    sell_threshold: float,
    p2_daily_by_date: dict[str, dict],
    p2_overall: dict[str, float | int],
    real_delta_map: dict[str, np.ndarray],
    rt_cost_pt: float,
    n_permutations: int,
    rng: np.random.Generator,
) -> list[dict]:
    rows: list[dict] = []
    for mode in ("within_day_shuffle", "random_sign"):
        lifts = []
        raw_evs = []
        for i in range(n_permutations):
            perm_map: dict[str, np.ndarray] = {}
            for day in test_days:
                delta = real_delta_map[day.date]
                valid = np.isfinite(delta)
                perm_delta = delta.copy()
                if mode == "within_day_shuffle":
                    vals = perm_delta[valid].copy()
                    rng.shuffle(vals)
                    perm_delta[valid] = vals
                else:
                    signs = rng.choice(np.asarray([-1.0, 1.0]), size=int(valid.sum()))
                    perm_delta[valid] = np.abs(perm_delta[valid]) * signs
                perm_map[day.date] = perm_delta
            result, _ = _candidate_metrics(
                test_days,
                horizon_ms,
                lag_ms,
                driver_threshold,
                buy_threshold,
                sell_threshold,
                p2_daily_by_date,
                p2_overall,
                perm_map,
                rt_cost_pt,
            )
            lifts.append(result["incremental_lift_raw_ev_pt"])
            raw_evs.append(result["candidate"]["raw_ev_pt"])
            rows.append(
                {
                    "mode": mode,
                    "iteration": i,
                    "candidate_raw_ev_pt": result["candidate"]["raw_ev_pt"],
                    "p2_only_raw_ev_pt": result["p2_only"]["raw_ev_pt"],
                    "incremental_lift_raw_ev_pt": result["incremental_lift_raw_ev_pt"],
                    "candidate_n": result["candidate"]["n"],
                }
            )
        log.info(
            "permutation mode=%s lift_p95=%.6f raw_p95=%.6f",
            mode,
            float(np.quantile(lifts, 0.95)),
            float(np.quantile(raw_evs, 0.95)),
        )
    return rows


def _mechanism_rows(
    days: list[PreparedDay],
    horizon_ms: int,
    lag_ms: int,
    driver_threshold: float,
    buy_threshold: float,
    sell_threshold: float,
    delta_map: dict[str, np.ndarray],
    rt_cost_pt: float,
) -> list[dict]:
    empty_bucket = {
        "score": [],
        "spread": [],
        "ret": [],
        "ret_gated": [],
        "pass": [],
        "fill": [],
        "mark": [],
        "sel": [],
    }
    buckets = {
        "long": {k: list(v) for k, v in empty_bucket.items()},
        "short": {k: list(v) for k, v in empty_bucket.items()},
        "neutral": {k: list(v) for k, v in empty_bucket.items()},
    }
    for day in days:
        delta = delta_map[day.date]
        long = delta >= driver_threshold
        short = delta <= -driver_threshold
        neutral = np.isfinite(delta) & ~(long | short)
        buy_fill, sell_fill, buy_mark, sell_mark, spread = _side_arrays(day, horizon_ms)
        buy_pass = (
            day.buy_finite_by_horizon[horizon_ms]
            & (day.buy_score_by_horizon[horizon_ms] >= buy_threshold)
        )
        sell_pass = (
            day.sell_finite_by_horizon[horizon_ms]
            & (day.sell_score_by_horizon[horizon_ms] >= sell_threshold)
        )
        directional_ret = _directional_mid_return(day, horizon_ms, long, short)
        for name, mask, score, passed, fill, mark in (
            ("long", long, day.buy_score_by_horizon[horizon_ms], buy_pass, buy_fill, buy_mark),
            ("short", short, day.sell_score_by_horizon[horizon_ms], sell_pass, sell_fill, sell_mark),
            (
                "neutral",
                neutral,
                np.maximum(day.buy_score_by_horizon[horizon_ms], day.sell_score_by_horizon[horizon_ms]),
                buy_pass | sell_pass,
                buy_fill,
                buy_mark,
            ),
        ):
            keep = mask & np.isfinite(score)
            buckets[name]["score"].append(score[keep])
            buckets[name]["spread"].append(spread[keep])
            buckets[name]["ret"].append(directional_ret[keep])
            buckets[name]["ret_gated"].append(directional_ret[keep & passed])
            buckets[name]["pass"].append(passed[keep])
            buckets[name]["fill"].append(fill[keep])
            buckets[name]["mark"].append(mark[keep])
            buckets[name]["sel"].append(passed[keep] & (fill[keep] != -1))
    rows = []
    for name, parts in buckets.items():
        score = np.concatenate(parts["score"]) if parts["score"] else np.asarray([])
        spread = np.concatenate(parts["spread"]) if parts["spread"] else np.asarray([])
        ret = np.concatenate(parts["ret"]) if parts["ret"] else np.asarray([])
        ret_gated = np.concatenate(parts["ret_gated"]) if parts["ret_gated"] else np.asarray([])
        passed = np.concatenate(parts["pass"]) if parts["pass"] else np.asarray([], dtype=bool)
        fill = np.concatenate(parts["fill"]) if parts["fill"] else np.asarray([], dtype=np.int8)
        mark = np.concatenate(parts["mark"]) if parts["mark"] else np.asarray([])
        selected = np.concatenate(parts["sel"]) if parts["sel"] else np.asarray([], dtype=bool)
        metrics = _selection_metrics(fill, mark, spread, selected, rt_cost_pt) if fill.size else {}
        rows.append(
            {
                "bucket": name,
                "n": int(score.size),
                "p2_score_mean": float(np.nanmean(score)) if score.size else float("nan"),
                "p2_score_p90": float(np.nanquantile(score, 0.9)) if score.size else float("nan"),
                "spread_mean_pt": float(np.nanmean(spread)) if spread.size else float("nan"),
                "p2_pass_rate": float(passed.mean()) if passed.size else float("nan"),
                "directional_mid_return_mean_pt": float(np.nanmean(ret)) if ret.size else float("nan"),
                "directional_mid_return_after_p2_mean_pt": float(np.nanmean(ret_gated))
                if ret_gated.size
                else float("nan"),
                "maker_raw_ev_after_p2_pt": metrics.get("raw_ev_pt", float("nan")),
                "maker_net_ev_after_p2_pt": metrics.get("net_ev_pt", float("nan")),
                "maker_n_after_p2": metrics.get("n", 0),
            }
        )
    return rows


def _run_split_candidate(
    prepared: list[PreparedDay],
    split: SplitSpec,
    horizon_ms: int,
    lag_ms: int,
    driver_pct: float,
    p2_gate_pct: float,
    rt_cost_pt: float,
) -> tuple[dict, list[dict], list[PreparedDay], list[PreparedDay], float, float, float, dict[str, np.ndarray]]:
    train_days = _select(prepared, split.train_idx)
    test_days = _select(prepared, split.test_idx)
    train_delta_map = _real_delta_map(train_days, lag_ms)
    test_delta_map = _real_delta_map(test_days, lag_ms)
    driver_threshold = _driver_threshold_from_map(train_days, train_delta_map, driver_pct)
    buy_threshold, sell_threshold = _p2_thresholds(train_days, horizon_ms, p2_gate_pct)
    p2_overall, p2_daily = _p2_only_metrics(test_days, horizon_ms, buy_threshold, sell_threshold, rt_cost_pt)
    p2_daily_by_date = {str(r["date"]): r for r in p2_daily}
    result, daily = _candidate_metrics(
        test_days,
        horizon_ms,
        lag_ms,
        driver_threshold,
        buy_threshold,
        sell_threshold,
        p2_daily_by_date,
        p2_overall,
        test_delta_map,
        rt_cost_pt,
    )
    return result, daily, train_days, test_days, driver_threshold, buy_threshold, sell_threshold, test_delta_map


def run_validation(
    tmf_dir: Path,
    txf_dir: Path,
    p2_model_dir: Path,
    out_dir: Path,
    cfg: F2BConfig | None = None,
) -> dict:
    cfg = cfg or F2BConfig()
    out_dir.mkdir(parents=True, exist_ok=True)
    rng = np.random.default_rng(cfg.seed)

    tmf_days = load_days(tmf_dir, min_fills=MIN_FILLS_PER_DAY)
    txf_days = load_days(txf_dir, min_fills=MIN_FILLS_PER_DAY)
    paired = _pair_days(tmf_days, txf_days)
    requested_horizons = tuple(sorted(set(cfg.horizons_ms + (cfg.best_horizon_ms,))))
    horizons_ms = _available_horizons([p.tmf for p in paired], p2_model_dir, requested_horizons)
    unsupported = sorted(set(requested_horizons) - set(horizons_ms))
    if cfg.best_horizon_ms not in horizons_ms:
        raise RuntimeError(f"best horizon {cfg.best_horizon_ms} is unavailable; available={horizons_ms}")
    models = _load_fill_models(p2_model_dir, horizons_ms)
    prepared = _prepare_days(paired, models, horizons_ms)
    splits = _split_specs(len(prepared))
    split_by_name = {s.name: s for s in splits}
    chrono = split_by_name["chrono_70_30"]

    # Best-cell daily audit and baseline.
    best, best_daily, train_days, test_days, driver_thr, buy_thr, sell_thr, test_delta_map = _run_split_candidate(
        prepared,
        chrono,
        cfg.best_horizon_ms,
        cfg.best_lag_ms,
        cfg.best_driver_pct,
        cfg.p2_gate_pct,
        cfg.rt_cost_pt,
    )
    daily_lift_values = np.asarray(
        [r["incremental_lift_raw_ev_pt"] for r in best_daily if np.isfinite(r["incremental_lift_raw_ev_pt"])],
        dtype=np.float64,
    )
    ci_low, ci_high = _bootstrap_ci(daily_lift_values, rng)
    best["daily_lift"]["bootstrap_ci_low_pt"] = ci_low
    best["daily_lift"]["bootstrap_ci_high_pt"] = ci_high

    # Parameter grid on chrono split.
    grid_rows = []
    for h_ms in horizons_ms:
        if h_ms not in cfg.horizons_ms:
            continue
        buy_threshold, sell_threshold = _p2_thresholds(train_days, h_ms, cfg.p2_gate_pct)
        p2_overall, p2_daily = _p2_only_metrics(test_days, h_ms, buy_threshold, sell_threshold, cfg.rt_cost_pt)
        p2_daily_by_date = {str(r["date"]): r for r in p2_daily}
        for lag_ms in cfg.lags_ms:
            train_delta_map = _real_delta_map(train_days, lag_ms)
            test_lag_delta_map = _real_delta_map(test_days, lag_ms)
            for driver_pct in cfg.driver_pcts:
                threshold = _driver_threshold_from_map(train_days, train_delta_map, driver_pct)
                result, _ = _candidate_metrics(
                    test_days,
                    h_ms,
                    lag_ms,
                    threshold,
                    buy_threshold,
                    sell_threshold,
                    p2_daily_by_date,
                    p2_overall,
                    test_lag_delta_map,
                    cfg.rt_cost_pt,
                )
                grid_rows.append(_row_from_candidate("chrono_70_30", driver_pct, result))

    # Alternate split controls for the best candidate.
    split_rows = []
    for split in splits:
        result, _, *_ = _run_split_candidate(
            prepared,
            split,
            cfg.best_horizon_ms,
            cfg.best_lag_ms,
            cfg.best_driver_pct,
            cfg.p2_gate_pct,
            cfg.rt_cost_pt,
        )
        split_rows.append(_row_from_candidate(split.name, cfg.best_driver_pct, result))

    # Shifted-day controls on the chrono test segment.
    shifted_rows = []
    for shift in (-1, 1):
        shifted_days, shifted_delta_map = _shifted_delta_map(test_days, cfg.best_lag_ms, shift)
        real_subset_map = _real_delta_map(shifted_days, cfg.best_lag_ms)
        p2_overall, p2_daily = _p2_only_metrics(
            shifted_days, cfg.best_horizon_ms, buy_thr, sell_thr, cfg.rt_cost_pt
        )
        p2_daily_by_date = {str(r["date"]): r for r in p2_daily}
        real_result, _ = _candidate_metrics(
            shifted_days,
            cfg.best_horizon_ms,
            cfg.best_lag_ms,
            driver_thr,
            buy_thr,
            sell_thr,
            p2_daily_by_date,
            p2_overall,
            real_subset_map,
            cfg.rt_cost_pt,
        )
        shifted_result, _ = _candidate_metrics(
            shifted_days,
            cfg.best_horizon_ms,
            cfg.best_lag_ms,
            driver_thr,
            buy_thr,
            sell_thr,
            p2_daily_by_date,
            p2_overall,
            shifted_delta_map,
            cfg.rt_cost_pt,
        )
        shifted_rows.append(
            {
                "shift_days": shift,
                "n_eval_days": len(shifted_days),
                "real_candidate_raw_ev_pt": real_result["candidate"]["raw_ev_pt"],
                "real_p2_only_raw_ev_pt": real_result["p2_only"]["raw_ev_pt"],
                "real_incremental_lift_raw_ev_pt": real_result["incremental_lift_raw_ev_pt"],
                "shifted_candidate_raw_ev_pt": shifted_result["candidate"]["raw_ev_pt"],
                "shifted_p2_only_raw_ev_pt": shifted_result["p2_only"]["raw_ev_pt"],
                "shifted_incremental_lift_raw_ev_pt": shifted_result["incremental_lift_raw_ev_pt"],
                "real_minus_shifted_lift_pt": real_result["incremental_lift_raw_ev_pt"]
                - shifted_result["incremental_lift_raw_ev_pt"],
            }
        )

    p2_overall_best, p2_daily_best = _p2_only_metrics(
        test_days, cfg.best_horizon_ms, buy_thr, sell_thr, cfg.rt_cost_pt
    )
    p2_daily_best_by_date = {str(r["date"]): r for r in p2_daily_best}
    permutation_rows = _permutation_controls(
        test_days,
        cfg.best_horizon_ms,
        cfg.best_lag_ms,
        driver_thr,
        buy_thr,
        sell_thr,
        p2_daily_best_by_date,
        p2_overall_best,
        test_delta_map,
        cfg.rt_cost_pt,
        cfg.n_permutations,
        rng,
    )

    mechanism_rows = _mechanism_rows(
        test_days,
        cfg.best_horizon_ms,
        cfg.best_lag_ms,
        driver_thr,
        buy_thr,
        sell_thr,
        test_delta_map,
        cfg.rt_cost_pt,
    )

    _write_csv(out_dir / "parameter_grid.csv", grid_rows)
    _write_csv(out_dir / "alternate_split.csv", split_rows)
    _write_csv(out_dir / "shifted_day_control.csv", shifted_rows)
    _write_csv(out_dir / "permutation_control.csv", permutation_rows)
    _write_csv(out_dir / "daily_lift.csv", best_daily)
    _write_csv(out_dir / "mechanism_buckets.csv", mechanism_rows)

    perm_summary = _summarize_permutations(permutation_rows, best["incremental_lift_raw_ev_pt"])
    neighborhood = _summarize_neighborhood(grid_rows, cfg.best_horizon_ms, cfg.best_lag_ms, cfg.best_driver_pct)
    verdict = _verdict(best, shifted_rows, perm_summary, split_rows, neighborhood)
    summary = {
        "experiment": "F2-B incremental external-driver validation",
        "tmf_dir": str(tmf_dir),
        "txf_dir": str(txf_dir),
        "p2_model_dir": str(p2_model_dir),
        "out_dir": str(out_dir),
        "config": {
            "horizons_ms": list(cfg.horizons_ms),
            "available_horizons_ms": list(horizons_ms),
            "unsupported_horizons_ms": unsupported,
            "lags_ms": list(cfg.lags_ms),
            "driver_pcts": list(cfg.driver_pcts),
            "best_horizon_ms": cfg.best_horizon_ms,
            "best_lag_ms": cfg.best_lag_ms,
            "best_driver_pct": cfg.best_driver_pct,
            "p2_gate_pct": cfg.p2_gate_pct,
            "rt_cost_pt": cfg.rt_cost_pt,
            "n_permutations": cfg.n_permutations,
            "seed": cfg.seed,
        },
        "split": {
            "common_active_days": len(prepared),
            "chrono_train_days": [d.date for d in train_days],
            "chrono_test_days": [d.date for d in test_days],
        },
        "best_candidate": best,
        "shifted_day_control": shifted_rows,
        "permutation_summary": perm_summary,
        "alternate_split": split_rows,
        "neighborhood_summary": neighborhood,
        "mechanism_buckets": mechanism_rows,
        "verdict": verdict,
    }
    (out_dir / "summary.json").write_text(json.dumps(summary, indent=2, sort_keys=True))
    _write_report(summary, out_dir / "REPORT.md")
    return summary


def _summarize_permutations(rows: list[dict], real_lift: float) -> dict:
    out: dict[str, dict] = {}
    for mode in sorted({str(r["mode"]) for r in rows}):
        vals = np.asarray([float(r["incremental_lift_raw_ev_pt"]) for r in rows if r["mode"] == mode])
        if vals.size == 0:
            continue
        out[mode] = {
            "n": int(vals.size),
            "mean_lift_pt": float(vals.mean()),
            "p50_lift_pt": float(np.quantile(vals, 0.5)),
            "p95_lift_pt": float(np.quantile(vals, 0.95)),
            "real_minus_p95_lift_pt": float(real_lift - np.quantile(vals, 0.95)),
            "real_gt_p95": bool(real_lift > np.quantile(vals, 0.95)),
        }
    return out


def _summarize_neighborhood(
    rows: list[dict],
    best_horizon_ms: int,
    best_lag_ms: int,
    best_driver_pct: float,
) -> dict:
    del best_lag_ms
    best_h_rows = [r for r in rows if int(r["horizon_ms"]) == best_horizon_ms]
    positive = [r for r in best_h_rows if float(r["incremental_lift_raw_ev_pt"]) > 0.0]
    best_cell = [
        r
        for r in rows
        if int(r["horizon_ms"]) == best_horizon_ms
        and int(r["lag_ms"]) == DEFAULT_BEST_LAG_MS
        and abs(float(r["driver_pct"]) - best_driver_pct) < 1e-12
    ]
    return {
        "best_horizon_cells": len(best_h_rows),
        "best_horizon_positive_lift_cells": len(positive),
        "best_horizon_positive_lift_pct": float(len(positive) / len(best_h_rows)) if best_h_rows else float("nan"),
        "best_cell_incremental_lift_raw_ev_pt": float(best_cell[0]["incremental_lift_raw_ev_pt"])
        if best_cell
        else float("nan"),
    }


def _verdict(
    best: dict,
    shifted_rows: list[dict],
    perm_summary: dict,
    split_rows: list[dict],
    neighborhood: dict,
) -> str:
    lift = float(best["incremental_lift_raw_ev_pt"])
    ci_low = float(best["daily_lift"]["bootstrap_ci_low_pt"])
    daily = best["daily_lift"]
    shifted_pass = all(float(r["real_minus_shifted_lift_pt"]) > 0.0 for r in shifted_rows)
    perm_pass = all(v.get("real_gt_p95", False) for v in perm_summary.values())
    split_lifts = np.asarray([float(r["incremental_lift_raw_ev_pt"]) for r in split_rows], dtype=np.float64)
    split_pass = bool(split_lifts.size and (split_lifts > 0).mean() >= 0.75)
    neighborhood_pass = float(neighborhood["best_horizon_positive_lift_pct"]) >= 0.5
    daily_pass = (
        float(daily["positive_lift_days_pct"]) > 0.55
        and float(daily["median_daily_lift_raw_ev_pt"]) > 0.0
        and float(daily["top_3_day_lift_share"]) < 0.5
        and float(daily["single_day_abs_lift_share"]) < 0.4
    )
    if (
        lift > 0
        and ci_low > 0
        and shifted_pass
        and perm_pass
        and split_pass
        and neighborhood_pass
        and daily_pass
    ):
        return "PROMOTE_EXTERNAL_DRIVER"
    if lift > 0 and (shifted_pass or perm_pass):
        return "WATCH_WEAK_EDGE"
    return "KILL_EXTERNAL_DRIVER"


def _write_report(summary: dict, path: Path) -> None:
    best = summary["best_candidate"]
    lines = [
        "# F2-B Incremental External-Driver Validation",
        "",
        "## Verdict",
        "",
        f"```text\n{summary['verdict']}\n```",
        "",
        "## Best Candidate",
        "",
        "| Metric | Value |",
        "| --- | ---: |",
        f"| Horizon | {best['horizon_ms']} ms |",
        f"| TXF lag | {best['lag_ms']} ms |",
        f"| Candidate raw EV | {_fmt(best['candidate']['raw_ev_pt'])} pt |",
        f"| Candidate net EV | {_fmt(best['candidate']['net_ev_pt'])} pt |",
        f"| P2-only raw EV | {_fmt(best['p2_only']['raw_ev_pt'])} pt |",
        f"| P2-only net EV | {_fmt(best['p2_only']['net_ev_pt'])} pt |",
        f"| Incremental lift | {_fmt(best['incremental_lift_raw_ev_pt'])} pt |",
        f"| Lift bootstrap CI low | {_fmt(best['daily_lift']['bootstrap_ci_low_pt'])} pt |",
        f"| Lift bootstrap CI high | {_fmt(best['daily_lift']['bootstrap_ci_high_pt'])} pt |",
        f"| Positive lift days | {_fmt(best['daily_lift']['positive_lift_days_pct'])} |",
        f"| Median daily lift | {_fmt(best['daily_lift']['median_daily_lift_raw_ev_pt'])} pt |",
        f"| Top 3 day lift share | {_fmt(best['daily_lift']['top_3_day_lift_share'])} |",
        "",
        "## Shifted-Day Control",
        "",
        "| Shift | Real lift | Shifted lift | Real - shifted |",
        "| ---: | ---: | ---: | ---: |",
    ]
    for r in summary["shifted_day_control"]:
        lines.append(
            f"| {r['shift_days']} | {_fmt(r['real_incremental_lift_raw_ev_pt'])} | "
            f"{_fmt(r['shifted_incremental_lift_raw_ev_pt'])} | "
            f"{_fmt(r['real_minus_shifted_lift_pt'])} |"
        )
    lines.extend(
        [
            "",
            "## Permutation Control",
            "",
            "| Mode | p50 | p95 | Real - p95 | Pass |",
            "| --- | ---: | ---: | ---: | --- |",
        ]
    )
    for mode, r in summary["permutation_summary"].items():
        lines.append(
            f"| {mode} | {_fmt(r['p50_lift_pt'])} | {_fmt(r['p95_lift_pt'])} | "
            f"{_fmt(r['real_minus_p95_lift_pt'])} | {r['real_gt_p95']} |"
        )
    lines.extend(
        [
            "",
            "## Alternate Splits",
            "",
            "| Split | Candidate raw | P2 raw | Lift | Positive lift days |",
            "| --- | ---: | ---: | ---: | ---: |",
        ]
    )
    for r in summary["alternate_split"]:
        lines.append(
            f"| {r['split']} | {_fmt(r['candidate_raw_ev_pt'])} | "
            f"{_fmt(r['p2_only_raw_ev_pt'])} | {_fmt(r['incremental_lift_raw_ev_pt'])} | "
            f"{_fmt(r['positive_lift_days_pct'])} |"
        )
    lines.extend(
        [
            "",
            "## Parameter Neighborhood",
            "",
            f"- Best-horizon cells: {summary['neighborhood_summary']['best_horizon_cells']}",
            "- Positive lift cells at best horizon: "
            f"{summary['neighborhood_summary']['best_horizon_positive_lift_cells']}",
            "- Positive lift pct at best horizon: "
            f"{_fmt(summary['neighborhood_summary']['best_horizon_positive_lift_pct'])}",
            "",
            "## Files",
            "",
            "- `summary.json`",
            "- `parameter_grid.csv`",
            "- `alternate_split.csv`",
            "- `shifted_day_control.csv`",
            "- `permutation_control.csv`",
            "- `daily_lift.csv`",
            "- `mechanism_buckets.csv`",
            "",
        ]
    )
    if summary["config"]["unsupported_horizons_ms"]:
        lines.extend(
            [
                "## Horizon Limitation",
                "",
                "The current P2 fill-event panel lacks labels/models for:",
                "",
                "```text",
                ", ".join(str(x) for x in summary["config"]["unsupported_horizons_ms"]),
                "```",
                "",
                "Regenerate the P2 fill-event panel before evaluating those horizons.",
                "",
            ]
        )
    path.write_text("\n".join(lines))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tmf-dir", type=Path, default=Path("research/data/derived/p2_fill_events_tmf_smoke"))
    parser.add_argument("--txf-dir", type=Path, default=Path("research/data/derived/p2_fill_events_txf_smoke"))
    parser.add_argument("--p2-model-dir", type=Path, default=Path("outputs/p2_exec_predictor/tmf"))
    parser.add_argument("--out", type=Path, default=Path("outputs/f2_external_driver/f2b_incremental_validation"))
    parser.add_argument("--horizons-ms", default="2000,3000,5000,8000,10000")
    parser.add_argument("--lags-ms", default="250,500,750,1000")
    parser.add_argument("--driver-pcts", default="0.20,0.30,0.40")
    parser.add_argument("--best-horizon-ms", type=int, default=DEFAULT_BEST_HORIZON_MS)
    parser.add_argument("--best-lag-ms", type=int, default=DEFAULT_BEST_LAG_MS)
    parser.add_argument("--best-driver-pct", type=float, default=DEFAULT_BEST_DRIVER_PCT)
    parser.add_argument("--p2-gate-pct", type=float, default=DEFAULT_P2_GATE_PCT)
    parser.add_argument("--n-permutations", type=int, default=DEFAULT_N_PERMUTATIONS)
    parser.add_argument("--seed", type=int, default=20260511)
    parser.add_argument("--log-level", default="INFO")
    args = parser.parse_args()

    logging.basicConfig(level=getattr(logging, args.log_level.upper()))
    cfg = F2BConfig(
        horizons_ms=_parse_int_tuple(args.horizons_ms),
        lags_ms=_parse_int_tuple(args.lags_ms),
        driver_pcts=_parse_float_tuple(args.driver_pcts),
        best_horizon_ms=args.best_horizon_ms,
        best_lag_ms=args.best_lag_ms,
        best_driver_pct=args.best_driver_pct,
        p2_gate_pct=args.p2_gate_pct,
        n_permutations=args.n_permutations,
        seed=args.seed,
    )
    run_validation(args.tmf_dir, args.txf_dir, args.p2_model_dir, args.out, cfg)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
