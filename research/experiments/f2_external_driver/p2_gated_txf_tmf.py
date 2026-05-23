"""F2-A TXF->TMF external-driver audit with the frozen P2 maker gate.

This is not a standalone P2-alpha test. The external driver chooses direction:

    TXF recent mid move > threshold  -> TMF maker_bid candidate
    TXF recent mid move < -threshold -> TMF maker_ask candidate

P2 only answers whether the corresponding maker action is executable enough:

    simple_gate_score = p_fill_hat * spread_z
    strict gate       = top 10% threshold frozen on train days

All thresholds are learned from chronological train days and applied to OOS
test days.
"""
from __future__ import annotations

import argparse
import json
import logging
from dataclasses import dataclass
from pathlib import Path

import numpy as np

from research.experiments.p2_exec_predictor.composite_ev import _load_model
from research.experiments.p2_exec_predictor.markout_regression import (
    DEFAULT_RT_COST_PT,
)
from research.experiments.p2_exec_predictor.train_eval import (
    DEFAULT_HORIZONS_MS,
    DEFAULT_TRAIN_FRAC,
    MIN_FILLS_PER_DAY,
    DayPanel,
    build_features,
    load_days,
)

log = logging.getLogger(__name__)

DEFAULT_LAGS_MS: tuple[int, ...] = (500, 2000, 5000)
DEFAULT_DRIVER_PCTS: tuple[float, ...] = (0.10, 0.20, 0.30)
DEFAULT_P2_GATE_PCT: float = 0.10
NS_PER_MS = 1_000_000


@dataclass(frozen=True, slots=True)
class F2Config:
    horizons_ms: tuple[int, ...] = DEFAULT_HORIZONS_MS
    lags_ms: tuple[int, ...] = DEFAULT_LAGS_MS
    driver_pcts: tuple[float, ...] = DEFAULT_DRIVER_PCTS
    p2_gate_pct: float = DEFAULT_P2_GATE_PCT
    train_frac: float = DEFAULT_TRAIN_FRAC
    rt_cost_pt: float = DEFAULT_RT_COST_PT


@dataclass(frozen=True, slots=True)
class PairedDay:
    date: str
    tmf: DayPanel
    txf: DayPanel


def _parse_int_tuple(text: str) -> tuple[int, ...]:
    return tuple(int(x.strip()) for x in text.split(",") if x.strip())


def _parse_float_tuple(text: str) -> tuple[float, ...]:
    return tuple(float(x.strip()) for x in text.split(",") if x.strip())


def _pair_days(tmf_days: list[DayPanel], txf_days: list[DayPanel]) -> list[PairedDay]:
    tmf_by_date = {d.date: d for d in tmf_days}
    txf_by_date = {d.date: d for d in txf_days}
    common = sorted(set(tmf_by_date) & set(txf_by_date))
    if len(common) < 4:
        raise RuntimeError(f"need >=4 common active days; got {len(common)}")
    return [PairedDay(date=d, tmf=tmf_by_date[d], txf=txf_by_date[d]) for d in common]


def _side_word(side: int) -> str:
    return "buy" if side > 0 else "sell"


def _p2_score(cols: dict[str, np.ndarray], side: int, fill_model) -> tuple[np.ndarray, np.ndarray]:
    X = build_features(cols, side)
    finite = np.all(np.isfinite(X), axis=1)
    score = np.full(X.shape[0], np.nan, dtype=np.float64)
    if finite.any():
        X_f = X[finite]
        p_fill = fill_model.predict_proba(X_f)
        spread_z = fill_model.normalizer.transform(X_f)[:, 0]
        score[finite] = p_fill * spread_z
    return score, finite


def _driver_delta(tmf_cols: dict[str, np.ndarray], txf_cols: dict[str, np.ndarray], lag_ms: int) -> np.ndarray:
    """Causal TXF mid delta ending at or before the TMF timestamp."""
    tmf_t = np.asarray(tmf_cols["t_ns"], dtype=np.int64)
    txf_t = np.asarray(txf_cols["t_ns"], dtype=np.int64)
    txf_mid = np.asarray(txf_cols["mid_px"], dtype=np.float64)

    current_idx = np.searchsorted(txf_t, tmf_t, side="right") - 1
    past_idx = np.searchsorted(txf_t, tmf_t - lag_ms * NS_PER_MS, side="right") - 1
    valid = (
        (current_idx >= 0)
        & (past_idx >= 0)
        & (current_idx < txf_mid.size)
        & (past_idx < txf_mid.size)
    )
    delta = np.full(tmf_t.size, np.nan, dtype=np.float64)
    delta[valid] = txf_mid[current_idx[valid]] - txf_mid[past_idx[valid]]
    return delta


def _gate_threshold(
    days: list[PairedDay],
    side: int,
    horizon_ms: int,
    fill_model,
    gate_pct: float,
) -> float:
    scores: list[np.ndarray] = []
    side_label = _side_word(side)
    fill_col = f"filled_{side_label}_h{horizon_ms}"
    for d in days:
        score, finite_x = _p2_score(d.tmf.cols, side, fill_model)
        fill = np.asarray(d.tmf.cols[fill_col], dtype=np.int8)
        keep = finite_x & (fill != -1) & np.isfinite(score)
        if keep.any():
            scores.append(score[keep])
    if not scores:
        raise RuntimeError(f"no P2 scores for side={side_label} horizon={horizon_ms}")
    merged = np.concatenate(scores)
    return float(np.quantile(merged, 1.0 - gate_pct))


def _driver_threshold(days: list[PairedDay], lag_ms: int, driver_pct: float) -> float:
    deltas = []
    for d in days:
        delta = _driver_delta(d.tmf.cols, d.txf.cols, lag_ms)
        finite = np.isfinite(delta)
        nonzero = finite & (delta != 0.0)
        if nonzero.any():
            deltas.append(np.abs(delta[nonzero]))
    if not deltas:
        raise RuntimeError(f"no driver deltas for lag={lag_ms}")
    merged = np.concatenate(deltas)
    return float(np.quantile(merged, 1.0 - driver_pct))


def _raw_ev(fill: np.ndarray, markout: np.ndarray) -> float:
    n = int(fill.size)
    if n == 0:
        return float("nan")
    fills = (fill == 1) & np.isfinite(markout)
    if not fills.any():
        return 0.0
    return float(fills.sum() / n) * float(markout[fills].mean())


def _selection_metrics(
    fill: np.ndarray,
    markout: np.ndarray,
    spread: np.ndarray,
    selected: np.ndarray,
    rt_cost_pt: float,
) -> dict[str, float | int]:
    n_total = int(selected.size)
    n = int(selected.sum())
    if n == 0:
        return {
            "n": 0,
            "sample_pct": 0.0,
            "fill_rate": float("nan"),
            "mean_markout_given_fill_pt": float("nan"),
            "adverse_rate_given_fill": float("nan"),
            "raw_ev_pt": float("nan"),
            "net_ev_pt": float("nan"),
            "avg_spread_pt": float("nan"),
        }
    fill_s = fill[selected]
    mark_s = markout[selected]
    spread_s = spread[selected]
    fills = (fill_s == 1) & np.isfinite(mark_s)
    n_fills = int(fills.sum())
    fill_rate = n_fills / n
    mean_markout = float(mark_s[fills].mean()) if n_fills else 0.0
    raw_ev = fill_rate * mean_markout
    adverse = float((mark_s[fills] < 0.0).mean()) if n_fills else float("nan")
    return {
        "n": n,
        "sample_pct": float(n / n_total) if n_total else 0.0,
        "fill_rate": float(fill_rate),
        "mean_markout_given_fill_pt": mean_markout,
        "adverse_rate_given_fill": adverse,
        "raw_ev_pt": float(raw_ev),
        "net_ev_pt": float(raw_ev - rt_cost_pt),
        "avg_spread_pt": float(np.nanmean(spread_s)),
    }


def _side_arrays(day: PairedDay, horizon_ms: int) -> tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray, np.ndarray]:
    buy_fill = np.asarray(day.tmf.cols[f"filled_buy_h{horizon_ms}"], dtype=np.int8)
    sell_fill = np.asarray(day.tmf.cols[f"filled_sell_h{horizon_ms}"], dtype=np.int8)
    buy_mark = np.asarray(day.tmf.cols[f"markout_buy_h{horizon_ms}"], dtype=np.float64)
    sell_mark = np.asarray(day.tmf.cols[f"markout_sell_h{horizon_ms}"], dtype=np.float64)
    spread = np.asarray(day.tmf.cols["spread_pt"], dtype=np.float64)
    return buy_fill, sell_fill, buy_mark, sell_mark, spread


def _evaluate_candidate(
    days: list[PairedDay],
    horizon_ms: int,
    lag_ms: int,
    driver_threshold: float,
    buy_gate_threshold: float,
    sell_gate_threshold: float,
    buy_fill_model,
    sell_fill_model,
    p2_only_baseline: dict[str, float | int],
    rt_cost_pt: float,
) -> dict:
    fill_parts: list[np.ndarray] = []
    mark_parts: list[np.ndarray] = []
    spread_parts: list[np.ndarray] = []
    active_parts: list[np.ndarray] = []
    gated_parts: list[np.ndarray] = []
    long_parts: list[np.ndarray] = []
    short_parts: list[np.ndarray] = []
    per_day: list[dict] = []

    for d in days:
        delta = _driver_delta(d.tmf.cols, d.txf.cols, lag_ms)
        long = delta >= driver_threshold
        short = delta <= -driver_threshold
        active = long | short

        buy_fill, sell_fill, buy_mark, sell_mark, spread = _side_arrays(d, horizon_ms)
        fill = np.where(long, buy_fill, sell_fill)
        mark = np.where(long, buy_mark, sell_mark)
        valid = np.where(long, buy_fill != -1, sell_fill != -1)

        buy_score, buy_finite = _p2_score(d.tmf.cols, 1, buy_fill_model)
        sell_score, sell_finite = _p2_score(d.tmf.cols, -1, sell_fill_model)
        buy_pass = buy_finite & (buy_score >= buy_gate_threshold)
        sell_pass = sell_finite & (sell_score >= sell_gate_threshold)
        gated = active & np.where(long, buy_pass, sell_pass)

        active_valid = active & valid & np.isfinite(delta)
        gated_valid = gated & valid & np.isfinite(delta)

        fill_parts.append(fill)
        mark_parts.append(mark)
        spread_parts.append(spread)
        active_parts.append(active_valid)
        gated_parts.append(gated_valid)
        long_parts.append(long & active_valid)
        short_parts.append(short & active_valid)

        active_raw = _raw_ev(fill[active_valid], mark[active_valid])
        gated_raw = _raw_ev(fill[gated_valid], mark[gated_valid])
        per_day.append(
            {
                "date": d.date,
                "active_n": int(active_valid.sum()),
                "gated_n": int(gated_valid.sum()),
                "active_raw_ev_pt": active_raw,
                "gated_raw_ev_pt": gated_raw,
                "gate_lift_raw_ev_pt": float(gated_raw - active_raw)
                if np.isfinite(active_raw) and np.isfinite(gated_raw)
                else float("nan"),
                "long_n": int((long & active_valid).sum()),
                "short_n": int((short & active_valid).sum()),
            }
        )

    fill_all = np.concatenate(fill_parts)
    mark_all = np.concatenate(mark_parts)
    spread_all = np.concatenate(spread_parts)
    active_all = np.concatenate(active_parts)
    gated_all = np.concatenate(gated_parts)
    long_all = np.concatenate(long_parts)
    short_all = np.concatenate(short_parts)

    active_m = _selection_metrics(fill_all, mark_all, spread_all, active_all, rt_cost_pt)
    gated_m = _selection_metrics(fill_all, mark_all, spread_all, gated_all, rt_cost_pt)
    long_m = _selection_metrics(fill_all, mark_all, spread_all, long_all, rt_cost_pt)
    short_m = _selection_metrics(fill_all, mark_all, spread_all, short_all, rt_cost_pt)

    lifts = np.asarray(
        [r["gate_lift_raw_ev_pt"] for r in per_day if np.isfinite(r["gate_lift_raw_ev_pt"])],
        dtype=np.float64,
    )
    gated_daily = np.asarray(
        [r["gated_raw_ev_pt"] for r in per_day if np.isfinite(r["gated_raw_ev_pt"])],
        dtype=np.float64,
    )
    abs_sum = float(np.abs(lifts).sum()) if lifts.size else 0.0
    gate_lift_share = float(np.max(np.abs(lifts)) / abs_sum) if abs_sum > 0 else float("nan")

    verdict = "KILL"
    if gated_m["n"] >= 500 and gated_m["raw_ev_pt"] > active_m["raw_ev_pt"]:
        verdict = "EXEC_GATE_IMPROVES"
    external_lift_vs_p2 = float(gated_m["raw_ev_pt"] - p2_only_baseline["raw_ev_pt"])
    if (
        verdict == "EXEC_GATE_IMPROVES"
        and gated_m["net_ev_pt"] > 0.0
        and external_lift_vs_p2 > 0.0
        and lifts.size
        and float((lifts > 0).mean()) >= 0.55
        and gate_lift_share < 0.4
    ):
        verdict = "F2_EDGE_CANDIDATE"
    elif verdict == "EXEC_GATE_IMPROVES" and gated_m["net_ev_pt"] > 0.0:
        verdict = "P2_ONLY_EXPLAINS_POSITIVE_EV"

    return {
        "horizon_ms": horizon_ms,
        "lag_ms": lag_ms,
        "driver_threshold_pt": driver_threshold,
        "buy_gate_threshold": buy_gate_threshold,
        "sell_gate_threshold": sell_gate_threshold,
        "active": active_m,
        "p2_gated": gated_m,
        "p2_only_baseline": p2_only_baseline,
        "long_active": long_m,
        "short_active": short_m,
        "gate_lift_raw_ev_pt": float(gated_m["raw_ev_pt"] - active_m["raw_ev_pt"]),
        "external_lift_vs_p2_raw_ev_pt": external_lift_vs_p2,
        "gate_pass_rate_within_active": float(gated_m["n"] / active_m["n"])
        if active_m["n"]
        else float("nan"),
        "stability": {
            "n_days": int(len(per_day)),
            "gate_lift_positive_days_pct": float((lifts > 0).mean()) if lifts.size else float("nan"),
            "gated_positive_days_pct": float((gated_daily > 0).mean()) if gated_daily.size else float("nan"),
            "median_gate_lift_raw_ev_pt": float(np.median(lifts)) if lifts.size else float("nan"),
            "max_single_day_gate_lift_share_abs": gate_lift_share,
        },
        "verdict": verdict,
        "per_day": per_day,
    }


def _p2_only_baseline(
    days: list[PairedDay],
    horizon_ms: int,
    buy_gate_threshold: float,
    sell_gate_threshold: float,
    buy_fill_model,
    sell_fill_model,
    rt_cost_pt: float,
) -> dict[str, float | int]:
    """Evaluate P2 strict gate without any external directional driver."""
    fill_parts: list[np.ndarray] = []
    mark_parts: list[np.ndarray] = []
    spread_parts: list[np.ndarray] = []
    selected_parts: list[np.ndarray] = []
    for d in days:
        buy_fill, sell_fill, buy_mark, sell_mark, spread = _side_arrays(d, horizon_ms)
        buy_score, buy_finite = _p2_score(d.tmf.cols, 1, buy_fill_model)
        sell_score, sell_finite = _p2_score(d.tmf.cols, -1, sell_fill_model)
        buy_selected = buy_finite & (buy_score >= buy_gate_threshold) & (buy_fill != -1)
        sell_selected = sell_finite & (sell_score >= sell_gate_threshold) & (sell_fill != -1)
        fill_parts.append(np.concatenate([buy_fill, sell_fill]))
        mark_parts.append(np.concatenate([buy_mark, sell_mark]))
        spread_parts.append(np.concatenate([spread, spread]))
        selected_parts.append(np.concatenate([buy_selected, sell_selected]))
    return _selection_metrics(
        np.concatenate(fill_parts),
        np.concatenate(mark_parts),
        np.concatenate(spread_parts),
        np.concatenate(selected_parts),
        rt_cost_pt,
    )


def run_audit(
    tmf_dir: Path,
    txf_dir: Path,
    p2_model_dir: Path,
    out_dir: Path,
    cfg: F2Config | None = None,
) -> dict:
    cfg = cfg or F2Config()
    out_dir.mkdir(parents=True, exist_ok=True)

    tmf_days = load_days(tmf_dir, min_fills=MIN_FILLS_PER_DAY)
    txf_days = load_days(txf_dir, min_fills=MIN_FILLS_PER_DAY)
    paired = _pair_days(tmf_days, txf_days)
    n_train = max(2, int(len(paired) * cfg.train_frac))
    train_days = paired[:n_train]
    test_days = paired[n_train:]
    if len(test_days) < 2:
        raise RuntimeError(f"need >=2 OOS days; got {len(test_days)}")

    models_dir = p2_model_dir / "models"
    results: list[dict] = []
    for horizon_ms in cfg.horizons_ms:
        buy_fill_model = _load_model(models_dir, f"buy_h{horizon_ms}_fill")
        sell_fill_model = _load_model(models_dir, f"sell_h{horizon_ms}_fill")
        buy_gate_threshold = _gate_threshold(
            train_days, 1, horizon_ms, buy_fill_model, cfg.p2_gate_pct
        )
        sell_gate_threshold = _gate_threshold(
            train_days, -1, horizon_ms, sell_fill_model, cfg.p2_gate_pct
        )
        p2_only_baseline = _p2_only_baseline(
            test_days,
            horizon_ms,
            buy_gate_threshold,
            sell_gate_threshold,
            buy_fill_model,
            sell_fill_model,
            cfg.rt_cost_pt,
        )
        for lag_ms in cfg.lags_ms:
            for driver_pct in cfg.driver_pcts:
                driver_threshold = _driver_threshold(train_days, lag_ms, driver_pct)
                result = _evaluate_candidate(
                    test_days,
                    horizon_ms=horizon_ms,
                    lag_ms=lag_ms,
                    driver_threshold=driver_threshold,
                    buy_gate_threshold=buy_gate_threshold,
                    sell_gate_threshold=sell_gate_threshold,
                    buy_fill_model=buy_fill_model,
                    sell_fill_model=sell_fill_model,
                    p2_only_baseline=p2_only_baseline,
                    rt_cost_pt=cfg.rt_cost_pt,
                )
                result["driver_pct"] = float(driver_pct)
                results.append(result)

    summary = {
        "experiment": "F2-A TXF->TMF external driver with frozen P2 strict gate",
        "tmf_dir": str(tmf_dir),
        "txf_dir": str(txf_dir),
        "p2_model_dir": str(p2_model_dir),
        "out_dir": str(out_dir),
        "config": {
            "horizons_ms": list(cfg.horizons_ms),
            "lags_ms": list(cfg.lags_ms),
            "driver_pcts": list(cfg.driver_pcts),
            "p2_gate_pct": cfg.p2_gate_pct,
            "train_frac": cfg.train_frac,
            "rt_cost_pt": cfg.rt_cost_pt,
        },
        "split": {
            "common_active_days": len(paired),
            "train_days": [d.date for d in train_days],
            "test_days": [d.date for d in test_days],
        },
        "results": results,
    }
    (out_dir / "summary.json").write_text(json.dumps(summary, indent=2, sort_keys=True))
    _write_report(summary, out_dir / "REPORT.md")
    return summary


def _fmt(x: object, nd: int = 3) -> str:
    if isinstance(x, float):
        if np.isnan(x):
            return "nan"
        return f"{x:.{nd}f}"
    return str(x)


def _write_report(summary: dict, path: Path) -> None:
    results = list(summary["results"])
    ranked = sorted(
        results,
        key=lambda r: (
            r["p2_gated"]["net_ev_pt"],
            r["gate_lift_raw_ev_pt"],
            r["p2_gated"]["n"],
        ),
        reverse=True,
    )
    verdict_counts: dict[str, int] = {}
    for r in results:
        verdict_counts[r["verdict"]] = verdict_counts.get(r["verdict"], 0) + 1

    lines = [
        "# F2-A TXF->TMF External Driver + P2 Gate Audit",
        "",
        "## Verdict Counts",
        "",
    ]
    for verdict, count in sorted(verdict_counts.items()):
        lines.append(f"- {verdict}: {count}")
    lines.extend(
        [
            "",
            "## Split",
            "",
            f"- Common active days: {summary['split']['common_active_days']}",
            (
                f"- Train: {summary['split']['train_days'][0]} -> "
                f"{summary['split']['train_days'][-1]} "
                f"({len(summary['split']['train_days'])} days)"
            ),
            (
                f"- Test: {summary['split']['test_days'][0]} -> "
                f"{summary['split']['test_days'][-1]} "
                f"({len(summary['split']['test_days'])} days)"
            ),
            "",
            "## Top Candidates By P2-Gated Net EV",
            "",
            "| h_ms | lag_ms | driver_pct | active_n | gated_n | active_raw | "
            "gated_raw | p2_raw | gated_net | lift | f2_vs_p2 | gate_pass | lift_pos_days | "
            "max_day_share | verdict |",
            "| ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | --- |",
        ]
    )
    for r in ranked[:20]:
        lines.append(
            "| "
            + " | ".join(
                [
                    str(r["horizon_ms"]),
                    str(r["lag_ms"]),
                    _fmt(r["driver_pct"], 2),
                    str(r["active"]["n"]),
                    str(r["p2_gated"]["n"]),
                    _fmt(r["active"]["raw_ev_pt"]),
                    _fmt(r["p2_gated"]["raw_ev_pt"]),
                    _fmt(r["p2_only_baseline"]["raw_ev_pt"]),
                    _fmt(r["p2_gated"]["net_ev_pt"]),
                    _fmt(r["gate_lift_raw_ev_pt"]),
                    _fmt(r["external_lift_vs_p2_raw_ev_pt"]),
                    _fmt(r["gate_pass_rate_within_active"]),
                    _fmt(r["stability"]["gate_lift_positive_days_pct"]),
                    _fmt(r["stability"]["max_single_day_gate_lift_share_abs"]),
                    r["verdict"],
                ]
            )
            + " |"
        )
    lines.extend(
        [
            "",
            "## Interpretation Rule",
            "",
            "- `F2_EDGE_CANDIDATE` requires positive OOS gated net EV, stable P2 lift, and improvement over P2-only.",
            "- `P2_ONLY_EXPLAINS_POSITIVE_EV` means the gated strategy is positive but does not beat P2-only.",
            "- `EXEC_GATE_IMPROVES` means P2 improves execution quality but does not yet clear strategy economics.",
            "- `KILL` means this TXF lag-return driver is not useful under this audit.",
            "",
        ]
    )
    path.write_text("\n".join(lines))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--tmf-dir",
        type=Path,
        default=Path("research/data/derived/p2_fill_events_tmf_smoke"),
    )
    parser.add_argument(
        "--txf-dir",
        type=Path,
        default=Path("research/data/derived/p2_fill_events_txf_smoke"),
    )
    parser.add_argument("--p2-model-dir", type=Path, default=Path("outputs/p2_exec_predictor/tmf"))
    parser.add_argument(
        "--out",
        type=Path,
        default=Path("outputs/f2_external_driver/txf_tmf_p2_gate"),
    )
    parser.add_argument("--horizons-ms", default="500,2000,5000")
    parser.add_argument("--lags-ms", default="500,2000,5000")
    parser.add_argument("--driver-pcts", default="0.10,0.20,0.30")
    parser.add_argument("--p2-gate-pct", type=float, default=DEFAULT_P2_GATE_PCT)
    parser.add_argument("--train-frac", type=float, default=DEFAULT_TRAIN_FRAC)
    parser.add_argument("--rt-cost-pt", type=float, default=DEFAULT_RT_COST_PT)
    parser.add_argument("--log-level", default="INFO")
    args = parser.parse_args()

    logging.basicConfig(level=getattr(logging, args.log_level.upper()))
    cfg = F2Config(
        horizons_ms=_parse_int_tuple(args.horizons_ms),
        lags_ms=_parse_int_tuple(args.lags_ms),
        driver_pcts=_parse_float_tuple(args.driver_pcts),
        p2_gate_pct=args.p2_gate_pct,
        train_frac=args.train_frac,
        rt_cost_pt=args.rt_cost_pt,
    )
    run_audit(
        tmf_dir=args.tmf_dir,
        txf_dir=args.txf_dir,
        p2_model_dir=args.p2_model_dir,
        out_dir=args.out,
        cfg=cfg,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
