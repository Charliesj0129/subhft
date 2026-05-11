"""P2-V maker gate validation for the frozen simple spread-fill gate.

This validates the deployable gate candidate:

    simple_score = p_fill_hat * spread_z

where ``p_fill_hat`` comes from the existing trained fill model and
``spread_z`` is the fill model's normalized spread feature. The script does
not train new models. It compares simple_score against the full regression
composite:

    full_score = p_fill_hat * pred_markout

Metrics are gate-only execution-quality metrics, not directional-alpha
metrics.
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
from research.experiments.p2_exec_predictor.stability_audit import _load_regressor
from research.experiments.p2_exec_predictor.train_eval import (
    DEFAULT_HORIZONS_MS,
    DEFAULT_TRAIN_FRAC,
    MIN_FILLS_PER_DAY,
    DayPanel,
    build_features,
    load_days,
)

log = logging.getLogger(__name__)

DEFAULT_GATE_PCTS: tuple[float, ...] = (0.30, 0.20, 0.10)


@dataclass(frozen=True, slots=True)
class GateValidationConfig:
    horizons_ms: tuple[int, ...] = DEFAULT_HORIZONS_MS
    train_frac: float = DEFAULT_TRAIN_FRAC
    gate_pcts: tuple[float, ...] = DEFAULT_GATE_PCTS
    rt_cost_pt: float = DEFAULT_RT_COST_PT


def _stack(days: list[DayPanel], col: str) -> np.ndarray:
    return np.concatenate([d.cols[col] for d in days])


def _stack_features(days: list[DayPanel], side: int) -> np.ndarray:
    return np.concatenate([build_features(d.cols, side) for d in days], axis=0)


def _raw_ev(fill: np.ndarray, markout: np.ndarray) -> float:
    fills = (fill == 1) & np.isfinite(markout)
    n = int(fill.size)
    if n == 0:
        return float("nan")
    if not fills.any():
        return 0.0
    return float(fills.sum() / n) * float(markout[fills].mean())


def _quality_metrics(
    fill: np.ndarray,
    markout: np.ndarray,
    spread: np.ndarray,
    selected: np.ndarray,
    rt_cost_pt: float,
) -> dict:
    n = int(selected.sum())
    if n == 0:
        return {
            "n": 0,
            "sample_pct": 0.0,
            "fill_rate": float("nan"),
            "no_fill_rate": float("nan"),
            "adverse_rate_given_fill": float("nan"),
            "mean_markout_given_fill_pt": float("nan"),
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
    adverse_rate = (
        float((mark_s[fills] < 0.0).mean()) if n_fills else float("nan")
    )
    mean_markout = float(mark_s[fills].mean()) if n_fills else 0.0
    raw_ev = fill_rate * mean_markout
    return {
        "n": n,
        "sample_pct": float(n / selected.size),
        "fill_rate": float(fill_rate),
        "no_fill_rate": float(1.0 - fill_rate),
        "adverse_rate_given_fill": adverse_rate,
        "mean_markout_given_fill_pt": mean_markout,
        "raw_ev_pt": float(raw_ev),
        "net_ev_pt": float(raw_ev - rt_cost_pt),
        "avg_spread_pt": float(np.nanmean(spread_s)),
    }


def _gate_metrics(
    score: np.ndarray,
    fill: np.ndarray,
    markout: np.ndarray,
    spread: np.ndarray,
    gate_pct: float,
    rt_cost_pt: float,
) -> dict:
    threshold = float(np.quantile(score, 1.0 - gate_pct))
    passed = score >= threshold
    failed = ~passed
    pass_m = _quality_metrics(fill, markout, spread, passed, rt_cost_pt)
    fail_m = _quality_metrics(fill, markout, spread, failed, rt_cost_pt)
    return {
        "gate_pct": float(gate_pct),
        "threshold": threshold,
        "pass": pass_m,
        "fail": fail_m,
        "pass_minus_fail": {
            "fill_rate": pass_m["fill_rate"] - fail_m["fill_rate"],
            "adverse_rate_given_fill": (
                pass_m["adverse_rate_given_fill"] - fail_m["adverse_rate_given_fill"]
            ),
            "mean_markout_given_fill_pt": (
                pass_m["mean_markout_given_fill_pt"]
                - fail_m["mean_markout_given_fill_pt"]
            ),
            "raw_ev_pt": pass_m["raw_ev_pt"] - fail_m["raw_ev_pt"],
            "net_ev_pt": pass_m["net_ev_pt"] - fail_m["net_ev_pt"],
            "avg_spread_pt": pass_m["avg_spread_pt"] - fail_m["avg_spread_pt"],
        },
    }


def _per_day_stability(
    days: list[DayPanel],
    side: int,
    horizon_ms: int,
    fill_model,
    regress_model,
    gate_pct: float,
    score_mode: str,
    rt_cost_pt: float,
) -> dict:
    del rt_cost_pt
    side_word = "buy" if side > 0 else "sell"
    fill_col = f"filled_{side_word}_h{horizon_ms}"
    mark_col = f"markout_{side_word}_h{horizon_ms}"
    rows: list[dict] = []
    for day in days:
        X = build_features(day.cols, side)
        fill = np.asarray(day.cols[fill_col], dtype=np.int8)
        markout = np.asarray(day.cols[mark_col], dtype=np.float64)
        spread = np.asarray(day.cols["spread_pt"], dtype=np.float64)
        keep = np.all(np.isfinite(X), axis=1) & (fill != -1)
        if int(keep.sum()) < 500:
            rows.append({"date": day.date, "n": int(keep.sum()), "skipped": "small"})
            continue
        X_k = X[keep]
        fill_k = fill[keep]
        mark_k = markout[keep]
        spread_k = spread[keep]
        p_fill = fill_model.predict_proba(X_k)
        if score_mode == "simple":
            spread_z = fill_model.normalizer.transform(X_k)[:, 0]
            score = p_fill * spread_z
        elif score_mode == "full":
            score = p_fill * regress_model.predict(X_k)
        else:  # pragma: no cover
            raise ValueError(score_mode)
        threshold = float(np.quantile(score, 1.0 - gate_pct))
        passed = score >= threshold
        failed = ~passed
        pass_raw = _raw_ev(fill_k[passed], mark_k[passed])
        fail_raw = _raw_ev(fill_k[failed], mark_k[failed])
        rows.append(
            {
                "date": day.date,
                "n": int(keep.sum()),
                "pass_raw_ev_pt": pass_raw,
                "fail_raw_ev_pt": fail_raw,
                "pass_minus_fail_raw_ev_pt": float(pass_raw - fail_raw),
                "pass_avg_spread_pt": float(np.nanmean(spread_k[passed])),
                "fail_avg_spread_pt": float(np.nanmean(spread_k[failed])),
            }
        )
    diffs = np.asarray(
        [
            r["pass_minus_fail_raw_ev_pt"]
            for r in rows
            if "pass_minus_fail_raw_ev_pt" in r
        ],
        dtype=np.float64,
    )
    if diffs.size:
        abs_sum = float(np.abs(diffs).sum())
        max_share = float(np.max(np.abs(diffs)) / abs_sum) if abs_sum > 0 else 0.0
        sign_consistency = float((diffs > 0).mean())
        mean_diff = float(diffs.mean())
        median_diff = float(np.median(diffs))
        worst_idx = int(np.argmin(diffs))
        valid_rows = [r for r in rows if "pass_minus_fail_raw_ev_pt" in r]
        worst_day = valid_rows[worst_idx]["date"]
    else:
        max_share = float("nan")
        sign_consistency = float("nan")
        mean_diff = float("nan")
        median_diff = float("nan")
        worst_day = ""
    return {
        "score_mode": score_mode,
        "gate_pct": float(gate_pct),
        "summary": {
            "n_days": int(diffs.size),
            "sign_consistency_pos": sign_consistency,
            "mean_pass_minus_fail_raw_ev_pt": mean_diff,
            "median_pass_minus_fail_raw_ev_pt": median_diff,
            "max_single_day_share_abs": max_share,
            "worst_day": worst_day,
        },
        "per_day": rows,
    }


def _evaluate_one(
    test_days: list[DayPanel],
    side: int,
    horizon_ms: int,
    fill_model,
    regress_model,
    cfg: GateValidationConfig,
) -> dict:
    side_word = "buy" if side > 0 else "sell"
    fill_col = f"filled_{side_word}_h{horizon_ms}"
    mark_col = f"markout_{side_word}_h{horizon_ms}"

    X_raw = _stack_features(test_days, side)
    fill = _stack(test_days, fill_col).astype(np.int8)
    markout = _stack(test_days, mark_col).astype(np.float64)
    spread = _stack(test_days, "spread_pt").astype(np.float64)
    keep = np.all(np.isfinite(X_raw), axis=1) & (fill != -1)

    X = X_raw[keep]
    fill_k = fill[keep]
    mark_k = markout[keep]
    spread_k = spread[keep]

    p_fill = fill_model.predict_proba(X)
    spread_z = fill_model.normalizer.transform(X)[:, 0]
    simple_score = p_fill * spread_z
    full_score = p_fill * regress_model.predict(X)

    gate_rows = []
    for pct in cfg.gate_pcts:
        simple = _gate_metrics(
            simple_score, fill_k, mark_k, spread_k, pct, cfg.rt_cost_pt
        )
        full = _gate_metrics(full_score, fill_k, mark_k, spread_k, pct, cfg.rt_cost_pt)
        simple_diff = simple["pass_minus_fail"]["raw_ev_pt"]
        full_diff = full["pass_minus_fail"]["raw_ev_pt"]
        retention = simple_diff / full_diff if abs(full_diff) > 1e-12 else float("nan")
        simple_stability = _per_day_stability(
            test_days, side, horizon_ms, fill_model, regress_model, pct,
            "simple", cfg.rt_cost_pt,
        )
        full_stability = _per_day_stability(
            test_days, side, horizon_ms, fill_model, regress_model, pct,
            "full", cfg.rt_cost_pt,
        )
        gate_rows.append(
            {
                "gate_pct": float(pct),
                "simple": simple,
                "full": full,
                "simple_vs_full_retention_raw_ev": float(retention),
                "simple_stability": simple_stability["summary"],
                "full_stability": full_stability["summary"],
            }
        )
    return {
        "side": side_word,
        "horizon_ms": int(horizon_ms),
        "n_test_rows": int(X.shape[0]),
        "gate_results": gate_rows,
    }


def validate(
    synth_dir: Path,
    src_out: Path,
    out_dir: Path,
    cfg: GateValidationConfig | None = None,
) -> dict:
    cfg = cfg or GateValidationConfig()
    out_root = out_dir / "simple_gate_validation"
    out_root.mkdir(parents=True, exist_ok=True)

    days = load_days(synth_dir, min_fills=MIN_FILLS_PER_DAY)
    if len(days) < 4:
        raise RuntimeError(f"need >=4 days; got {len(days)}")
    n_train = max(2, int(len(days) * cfg.train_frac))
    test_days = days[n_train:]

    models_dir = src_out / "models"
    regress_dir = src_out / "models_regress"
    if not models_dir.exists():
        raise FileNotFoundError(f"fill models missing under {models_dir}")
    if not regress_dir.exists():
        raise FileNotFoundError(f"regression models missing under {regress_dir}")

    summary = {
        "synth_dir": str(synth_dir),
        "src_out": str(src_out),
        "rt_cost_pt": cfg.rt_cost_pt,
        "gate_pcts": list(cfg.gate_pcts),
        "test_dates": [d.date for d in test_days],
        "score_definitions": {
            "simple": "p_fill_hat * spread_z",
            "full": "p_fill_hat * pred_markout",
        },
        "results": [],
    }

    for side in (1, -1):
        side_word = "buy" if side > 0 else "sell"
        for h_ms in cfg.horizons_ms:
            fill_model = _load_model(models_dir, f"{side_word}_h{h_ms}_fill")
            regress_model = _load_regressor(
                regress_dir, f"{side_word}_h{h_ms}_markout"
            )
            log.info("simple gate %s h=%d", side_word, h_ms)
            result = _evaluate_one(test_days, side, h_ms, fill_model, regress_model, cfg)
            (out_root / f"{side_word}_h{h_ms}.json").write_text(
                json.dumps(result, indent=2)
            )
            summary["results"].append(result)

    (out_root / "summary.json").write_text(json.dumps(summary, indent=2))
    _write_report(out_root, summary)
    return summary


def _write_report(out_dir: Path, summary: dict) -> None:
    lines = [
        "# P2-V Maker Gate Validation — simple spread-fill gate",
        "",
        f"- synth_dir: `{summary['synth_dir']}`",
        f"- src_out: `{summary['src_out']}`",
        f"- rt_cost_pt: {summary['rt_cost_pt']:.2f}",
        f"- test dates: {summary['test_dates'][0]} -> {summary['test_dates'][-1]} "
        f"({len(summary['test_dates'])} days)",
        "",
        "Score definitions:",
        "",
        "```text",
        "simple = p_fill_hat * spread_z",
        "full   = p_fill_hat * pred_markout",
        "```",
        "",
        "## Gate Results",
        "",
        "| side | h(ms) | gate | simple ΔEV | full ΔEV | retention | "
        "simple sign | simple max_share | simple Δfill | simple Δspread |",
        "|------|-------|------|------------|---------|-----------|"
        "-------------|------------------|--------------|----------------|",
    ]
    for result in summary["results"]:
        for gate in result["gate_results"]:
            simple = gate["simple"]
            full = gate["full"]
            simple_diff = simple["pass_minus_fail"]
            simple_stab = gate["simple_stability"]
            lines.append(
                f"| {result['side']:<4} | {result['horizon_ms']:>5} | "
                f"top {int(gate['gate_pct'] * 100):>2}% | "
                f"{simple_diff['raw_ev_pt']:>+10.4f} | "
                f"{full['pass_minus_fail']['raw_ev_pt']:>+7.4f} | "
                f"{gate['simple_vs_full_retention_raw_ev']:>9.2f} | "
                f"{simple_stab['sign_consistency_pos']:>11.4f} | "
                f"{simple_stab['max_single_day_share_abs']:>16.4f} | "
                f"{simple_diff['fill_rate']:>+12.4f} | "
                f"{simple_diff['avg_spread_pt']:>+14.4f} |"
            )
    lines += [
        "",
        "## Reading Guide",
        "",
        "- `simple ΔEV` is pass-minus-fail unconditional raw EV in points.",
        "- `retention` is simple ΔEV divided by full-regression ΔEV.",
        "- Promote the simple gate if retention is >=0.80 for the relevant"
        " side/horizon and stability remains within R65-style gates.",
        "- This is a maker execution-quality audit, not a standalone alpha audit.",
    ]
    (out_dir / "REPORT.md").write_text("\n".join(lines))


def _parse_gate_pcts(raw: str) -> tuple[float, ...]:
    return tuple(float(x) for x in raw.split(",") if x.strip())


def _build_argparser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="simple_gate_validation")
    p.add_argument("--synth-dir", type=Path, required=True)
    p.add_argument("--src-out", type=Path, required=True)
    p.add_argument("--out", type=Path, required=True)
    p.add_argument(
        "--horizons-ms",
        type=str,
        default=",".join(str(h) for h in DEFAULT_HORIZONS_MS),
    )
    p.add_argument("--train-frac", type=float, default=DEFAULT_TRAIN_FRAC)
    p.add_argument(
        "--gate-pcts",
        type=str,
        default=",".join(str(p) for p in DEFAULT_GATE_PCTS),
    )
    p.add_argument("--rt-cost-pt", type=float, default=DEFAULT_RT_COST_PT)
    return p


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    args = _build_argparser().parse_args(argv)
    cfg = GateValidationConfig(
        horizons_ms=tuple(int(x) for x in args.horizons_ms.split(",") if x.strip()),
        train_frac=args.train_frac,
        gate_pcts=_parse_gate_pcts(args.gate_pcts),
        rt_cost_pt=args.rt_cost_pt,
    )
    summary = validate(args.synth_dir, args.src_out, args.out, cfg)
    log.info(
        "simple gate validation done; %d results -> %s",
        len(summary["results"]),
        (args.out / "simple_gate_validation" / "REPORT.md").as_posix(),
    )
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
