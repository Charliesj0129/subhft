"""Composite EV check — top-vs-bottom decile separation on the test panel.

For each (side, horizon):
  1. Reload the trained ``fill`` and ``adverse`` ``LogisticBinary`` models
     from ``<out>/models/``.
  2. Re-score every test-day snapshot:
       p_fill_hat               = sigmoid model_fill(x)
       p_adverse_given_fill_hat = sigmoid model_adverse(x)
       p_good_fill              = p_fill_hat * (1 - p_adverse_given_fill_hat)
  3. Bin test rows into 10 deciles of ``p_good_fill``.
  4. Per decile: n, mean predicted scores, **realized** fill rate,
     **realized** E[markout | fill] (pt), **realized** raw EV
     (= fill_rate × E[markout|fill]) and **realized** net EV
     (= raw_EV − ``rt_cost_pt``).
  5. Headline = top decile raw EV − bottom decile raw EV.
     Positive ⇒ the composite predictor materially orders snapshots
     by ex-post realised maker quality. Zero / negative ⇒ component
     models don't combine cleanly.

This is post-training validation; nothing is wired into live code.
"""
from __future__ import annotations

import argparse
import json
import logging
from dataclasses import dataclass
from pathlib import Path

import numpy as np

from research.experiments.p2_exec_predictor.models import (
    FeatureNormalizer,
    LogisticBinary,
)
from research.experiments.p2_exec_predictor.train_eval import (
    DEFAULT_HORIZONS_MS,
    DEFAULT_TRAIN_FRAC,
    MIN_FILLS_PER_DAY,
    build_features,
    load_days,
)

log = logging.getLogger(__name__)

DEFAULT_RT_COST_PT: float = 4.0  # TMFD6 retail RT (1.3 comm + 0.7 tax) × 2
DEFAULT_N_DECILES: int = 10


@dataclass(frozen=True, slots=True)
class CompositeConfig:
    horizons_ms: tuple[int, ...] = DEFAULT_HORIZONS_MS
    train_frac: float = DEFAULT_TRAIN_FRAC
    rt_cost_pt: float = DEFAULT_RT_COST_PT
    n_deciles: int = DEFAULT_N_DECILES


def _load_model(models_dir: Path, tag: str) -> LogisticBinary:
    payload = json.loads((models_dir / f"{tag}.json").read_text())
    weights = np.asarray(payload["weights"], dtype=np.float64)
    norm_mean = np.asarray(payload["norm_mean"], dtype=np.float64)
    norm_m2 = np.asarray(payload["norm_m2"], dtype=np.float64)
    norm_count = int(payload["norm_count"])
    normalizer = FeatureNormalizer(
        n_features=int(weights.size),
        mean=norm_mean,
        m2=norm_m2,
        count=norm_count,
    )
    return LogisticBinary(
        weights=weights,
        bias=float(payload["bias"]),
        normalizer=normalizer,
        is_trained=bool(payload["is_trained"]),
    )


def _decile_table(
    p_good: np.ndarray,
    fill: np.ndarray,
    markout: np.ndarray,
    p_fill: np.ndarray,
    p_adv_given_fill: np.ndarray,
    rt_cost_pt: float,
    n_deciles: int,
) -> list[dict[str, float | int]]:
    if p_good.size == 0:
        return []
    edges = np.quantile(p_good, np.linspace(0, 1, n_deciles + 1))
    edges[0] -= 1e-9
    edges[-1] += 1e-9
    bins = np.clip(np.digitize(p_good, edges) - 1, 0, n_deciles - 1)
    out: list[dict[str, float | int]] = []
    for b in range(n_deciles):
        mask = bins == b
        n = int(mask.sum())
        if n == 0:
            continue
        fill_mask = mask & (fill == 1) & np.isfinite(markout)
        n_fills = int(fill_mask.sum())
        realized_fill_rate = n_fills / n if n else 0.0
        realized_e_markout_given_fill = (
            float(markout[fill_mask].mean()) if n_fills > 0 else 0.0
        )
        raw_ev = realized_fill_rate * realized_e_markout_given_fill
        net_ev = raw_ev - rt_cost_pt
        out.append(
            {
                "decile": int(b),
                "n": n,
                "n_fills": n_fills,
                "p_good_mean": float(p_good[mask].mean()),
                "p_fill_mean": float(p_fill[mask].mean()),
                "p_adv_mean": float(p_adv_given_fill[mask].mean()),
                "realized_fill_rate": realized_fill_rate,
                "realized_e_markout_given_fill_pt": realized_e_markout_given_fill,
                "realized_raw_ev_pt": raw_ev,
                "realized_net_ev_pt": net_ev,
            }
        )
    return out


def _eval_one(
    test_days,
    side: int,
    horizon_ms: int,
    fill_model: LogisticBinary,
    adv_model: LogisticBinary,
    cfg: CompositeConfig,
) -> dict:
    side_word = "buy" if side > 0 else "sell"
    fill_col = f"filled_{side_word}_h{horizon_ms}"
    mark_col = f"markout_{side_word}_h{horizon_ms}"

    X_parts: list[np.ndarray] = []
    fill_parts: list[np.ndarray] = []
    mark_parts: list[np.ndarray] = []
    for d in test_days:
        X_parts.append(build_features(d.cols, side))
        fill_parts.append(np.asarray(d.cols[fill_col], dtype=np.int8))
        mark_parts.append(np.asarray(d.cols[mark_col], dtype=np.float64))
    X = np.concatenate(X_parts, axis=0)
    fill = np.concatenate(fill_parts)
    markout = np.concatenate(mark_parts)

    finite_X = np.all(np.isfinite(X), axis=1)
    keep = finite_X & (fill != -1)
    X = X[keep]
    fill = fill[keep]
    markout = markout[keep]

    p_fill = fill_model.predict_proba(X)
    p_adv = adv_model.predict_proba(X)
    p_good = p_fill * (1.0 - p_adv)

    deciles = _decile_table(
        p_good, fill, markout, p_fill, p_adv,
        rt_cost_pt=cfg.rt_cost_pt,
        n_deciles=cfg.n_deciles,
    )
    if deciles:
        top = deciles[-1]
        bot = deciles[0]
        sep = {
            "top_decile_raw_ev_pt": top["realized_raw_ev_pt"],
            "bot_decile_raw_ev_pt": bot["realized_raw_ev_pt"],
            "top_minus_bot_raw_ev_pt": top["realized_raw_ev_pt"]
            - bot["realized_raw_ev_pt"],
            "top_decile_fill_rate": top["realized_fill_rate"],
            "bot_decile_fill_rate": bot["realized_fill_rate"],
            "top_decile_e_markout_given_fill_pt": top[
                "realized_e_markout_given_fill_pt"
            ],
            "bot_decile_e_markout_given_fill_pt": bot[
                "realized_e_markout_given_fill_pt"
            ],
        }
    else:
        sep = {}
    return {
        "side": side_word,
        "horizon_ms": horizon_ms,
        "n_test_rows": int(X.shape[0]),
        "rt_cost_pt": cfg.rt_cost_pt,
        "deciles": deciles,
        "separation": sep,
    }


def evaluate(
    synth_dir: Path,
    out_dir: Path,
    cfg: CompositeConfig | None = None,
) -> dict:
    cfg = cfg or CompositeConfig()
    composite_dir = out_dir / "composite_ev"
    composite_dir.mkdir(parents=True, exist_ok=True)
    models_dir = out_dir / "models"
    if not models_dir.exists():
        raise FileNotFoundError(
            f"models dir missing at {models_dir} — run train_eval first"
        )

    days = load_days(synth_dir, min_fills=MIN_FILLS_PER_DAY)
    if len(days) < 4:
        raise RuntimeError(f"need >=4 days for OOS split; got {len(days)}")
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
        "test_dates": [d.date for d in test_days],
        "results": [],
    }
    for side in (1, -1):
        for h_ms in cfg.horizons_ms:
            side_word = "buy" if side > 0 else "sell"
            tag_fill = f"{side_word}_h{h_ms}_fill"
            tag_adv = f"{side_word}_h{h_ms}_adverse"
            log.info("composite-eval %s + %s", tag_fill, tag_adv)
            fill_model = _load_model(models_dir, tag_fill)
            adv_model = _load_model(models_dir, tag_adv)
            report = _eval_one(test_days, side, h_ms, fill_model, adv_model, cfg)
            (composite_dir / f"{side_word}_h{h_ms}.json").write_text(
                json.dumps(report, indent=2)
            )
            short = {
                "side": side_word,
                "horizon_ms": h_ms,
                "n_test_rows": report["n_test_rows"],
                **report["separation"],
            }
            summary["results"].append(short)

    (composite_dir / "summary.json").write_text(json.dumps(summary, indent=2))
    _write_report(composite_dir, summary)
    return summary


def _write_report(out_dir: Path, summary: dict) -> None:
    lines = [
        "# P2 Composite EV — top-vs-bottom decile separation",
        "",
        f"- rt_cost_pt: **{summary['rt_cost_pt']:.2f}** (TMFD6 RT retail)",
        f"- n_deciles: {summary['n_deciles']}",
        f"- test dates: {summary['test_dates'][0]} → {summary['test_dates'][-1]}"
        f" ({len(summary['test_dates'])} days)",
        "",
        "## Headline (raw EV in pt = fill_rate × E[markout|fill])",
        "",
        "| side | h(ms) | top    | bot    | top - bot | top fill_rate | bot fill_rate | top E[mko|fill] | bot E[mko|fill] |",
        "|------|-------|--------|--------|-----------|---------------|---------------|-----------------|-----------------|",
    ]
    for r in summary["results"]:
        lines.append(
            f"| {r['side']:<4} | {r['horizon_ms']:>5} | "
            f"{r.get('top_decile_raw_ev_pt', float('nan')):>+6.4f} | "
            f"{r.get('bot_decile_raw_ev_pt', float('nan')):>+6.4f} | "
            f"{r.get('top_minus_bot_raw_ev_pt', float('nan')):>+8.4f}  | "
            f"{r.get('top_decile_fill_rate', float('nan')):>13.4f} | "
            f"{r.get('bot_decile_fill_rate', float('nan')):>13.4f} | "
            f"{r.get('top_decile_e_markout_given_fill_pt', float('nan')):>+15.4f} | "
            f"{r.get('bot_decile_e_markout_given_fill_pt', float('nan')):>+15.4f} |"
        )
    lines.append("")
    lines.append("**Reading guide.**")
    lines.append(
        "- `top - bot > 0` ⇒ predictor combination orders snapshots by realised maker quality."
    )
    lines.append(
        "- A positive separation ≪ rt_cost_pt is still informative — it"
        " demonstrates the predictor produces a real ranking; whether it"
        " clears costs is a separate per-attempt unit-economics question."
    )
    lines.append(
        "- `realized E[markout|fill]` near 0 means a fill is roughly a coin-flip"
        " on mid drift — typical for liquid futures markets at sub-second horizons."
    )
    (out_dir / "REPORT.md").write_text("\n".join(lines))


def _build_argparser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="composite_ev")
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
    return p


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s"
    )
    args = _build_argparser().parse_args(argv)
    horizons = tuple(int(x) for x in args.horizons_ms.split(",") if x.strip())
    cfg = CompositeConfig(
        horizons_ms=horizons,
        train_frac=args.train_frac,
        rt_cost_pt=args.rt_cost_pt,
        n_deciles=args.n_deciles,
    )
    summary = evaluate(args.synth_dir, args.out, cfg)
    log.info(
        "done; %d results -> %s",
        len(summary["results"]),
        (args.out / "composite_ev" / "REPORT.md").as_posix(),
    )
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
