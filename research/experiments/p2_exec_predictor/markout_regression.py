"""Continuous markout regression — replace the binary adverse classifier
with a linear regression of *realized markout in points* on the same 7
features, then re-score the composite as ``p_fill_hat * markout_hat``.

Why
---
The composite-EV stability audit (`stability_audit.py`) showed that a
binary `adverse` threshold may be hiding the marginal gradient of the
markout. A continuous regressor predicts markout magnitude directly and
turns the composite into a raw EV proxy in points, with no threshold
choice to worry about.

The regressor is trained on **filled rows only** (``fill == 1`` and
markout finite). Loss is MSE with L2 regularization. Self-contained
implementation, no sklearn.

Composite for the decile + stability audit:
    score = p_fill_hat * markout_hat        # in points
The decile is taken on `score` (not on `p_fill_hat * (1 - p_adv)` as in
``composite_ev.py``).
"""
from __future__ import annotations

import argparse
import json
import logging
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np

from research.experiments.p2_exec_predictor.composite_ev import _load_model
from research.experiments.p2_exec_predictor.models import (
    FeatureNormalizer,
    compute_auc,
)
from research.experiments.p2_exec_predictor.train_eval import (
    DEFAULT_HORIZONS_MS,
    DEFAULT_TRAIN_FRAC,
    FEATURE_NAMES,
    MIN_FILLS_PER_DAY,
    build_features,
    load_days,
)

log = logging.getLogger(__name__)

DEFAULT_RT_COST_PT: float = 4.0
DEFAULT_N_DECILES: int = 10


@dataclass(frozen=True, slots=True)
class RegressConfig:
    horizons_ms: tuple[int, ...] = DEFAULT_HORIZONS_MS
    train_frac: float = DEFAULT_TRAIN_FRAC
    rt_cost_pt: float = DEFAULT_RT_COST_PT
    n_deciles: int = DEFAULT_N_DECILES
    max_iter: int = 200
    lr: float = 0.05
    l2_lambda: float = 0.05


@dataclass(slots=True)
class LinearRegressor:
    """Linear regression with L2 reg, GD, batch z-score normalization."""

    weights: np.ndarray = field(default_factory=lambda: np.zeros(0))
    bias: float = 0.0
    normalizer: FeatureNormalizer = field(default_factory=FeatureNormalizer)
    is_trained: bool = False

    def fit(
        self,
        X: np.ndarray,
        y: np.ndarray,
        *,
        max_iter: int = 200,
        lr: float = 0.05,
        l2_lambda: float = 0.05,
        tol: float = 1e-7,
    ) -> dict[str, float | int | bool]:
        if X.ndim != 2 or y.ndim != 1 or X.shape[0] != y.shape[0]:
            raise ValueError(f"shape mismatch X={X.shape} y={y.shape}")
        n, d = X.shape
        if n < 50:
            return {"error_insufficient_data": True, "n": n}
        self.normalizer = FeatureNormalizer.fit(X)
        Xn = self.normalizer.transform(X)
        w = np.zeros(d, dtype=np.float64)
        b = float(y.mean())
        prev_loss = float("inf")
        loss = float("inf")
        n_iter = 0
        for n_iter in range(1, max_iter + 1):
            pred = Xn @ w + b
            error = pred - y
            loss = 0.5 * float(np.mean(error ** 2)) + 0.5 * l2_lambda * float(w @ w)
            grad_w = (Xn.T @ error) / n + l2_lambda * w
            grad_b = float(error.mean())
            w -= lr * grad_w
            b -= lr * grad_b
            if abs(prev_loss - loss) < tol:
                break
            prev_loss = loss

        self.weights = w
        self.bias = b
        self.is_trained = True

        pred = Xn @ w + b
        ss_res = float(np.sum((y - pred) ** 2))
        ss_tot = float(np.sum((y - y.mean()) ** 2))
        r2 = 1.0 - ss_res / ss_tot if ss_tot > 0 else 0.0
        return {
            "n_samples": int(n),
            "n_features": int(d),
            "iterations": int(n_iter),
            "final_loss": float(loss),
            "r2": float(r2),
            "y_mean": float(y.mean()),
            "y_std": float(y.std()),
        }

    def predict(self, X: np.ndarray) -> np.ndarray:
        if not self.is_trained:
            return np.zeros(X.shape[0], dtype=np.float64)
        Xn = self.normalizer.transform(X)
        return Xn @ self.weights + self.bias

    def to_dict(self) -> dict:
        return {
            "weights": self.weights.tolist(),
            "bias": float(self.bias),
            "norm_mean": self.normalizer.mean.tolist(),
            "norm_m2": self.normalizer.m2.tolist(),
            "norm_count": int(self.normalizer.count),
            "is_trained": bool(self.is_trained),
        }


def _stack(days, col: str) -> np.ndarray:
    return np.concatenate([d.cols[col] for d in days])


def _stack_features(days, side: int) -> np.ndarray:
    return np.concatenate([build_features(d.cols, side) for d in days], axis=0)


def _decile_table(
    score: np.ndarray,
    fill: np.ndarray,
    markout: np.ndarray,
    pred_markout: np.ndarray,
    p_fill: np.ndarray,
    rt_cost_pt: float,
    n_deciles: int,
) -> list[dict]:
    if score.size == 0:
        return []
    edges = np.quantile(score, np.linspace(0, 1, n_deciles + 1))
    edges[0] -= 1e-9
    edges[-1] += 1e-9
    bins = np.clip(np.digitize(score, edges) - 1, 0, n_deciles - 1)
    out: list[dict] = []
    for b in range(n_deciles):
        mask = bins == b
        n = int(mask.sum())
        if n == 0:
            continue
        fill_mask = mask & (fill == 1) & np.isfinite(markout)
        n_fills = int(fill_mask.sum())
        realized_fill_rate = n_fills / n if n else 0.0
        realized_e_markout = (
            float(markout[fill_mask].mean()) if n_fills > 0 else 0.0
        )
        raw_ev = realized_fill_rate * realized_e_markout
        out.append(
            {
                "decile": int(b),
                "n": n,
                "n_fills": n_fills,
                "score_mean": float(score[mask].mean()),
                "p_fill_mean": float(p_fill[mask].mean()),
                "pred_markout_mean": float(pred_markout[mask].mean()),
                "realized_fill_rate": realized_fill_rate,
                "realized_e_markout_given_fill_pt": realized_e_markout,
                "realized_raw_ev_pt": raw_ev,
                "realized_net_ev_pt": raw_ev - rt_cost_pt,
            }
        )
    return out


def _train_one(
    train_days,
    test_days,
    side: int,
    horizon_ms: int,
    fill_model,
    cfg: RegressConfig,
    out_dir: Path,
) -> dict:
    side_word = "buy" if side > 0 else "sell"
    fill_col = f"filled_{side_word}_h{horizon_ms}"
    mark_col = f"markout_{side_word}_h{horizon_ms}"

    X_train_raw = _stack_features(train_days, side)
    fill_train = _stack(train_days, fill_col).astype(np.int8)
    mark_train = _stack(train_days, mark_col).astype(np.float64)

    train_finite_X = np.all(np.isfinite(X_train_raw), axis=1)
    train_keep = (
        train_finite_X & (fill_train == 1) & np.isfinite(mark_train)
    )
    X_train = X_train_raw[train_keep]
    y_train = mark_train[train_keep]
    if X_train.shape[0] < 500:
        return {
            "side": side_word,
            "horizon_ms": horizon_ms,
            "skipped_reason": "insufficient_train_fills",
            "n_train": int(X_train.shape[0]),
        }

    regressor = LinearRegressor()
    train_metrics = regressor.fit(
        X_train,
        y_train,
        max_iter=cfg.max_iter,
        lr=cfg.lr,
        l2_lambda=cfg.l2_lambda,
    )

    X_test_raw = _stack_features(test_days, side)
    fill_test = _stack(test_days, fill_col).astype(np.int8)
    mark_test = _stack(test_days, mark_col).astype(np.float64)
    test_finite_X = np.all(np.isfinite(X_test_raw), axis=1)
    test_keep = test_finite_X & (fill_test != -1)
    X_test = X_test_raw[test_keep]
    fill_test_k = fill_test[test_keep]
    mark_test_k = mark_test[test_keep]

    pred_markout = regressor.predict(X_test)
    p_fill = fill_model.predict_proba(X_test)
    score = p_fill * pred_markout

    deciles = _decile_table(
        score, fill_test_k, mark_test_k, pred_markout, p_fill,
        rt_cost_pt=cfg.rt_cost_pt, n_deciles=cfg.n_deciles,
    )
    if deciles:
        top = deciles[-1]
        bot = deciles[0]
        sep = {
            "top_decile_raw_ev_pt": top["realized_raw_ev_pt"],
            "bot_decile_raw_ev_pt": bot["realized_raw_ev_pt"],
            "top_minus_bot_raw_ev_pt": top["realized_raw_ev_pt"]
            - bot["realized_raw_ev_pt"],
            "top_decile_pred_markout": top["pred_markout_mean"],
            "bot_decile_pred_markout": bot["pred_markout_mean"],
            "top_decile_e_markout_given_fill_pt": top[
                "realized_e_markout_given_fill_pt"
            ],
            "bot_decile_e_markout_given_fill_pt": bot[
                "realized_e_markout_given_fill_pt"
            ],
        }
    else:
        sep = {}

    fill_filter = (fill_test_k == 1) & np.isfinite(mark_test_k)
    if fill_filter.sum() > 0:
        pred_on_fills = pred_markout[fill_filter]
        true_on_fills = mark_test_k[fill_filter]
        ss_res = float(np.sum((true_on_fills - pred_on_fills) ** 2))
        ss_tot = float(np.sum((true_on_fills - true_on_fills.mean()) ** 2))
        test_r2 = 1.0 - ss_res / ss_tot if ss_tot > 0 else 0.0
        pearson = (
            float(np.corrcoef(pred_on_fills, true_on_fills)[0, 1])
            if pred_on_fills.size >= 2
            else 0.0
        )
        positive = (true_on_fills > 0).astype(np.float64)
        discr_auc = float(compute_auc(positive, pred_on_fills))
    else:
        test_r2 = float("nan")
        pearson = float("nan")
        discr_auc = float("nan")

    (out_dir / "models_regress").mkdir(exist_ok=True)
    (out_dir / "models_regress" / f"{side_word}_h{horizon_ms}_markout.json").write_text(
        json.dumps(regressor.to_dict(), indent=2)
    )

    return {
        "side": side_word,
        "horizon_ms": horizon_ms,
        "feature_names": list(FEATURE_NAMES),
        "n_train_fills": int(X_train.shape[0]),
        "n_test_rows": int(X_test.shape[0]),
        "n_test_fills": int(fill_filter.sum()),
        "train_metrics": train_metrics,
        "test_r2_on_fills": test_r2,
        "test_pearson_on_fills": pearson,
        "test_discr_auc_markout_pos_on_fills": discr_auc,
        "deciles": deciles,
        "separation": sep,
    }


def train_and_evaluate(
    synth_dir: Path,
    out_dir: Path,
    cfg: RegressConfig | None = None,
) -> dict:
    cfg = cfg or RegressConfig()
    regress_dir = out_dir / "markout_regression"
    regress_dir.mkdir(parents=True, exist_ok=True)
    models_dir = out_dir / "models"
    if not models_dir.exists():
        raise FileNotFoundError(
            f"models dir missing at {models_dir} — run train_eval first"
        )

    days = load_days(synth_dir, min_fills=MIN_FILLS_PER_DAY)
    if len(days) < 4:
        raise RuntimeError(f"need >=4 days for OOS split; got {len(days)}")
    n_train_days = max(2, int(len(days) * cfg.train_frac))
    train_days = days[:n_train_days]
    test_days = days[n_train_days:]
    log.info(
        "split: train=%d [%s..%s] test=%d [%s..%s]",
        len(train_days),
        train_days[0].date,
        train_days[-1].date,
        len(test_days),
        test_days[0].date,
        test_days[-1].date,
    )

    summary: dict = {
        "synth_dir": str(synth_dir),
        "rt_cost_pt": cfg.rt_cost_pt,
        "n_deciles": cfg.n_deciles,
        "n_train_days": len(train_days),
        "n_test_days": len(test_days),
        "test_dates": [d.date for d in test_days],
        "results": [],
    }
    for side in (1, -1):
        for h_ms in cfg.horizons_ms:
            side_word = "buy" if side > 0 else "sell"
            fill_tag = f"{side_word}_h{h_ms}_fill"
            log.info("regress %s + reuse fill model %s", side_word, fill_tag)
            fill_model = _load_model(models_dir, fill_tag)
            report = _train_one(
                train_days, test_days, side, h_ms, fill_model, cfg, out_dir
            )
            (regress_dir / f"{side_word}_h{h_ms}.json").write_text(
                json.dumps(report, indent=2)
            )
            short = {
                "side": side_word,
                "horizon_ms": h_ms,
                "n_test_fills": report.get("n_test_fills"),
                "test_r2_on_fills": report.get("test_r2_on_fills"),
                "test_pearson_on_fills": report.get("test_pearson_on_fills"),
                "test_discr_auc_markout_pos_on_fills": report.get(
                    "test_discr_auc_markout_pos_on_fills"
                ),
                **report.get("separation", {}),
            }
            summary["results"].append(short)

    (regress_dir / "summary.json").write_text(json.dumps(summary, indent=2))
    _write_report(regress_dir, summary)
    return summary


def _write_report(out_dir: Path, summary: dict) -> None:
    lines = [
        "# P2 Markout Regression — composite EV via continuous predictor",
        "",
        f"- rt_cost_pt: {summary['rt_cost_pt']:.2f}",
        f"- n_deciles: {summary['n_deciles']}",
        f"- test dates: {summary['test_dates'][0]} → {summary['test_dates'][-1]} "
        f"({len(summary['test_dates'])} days)",
        "",
        "Composite score: `p_fill_hat × pred_markout` (no threshold).",
        "",
        "## Predictor quality on filled rows",
        "",
        "| side | h(ms) | n_test_fills | r² | pearson | discr_auc(mko>0) |",
        "|------|-------|--------------|----|---------|------------------|",
    ]
    for r in summary["results"]:
        lines.append(
            f"| {r['side']:<4} | {r['horizon_ms']:>5} | "
            f"{r['n_test_fills']:>12d} | "
            f"{r['test_r2_on_fills']:>+5.3f} | "
            f"{r['test_pearson_on_fills']:>+7.3f} | "
            f"{r['test_discr_auc_markout_pos_on_fills']:>16.4f} |"
        )
    lines.append("")
    lines.append("## Composite EV — top vs bottom decile (raw EV in pt)")
    lines.append("")
    lines.append(
        "| side | h(ms) | top    | bot    | top - bot | "
        "top pred_mko | bot pred_mko | top E[mko\\|fill] | bot E[mko\\|fill] |"
    )
    lines.append(
        "|------|-------|--------|--------|-----------|"
        "--------------|--------------|------------------|------------------|"
    )
    for r in summary["results"]:
        lines.append(
            f"| {r['side']:<4} | {r['horizon_ms']:>5} | "
            f"{r.get('top_decile_raw_ev_pt', float('nan')):>+6.4f} | "
            f"{r.get('bot_decile_raw_ev_pt', float('nan')):>+6.4f} | "
            f"{r.get('top_minus_bot_raw_ev_pt', float('nan')):>+8.4f}  | "
            f"{r.get('top_decile_pred_markout', float('nan')):>+12.4f} | "
            f"{r.get('bot_decile_pred_markout', float('nan')):>+12.4f} | "
            f"{r.get('top_decile_e_markout_given_fill_pt', float('nan')):>+16.4f} | "
            f"{r.get('bot_decile_e_markout_given_fill_pt', float('nan')):>+16.4f} |"
        )
    (out_dir / "REPORT.md").write_text("\n".join(lines))


def _build_argparser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="markout_regression")
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
    cfg = RegressConfig(
        horizons_ms=horizons,
        train_frac=args.train_frac,
        rt_cost_pt=args.rt_cost_pt,
        n_deciles=args.n_deciles,
    )
    summary = train_and_evaluate(args.synth_dir, args.out, cfg)
    log.info(
        "done; %d results -> %s",
        len(summary["results"]),
        (args.out / "markout_regression" / "REPORT.md").as_posix(),
    )
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
