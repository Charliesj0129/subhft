"""Per-test-day stability audit on the composite EV predictor.

The R65 lesson is "global metrics can hide single-day dominance."
This module recomputes the composite ``p_good = p_fill * (1 - p_adv)``
per test day independently, deciles WITHIN each day, and reports:

  - sign consistency: fraction of days where top decile raw EV > bot
  - max single-day share of |sum(top - bot)| across days
  - per-day raw EV top / bot / diff

Also produces a spread-quintile composite breakdown (global ranking but
sliced by spread) so we can confirm the wide-spread cohort still passes
even after composite layering.

This is a post-training audit; no retraining.
"""
from __future__ import annotations

import argparse
import json
import logging
from dataclasses import dataclass
from pathlib import Path

import numpy as np

from research.experiments.p2_exec_predictor.composite_ev import (
    DEFAULT_N_DECILES,
    DEFAULT_RT_COST_PT,
    _load_model,
)
from research.experiments.p2_exec_predictor.markout_regression import LinearRegressor
from research.experiments.p2_exec_predictor.models import FeatureNormalizer
from research.experiments.p2_exec_predictor.train_eval import (
    DEFAULT_HORIZONS_MS,
    DEFAULT_TRAIN_FRAC,
    MIN_FILLS_PER_DAY,
    build_features,
    load_days,
)

log = logging.getLogger(__name__)

DEFAULT_N_SPREAD_QUINTILES: int = 5
SCORE_MODES = ("composite", "regression")


def _load_regressor(models_dir: Path, tag: str) -> LinearRegressor:
    payload = json.loads((models_dir / f"{tag}.json").read_text())
    weights = np.asarray(payload["weights"], dtype=np.float64)
    norm_mean = np.asarray(payload["norm_mean"], dtype=np.float64)
    norm_m2 = np.asarray(payload["norm_m2"], dtype=np.float64)
    norm_count = int(payload["norm_count"])
    return LinearRegressor(
        weights=weights,
        bias=float(payload["bias"]),
        normalizer=FeatureNormalizer(
            n_features=int(weights.size),
            mean=norm_mean,
            m2=norm_m2,
            count=norm_count,
        ),
        is_trained=bool(payload["is_trained"]),
    )


@dataclass(frozen=True, slots=True)
class StabilityConfig:
    horizons_ms: tuple[int, ...] = DEFAULT_HORIZONS_MS
    train_frac: float = DEFAULT_TRAIN_FRAC
    rt_cost_pt: float = DEFAULT_RT_COST_PT
    n_deciles: int = DEFAULT_N_DECILES
    n_spread_quintiles: int = DEFAULT_N_SPREAD_QUINTILES
    score_mode: str = "composite"  # composite | regression


def _compose_score(
    fill_model, second_model, X: np.ndarray, score_mode: str
) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Return (p_fill, second_signal, score).

    For ``composite``: second_signal = p_adverse, score = p_fill * (1 - p_adv).
    For ``regression``: second_signal = pred_markout, score = p_fill * pred_markout.
    """
    p_fill = fill_model.predict_proba(X)
    if score_mode == "composite":
        p_adv = second_model.predict_proba(X)
        return p_fill, p_adv, p_fill * (1.0 - p_adv)
    if score_mode == "regression":
        pred_markout = second_model.predict(X)
        return p_fill, pred_markout, p_fill * pred_markout
    raise ValueError(f"unknown score_mode {score_mode!r}; expected one of {SCORE_MODES}")


def _decile_raw_ev(
    score: np.ndarray,
    fill: np.ndarray,
    markout: np.ndarray,
    n_deciles: int,
    decile_idx: int,
) -> tuple[float, float, int]:
    """Return (raw_ev, fill_rate, n) for one decile of `score`."""
    if score.size == 0:
        return float("nan"), float("nan"), 0
    edges = np.quantile(score, np.linspace(0, 1, n_deciles + 1))
    edges[0] -= 1e-9
    edges[-1] += 1e-9
    bins = np.clip(np.digitize(score, edges) - 1, 0, n_deciles - 1)
    mask = bins == decile_idx
    n = int(mask.sum())
    if n == 0:
        return float("nan"), float("nan"), 0
    fill_mask = mask & (fill == 1) & np.isfinite(markout)
    n_fills = int(fill_mask.sum())
    fill_rate = n_fills / n
    e_mko = float(markout[fill_mask].mean()) if n_fills > 0 else 0.0
    return fill_rate * e_mko, fill_rate, n


def _per_day_audit(
    test_days,
    side: int,
    horizon_ms: int,
    fill_model,
    second_model,
    cfg: StabilityConfig,
) -> dict:
    side_word = "buy" if side > 0 else "sell"
    fill_col = f"filled_{side_word}_h{horizon_ms}"
    mark_col = f"markout_{side_word}_h{horizon_ms}"

    per_day: list[dict] = []
    for d in test_days:
        X = build_features(d.cols, side)
        finite_X = np.all(np.isfinite(X), axis=1)
        fill = np.asarray(d.cols[fill_col], dtype=np.int8)
        markout = np.asarray(d.cols[mark_col], dtype=np.float64)
        keep = finite_X & (fill != -1)
        X_k = X[keep]
        fill_k = fill[keep]
        markout_k = markout[keep]
        if X_k.shape[0] < cfg.n_deciles * 50:
            per_day.append(
                {"date": d.date, "n": int(X_k.shape[0]), "skipped": "small"}
            )
            continue
        _, _, p_good = _compose_score(fill_model, second_model, X_k, cfg.score_mode)
        top_raw, top_fr, top_n = _decile_raw_ev(
            p_good, fill_k, markout_k, cfg.n_deciles, cfg.n_deciles - 1
        )
        bot_raw, bot_fr, bot_n = _decile_raw_ev(
            p_good, fill_k, markout_k, cfg.n_deciles, 0
        )
        per_day.append(
            {
                "date": d.date,
                "n": int(X_k.shape[0]),
                "top_decile_raw_ev_pt": top_raw,
                "bot_decile_raw_ev_pt": bot_raw,
                "top_minus_bot_raw_ev_pt": top_raw - bot_raw,
                "top_fill_rate": top_fr,
                "bot_fill_rate": bot_fr,
                "top_n": top_n,
                "bot_n": bot_n,
            }
        )

    diffs = np.array(
        [r["top_minus_bot_raw_ev_pt"] for r in per_day if "top_minus_bot_raw_ev_pt" in r]
    )
    if diffs.size > 0:
        n_pos = int((diffs > 0).sum())
        sign_consistency = n_pos / diffs.size
        abs_sum = float(np.abs(diffs).sum())
        max_share = float(np.max(np.abs(diffs)) / abs_sum) if abs_sum > 0 else 0.0
        worst_day_idx = int(np.argmax(np.abs(diffs)))
        worst_day = [
            r for r in per_day if "top_minus_bot_raw_ev_pt" in r
        ][worst_day_idx]["date"]
    else:
        sign_consistency = float("nan")
        max_share = float("nan")
        worst_day = None

    return {
        "side": side_word,
        "horizon_ms": horizon_ms,
        "n_days": len(per_day),
        "per_day": per_day,
        "summary": {
            "n_days_with_diff": int(diffs.size),
            "sign_consistency_pos": sign_consistency,
            "mean_top_minus_bot_pt": float(diffs.mean()) if diffs.size else float("nan"),
            "median_top_minus_bot_pt": float(np.median(diffs))
            if diffs.size
            else float("nan"),
            "max_single_day_share_of_abs_sum": max_share,
            "worst_day": worst_day,
        },
    }


def _spread_quintile_audit(
    test_days,
    side: int,
    horizon_ms: int,
    fill_model,
    second_model,
    cfg: StabilityConfig,
) -> dict:
    """Concatenate test days, decile by p_good GLOBALLY, then per spread quintile
    report the top-vs-bot raw EV separation. Confirms wide-spread cohort still
    works after composite layering (matches the §7.7 Step 1 lesson)."""
    side_word = "buy" if side > 0 else "sell"
    fill_col = f"filled_{side_word}_h{horizon_ms}"
    mark_col = f"markout_{side_word}_h{horizon_ms}"

    X_parts: list[np.ndarray] = []
    fill_parts: list[np.ndarray] = []
    mark_parts: list[np.ndarray] = []
    spread_parts: list[np.ndarray] = []
    for d in test_days:
        X_parts.append(build_features(d.cols, side))
        fill_parts.append(np.asarray(d.cols[fill_col], dtype=np.int8))
        mark_parts.append(np.asarray(d.cols[mark_col], dtype=np.float64))
        spread_parts.append(np.asarray(d.cols["spread_pt"], dtype=np.float64))
    X = np.concatenate(X_parts, axis=0)
    fill = np.concatenate(fill_parts)
    markout = np.concatenate(mark_parts)
    spread = np.concatenate(spread_parts)

    finite_X = np.all(np.isfinite(X), axis=1)
    keep = finite_X & (fill != -1) & np.isfinite(spread)
    X_k = X[keep]
    fill_k = fill[keep]
    markout_k = markout[keep]
    spread_k = spread[keep]

    _, _, p_good = _compose_score(fill_model, second_model, X_k, cfg.score_mode)

    spread_edges = np.quantile(spread_k, np.linspace(0, 1, cfg.n_spread_quintiles + 1))
    spread_edges[0] -= 1e-9
    spread_edges[-1] += 1e-9
    spread_bins = np.clip(
        np.digitize(spread_k, spread_edges) - 1, 0, cfg.n_spread_quintiles - 1
    )

    quintiles: list[dict] = []
    for q in range(cfg.n_spread_quintiles):
        mask = spread_bins == q
        n = int(mask.sum())
        if n < cfg.n_deciles * 50:
            quintiles.append({"quintile": q, "n": n, "skipped": "small"})
            continue
        score_q = p_good[mask]
        fill_q = fill_k[mask]
        mko_q = markout_k[mask]
        spread_q_mean = float(spread_k[mask].mean())
        top_raw, top_fr, top_n = _decile_raw_ev(
            score_q, fill_q, mko_q, cfg.n_deciles, cfg.n_deciles - 1
        )
        bot_raw, bot_fr, bot_n = _decile_raw_ev(
            score_q, fill_q, mko_q, cfg.n_deciles, 0
        )
        quintiles.append(
            {
                "quintile": q,
                "mean_spread_pt": spread_q_mean,
                "n": n,
                "top_decile_raw_ev_pt": top_raw,
                "bot_decile_raw_ev_pt": bot_raw,
                "top_minus_bot_raw_ev_pt": top_raw - bot_raw,
                "top_fill_rate": top_fr,
                "bot_fill_rate": bot_fr,
            }
        )

    return {
        "side": side_word,
        "horizon_ms": horizon_ms,
        "n_quintiles": cfg.n_spread_quintiles,
        "n_total": int(X_k.shape[0]),
        "quintiles": quintiles,
    }


def audit(
    synth_dir: Path,
    out_dir: Path,
    cfg: StabilityConfig | None = None,
) -> dict:
    cfg = cfg or StabilityConfig()
    audit_subdir = (
        "stability_audit" if cfg.score_mode == "composite"
        else f"stability_audit_{cfg.score_mode}"
    )
    audit_dir = out_dir / audit_subdir
    audit_dir.mkdir(parents=True, exist_ok=True)
    models_dir = out_dir / "models"
    regress_dir = out_dir / "models_regress"
    if not models_dir.exists():
        raise FileNotFoundError(
            f"models dir missing at {models_dir} — run train_eval first"
        )
    if cfg.score_mode == "regression" and not regress_dir.exists():
        raise FileNotFoundError(
            f"models_regress missing at {regress_dir} — run markout_regression first"
        )

    days = load_days(synth_dir, min_fills=MIN_FILLS_PER_DAY)
    n_train_days = max(2, int(len(days) * cfg.train_frac))
    test_days = days[n_train_days:]
    log.info(
        "test split: %d days [%s..%s]",
        len(test_days),
        test_days[0].date,
        test_days[-1].date,
    )

    summary: dict = {
        "synth_dir": str(synth_dir),
        "rt_cost_pt": cfg.rt_cost_pt,
        "n_deciles": cfg.n_deciles,
        "n_spread_quintiles": cfg.n_spread_quintiles,
        "test_dates": [d.date for d in test_days],
        "per_day_results": [],
        "spread_quintile_results": [],
    }
    for side in (1, -1):
        for h_ms in cfg.horizons_ms:
            side_word = "buy" if side > 0 else "sell"
            tag_fill = f"{side_word}_h{h_ms}_fill"
            fill_model = _load_model(models_dir, tag_fill)
            if cfg.score_mode == "composite":
                tag_second = f"{side_word}_h{h_ms}_adverse"
                second_model = _load_model(models_dir, tag_second)
            else:
                tag_second = f"{side_word}_h{h_ms}_markout"
                second_model = _load_regressor(regress_dir, tag_second)
            log.info("stability-audit (%s) %s + %s", cfg.score_mode, tag_fill, tag_second)

            per_day = _per_day_audit(
                test_days, side, h_ms, fill_model, second_model, cfg
            )
            (audit_dir / f"per_day_{side_word}_h{h_ms}.json").write_text(
                json.dumps(per_day, indent=2)
            )
            summary["per_day_results"].append(
                {
                    "side": side_word,
                    "horizon_ms": h_ms,
                    **per_day["summary"],
                }
            )

            sq = _spread_quintile_audit(
                test_days, side, h_ms, fill_model, second_model, cfg
            )
            (audit_dir / f"spread_quintile_{side_word}_h{h_ms}.json").write_text(
                json.dumps(sq, indent=2)
            )
            short = {
                "side": side_word,
                "horizon_ms": h_ms,
                "by_quintile": [
                    {
                        "q": q.get("quintile"),
                        "spread_pt": q.get("mean_spread_pt"),
                        "n": q.get("n"),
                        "top_minus_bot": q.get("top_minus_bot_raw_ev_pt"),
                    }
                    for q in sq["quintiles"]
                ],
            }
            summary["spread_quintile_results"].append(short)

    (audit_dir / "summary.json").write_text(json.dumps(summary, indent=2))
    _write_report(audit_dir, summary)
    return summary


def _write_report(out_dir: Path, summary: dict) -> None:
    lines = [
        "# P2 Composite EV — Stability Audit",
        "",
        f"- rt_cost_pt: {summary['rt_cost_pt']:.2f}",
        f"- n_deciles: {summary['n_deciles']}, n_spread_quintiles: "
        f"{summary['n_spread_quintiles']}",
        f"- test dates: {summary['test_dates'][0]} → {summary['test_dates'][-1]} "
        f"({len(summary['test_dates'])} days)",
        "",
        "## Per-day stability (deciled WITHIN each test day)",
        "",
        "| side | h(ms) | n_days | sign_consistency | mean(top-bot) pt | "
        "median(top-bot) pt | max_single_day_share | worst_day |",
        "|------|-------|--------|------------------|------------------|"
        "--------------------|----------------------|-----------|",
    ]
    for r in summary["per_day_results"]:
        lines.append(
            f"| {r['side']:<4} | {r['horizon_ms']:>5} | "
            f"{r['n_days_with_diff']:>6d} | "
            f"{r['sign_consistency_pos']:>16.4f} | "
            f"{r['mean_top_minus_bot_pt']:>+16.4f} | "
            f"{r['median_top_minus_bot_pt']:>+18.4f} | "
            f"{r['max_single_day_share_of_abs_sum']:>20.4f} | "
            f"{r['worst_day']:>9s} |"
        )
    lines.append("")
    lines.append("**Reading guide.**")
    lines.append(
        "- `sign_consistency >= 0.7` and `max_single_day_share <= 0.4` ⇒ "
        "predictor is stable, not single-day dominated (R65 §7.1 gate)."
    )
    lines.append(
        "- `mean(top-bot)` close to global headline (composite_ev/REPORT.md) "
        "⇒ no daily averaging artefact."
    )
    lines.append("")
    lines.append("## Spread-quintile composite separation (global ranking, sliced by spread)")
    lines.append("")
    lines.append(
        "| side | h(ms) | "
        + " | ".join(
            f"q{q} (n / sp_pt / top-bot)" for q in range(summary["n_spread_quintiles"])
        )
        + " |"
    )
    lines.append("|------|-------|" + "|".join(["---"] * summary["n_spread_quintiles"]) + "|")
    for r in summary["spread_quintile_results"]:
        cells = []
        for q in r["by_quintile"]:
            if q.get("top_minus_bot") is None:
                cells.append("skipped")
            else:
                cells.append(
                    f"n={q['n']:>6d}  sp={q['spread_pt']:.2f}  d={q['top_minus_bot']:+.4f}"
                )
        lines.append(f"| {r['side']:<4} | {r['horizon_ms']:>5} | " + " | ".join(cells) + " |")
    (out_dir / "REPORT.md").write_text("\n".join(lines))


def _build_argparser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="stability_audit")
    p.add_argument("--synth-dir", type=Path, required=True)
    p.add_argument("--out", type=Path, required=True)
    p.add_argument(
        "--horizons-ms",
        type=str,
        default=",".join(str(h) for h in DEFAULT_HORIZONS_MS),
    )
    p.add_argument("--train-frac", type=float, default=DEFAULT_TRAIN_FRAC)
    p.add_argument("--rt-cost-pt", type=float, default=DEFAULT_RT_COST_PT)
    p.add_argument("--n-deciles", type=int, default=DEFAULT_N_DECILES)
    p.add_argument("--n-spread-quintiles", type=int, default=DEFAULT_N_SPREAD_QUINTILES)
    p.add_argument("--score-mode", choices=SCORE_MODES, default="composite")
    return p


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s"
    )
    args = _build_argparser().parse_args(argv)
    horizons = tuple(int(x) for x in args.horizons_ms.split(",") if x.strip())
    cfg = StabilityConfig(
        horizons_ms=horizons,
        train_frac=args.train_frac,
        rt_cost_pt=args.rt_cost_pt,
        n_deciles=args.n_deciles,
        n_spread_quintiles=args.n_spread_quintiles,
        score_mode=args.score_mode,
    )
    summary = audit(args.synth_dir, args.out, cfg)
    audit_subdir = (
        "stability_audit" if cfg.score_mode == "composite"
        else f"stability_audit_{cfg.score_mode}"
    )
    log.info(
        "done; per_day=%d quintile=%d -> %s",
        len(summary["per_day_results"]),
        len(summary["spread_quintile_results"]),
        (args.out / audit_subdir / "REPORT.md").as_posix(),
    )
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
