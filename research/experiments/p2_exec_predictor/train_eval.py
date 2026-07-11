"""Train + day-OOS evaluate the P2 fill / adverse-selection predictors.

Pipeline
--------
1. Load all per-day fill-event ``.npz`` files (from
   ``synth_fill_events.synth_panel``) under ``--synth-dir``.
2. Day-OOS split: first 70 % of active trading days = train,
   last 30 % = test (chronological).
3. For each (side, horizon, target) train a ``LogisticBinary``:
     side    ∈ {buy, sell}
     horizon ∈ {500, 2000, 5000}
     target  ∈ {fill, adverse}        # adverse = filled & markout < adverse_threshold_pt
4. Evaluate on test: AUC, Brier, calibration by decile, stratified AUC
   by spread quintile (the §7.7 Step 1 lesson — detect cohort flip
   before any downstream use).
5. Write per-target eval JSON + a summary REPORT.md.

Usage
-----
    uv run python -m research.experiments.p2_exec_predictor.train_eval \\
        --synth-dir research/data/derived/p2_fill_events_tmf_smoke \\
        --out outputs/p2_exec_predictor/tmf
"""
from __future__ import annotations

import argparse
import json
import logging
from dataclasses import dataclass
from pathlib import Path

import numpy as np

from research.experiments.p2_exec_predictor.models import (
    LogisticBinary,
    calibration_by_decile,
    compute_auc,
    stratified_metric,
)

log = logging.getLogger(__name__)

DEFAULT_HORIZONS_MS: tuple[int, ...] = (500, 2000, 5000)
DEFAULT_ADVERSE_THRESHOLD_PT: float = 0.5  # markout < -0.5 pt = adverse for the maker
DEFAULT_TRAIN_FRAC: float = 0.7
# Drop non-trading days — gridded snapshots on holidays carry stale book
# state forward but produce zero fills, which would inflate sample size and
# degrade the predictor. A real TMF trading day produces >>1000 fills.
MIN_FILLS_PER_DAY: int = 200

FEATURE_NAMES: tuple[str, ...] = (
    "spread_pt",
    "depth_imb_signed",
    "ofi_signed",
    "queue_ratio_near_far",
    "vol",
    "churn",
    "toxicity_signed",
)


@dataclass(frozen=True, slots=True)
class TrainConfig:
    horizons_ms: tuple[int, ...] = DEFAULT_HORIZONS_MS
    adverse_threshold_pt: float = DEFAULT_ADVERSE_THRESHOLD_PT
    train_frac: float = DEFAULT_TRAIN_FRAC


@dataclass(frozen=True, slots=True)
class DayPanel:
    date: str
    cols: dict[str, np.ndarray]


def _is_active_day(cols: dict[str, np.ndarray], min_fills: int) -> bool:
    """Active iff the union of all-horizon fill counts exceeds min_fills."""
    total = 0
    for k, v in cols.items():
        if k.startswith(("filled_buy_", "filled_sell_")):
            total += int((v == 1).sum())
    return total >= min_fills


def load_days(synth_dir: Path, min_fills: int = MIN_FILLS_PER_DAY) -> list[DayPanel]:
    paths = sorted(synth_dir.glob("[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9].npz"))
    if not paths:
        raise FileNotFoundError(f"no per-day fill-event npz found under {synth_dir}")
    out: list[DayPanel] = []
    skipped = 0
    for p in paths:
        with np.load(p, allow_pickle=False) as loaded:
            cols = {k: np.asarray(loaded[k]).copy() for k in loaded.files}
        if not _is_active_day(cols, min_fills):
            skipped += 1
            continue
        out.append(DayPanel(date=p.stem, cols=cols))
    log.info("load_days kept=%d skipped_inactive=%d", len(out), skipped)
    return out


def _signed(arr: np.ndarray, side: int) -> np.ndarray:
    return arr * float(side)


def _queue_ratio(bid_qty: np.ndarray, ask_qty: np.ndarray, side: int) -> np.ndarray:
    if side > 0:
        near = bid_qty
        far = ask_qty
    else:
        near = ask_qty
        far = bid_qty
    safe_far = np.where(far > 0, far, 1.0)
    return near / safe_far


def build_features(cols: dict[str, np.ndarray], side: int) -> np.ndarray:
    """Build the 7-feature matrix for one side. Rows aligned to t_ns."""
    spread = cols["spread_pt"].astype(np.float64)
    depth = cols["depth_imbalance"].astype(np.float64)
    ofi = cols["ofi"].astype(np.float64)
    vol = cols["vol"].astype(np.float64)
    churn = cols["churn"].astype(np.float64)
    tox = cols["toxicity"].astype(np.float64)
    bid_qty = cols["l1_bid_qty"].astype(np.float64)
    ask_qty = cols["l1_ask_qty"].astype(np.float64)

    n = spread.size
    X = np.empty((n, len(FEATURE_NAMES)), dtype=np.float64)
    X[:, 0] = spread
    X[:, 1] = _signed(depth, side)
    X[:, 2] = _signed(ofi, side)
    X[:, 3] = _queue_ratio(bid_qty, ask_qty, side)
    X[:, 4] = vol
    X[:, 5] = churn
    X[:, 6] = _signed(np.where(np.isnan(tox), 0.0, tox), side)
    return X


def _stack(days: list[DayPanel], col: str) -> np.ndarray:
    return np.concatenate([d.cols[col] for d in days])


def _stack_features(days: list[DayPanel], side: int) -> np.ndarray:
    return np.concatenate([build_features(d.cols, side) for d in days], axis=0)


def _train_one(
    train_days: list[DayPanel],
    test_days: list[DayPanel],
    side: int,
    horizon_ms: int,
    target: str,
    cfg: TrainConfig,
) -> dict:
    side_word = "buy" if side > 0 else "sell"
    fill_col = f"filled_{side_word}_h{horizon_ms}"
    mark_col = f"markout_{side_word}_h{horizon_ms}"

    X_train_raw = _stack_features(train_days, side)
    X_test_raw = _stack_features(test_days, side)
    fill_train = _stack(train_days, fill_col).astype(np.int8)
    fill_test = _stack(test_days, fill_col).astype(np.int8)
    mark_train = _stack(train_days, mark_col).astype(np.float64)
    mark_test = _stack(test_days, mark_col).astype(np.float64)
    spread_test = _stack(test_days, "spread_pt").astype(np.float64)

    if target == "fill":
        y_train_raw = fill_train.astype(np.float64)
        y_test_raw = fill_test.astype(np.float64)
        train_keep_extra = fill_train != -1
        test_keep_extra = fill_test != -1
    elif target == "adverse":
        y_train_raw = (mark_train < -cfg.adverse_threshold_pt).astype(np.float64)
        y_test_raw = (mark_test < -cfg.adverse_threshold_pt).astype(np.float64)
        train_keep_extra = (fill_train == 1) & np.isfinite(mark_train)
        test_keep_extra = (fill_test == 1) & np.isfinite(mark_test)
    else:
        raise ValueError(f"unknown target {target}")

    train_finite_X = np.all(np.isfinite(X_train_raw), axis=1)
    test_finite_X = np.all(np.isfinite(X_test_raw), axis=1)
    train_keep = train_finite_X & train_keep_extra & np.isfinite(y_train_raw)
    test_keep = test_finite_X & test_keep_extra & np.isfinite(y_test_raw)

    X_train = X_train_raw[train_keep]
    y_train = y_train_raw[train_keep]
    X_test = X_test_raw[test_keep]
    y_test = y_test_raw[test_keep]
    spread_test_kept = spread_test[test_keep]

    n_train = int(X_train.shape[0])
    n_test = int(X_test.shape[0])
    if n_train < 500 or n_test < 100:
        return {
            "side": side_word,
            "horizon_ms": horizon_ms,
            "target": target,
            "n_train": n_train,
            "n_test": n_test,
            "skipped_reason": "insufficient_data",
        }

    model = LogisticBinary()
    train_metrics = model.fit(X_train, y_train)

    p_test = model.predict_proba(X_test)
    test_auc = compute_auc(y_test, p_test)
    test_brier = float(np.mean((p_test - y_test) ** 2))
    test_base_rate = float(y_test.mean())
    calib = calibration_by_decile(y_test, p_test)
    by_spread = stratified_metric(y_test, p_test, spread_test_kept, n_bins=5)

    return {
        "side": side_word,
        "horizon_ms": horizon_ms,
        "target": target,
        "feature_names": list(FEATURE_NAMES),
        "model": model.to_dict(),
        "train": {
            "n": n_train,
            "base_rate": float(y_train.mean()),
            **{k: v for k, v in train_metrics.items() if not isinstance(v, list)},
        },
        "test": {
            "n": n_test,
            "base_rate": test_base_rate,
            "auc": float(test_auc),
            "brier": test_brier,
            "calibration_by_decile": calib,
            "by_spread_quintile": by_spread,
        },
    }


def train_all(
    synth_dir: Path,
    out_dir: Path,
    cfg: TrainConfig | None = None,
) -> dict:
    cfg = cfg or TrainConfig()
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "models").mkdir(exist_ok=True)
    (out_dir / "eval").mkdir(exist_ok=True)

    days = load_days(synth_dir)
    if len(days) < 4:
        raise RuntimeError(f"need >=4 days for OOS split; got {len(days)}")
    n_train_days = max(2, int(len(days) * cfg.train_frac))
    train_days = days[:n_train_days]
    test_days = days[n_train_days:]
    log.info(
        "split: train=%d days [%s..%s] test=%d days [%s..%s]",
        len(train_days),
        train_days[0].date,
        train_days[-1].date,
        len(test_days),
        test_days[0].date,
        test_days[-1].date,
    )

    summary: dict = {
        "synth_dir": str(synth_dir),
        "n_days_total": len(days),
        "n_train_days": len(train_days),
        "n_test_days": len(test_days),
        "train_dates": [d.date for d in train_days],
        "test_dates": [d.date for d in test_days],
        "config": {
            "horizons_ms": list(cfg.horizons_ms),
            "adverse_threshold_pt": cfg.adverse_threshold_pt,
            "train_frac": cfg.train_frac,
        },
        "results": [],
    }

    for side in (1, -1):
        for h_ms in cfg.horizons_ms:
            for target in ("fill", "adverse"):
                tag = f"{('buy' if side > 0 else 'sell')}_h{h_ms}_{target}"
                log.info("training %s", tag)
                report = _train_one(train_days, test_days, side, h_ms, target, cfg)
                eval_path = out_dir / "eval" / f"{tag}.json"
                eval_path.write_text(json.dumps(report, indent=2))
                if "model" in report:
                    (out_dir / "models" / f"{tag}.json").write_text(
                        json.dumps(report["model"], indent=2)
                    )
                short = {
                    k: report.get(k)
                    for k in ("side", "horizon_ms", "target", "skipped_reason")
                }
                if "test" in report:
                    short["test_auc"] = report["test"]["auc"]
                    short["test_brier"] = report["test"]["brier"]
                    short["test_base_rate"] = report["test"]["base_rate"]
                    short["test_n"] = report["test"]["n"]
                    short["train_auc"] = report["train"].get("auc")
                summary["results"].append(short)

    summary_path = out_dir / "summary.json"
    summary_path.write_text(json.dumps(summary, indent=2))
    _write_report(out_dir, summary)
    return summary


def _write_report(out_dir: Path, summary: dict) -> None:
    lines = [
        "# P2 Execution Predictor — Train/Eval Report",
        "",
        f"- synth_dir: `{summary['synth_dir']}`",
        f"- days: total={summary['n_days_total']}  train={summary['n_train_days']}  "
        f"test={summary['n_test_days']}",
        f"- train range: {summary['train_dates'][0]} → {summary['train_dates'][-1]}",
        f"- test range:  {summary['test_dates'][0]} → {summary['test_dates'][-1]}",
        "",
        "## Per-target results",
        "",
        "| side | h(ms) | target  | n_test  | base_rate | train_auc | test_auc | brier  |",
        "|------|-------|---------|---------|-----------|-----------|----------|--------|",
    ]
    for r in summary["results"]:
        if "test_auc" not in r:
            lines.append(
                f"| {r['side']:<4} | {r['horizon_ms']:>5} | {r['target']:<7} | "
                f"SKIPPED ({r.get('skipped_reason')}) | | | | |"
            )
            continue
        lines.append(
            f"| {r['side']:<4} | {r['horizon_ms']:>5} | {r['target']:<7} | "
            f"{r['test_n']:>7d} | {r['test_base_rate']:>9.4f} | "
            f"{r['train_auc']:>9.4f} | {r['test_auc']:>8.4f} | "
            f"{r['test_brier']:>6.4f} |"
        )
    lines.append("")
    lines.append("Verdict heuristics:")
    lines.append("- AUC < 0.55 → predictor is uninformative for that target.")
    lines.append("- |train_auc - test_auc| > 0.05 → cohort drift; check by_spread_quintile.")
    lines.append("- Brier ≈ base_rate*(1-base_rate) → no improvement vs constant.")
    (out_dir / "REPORT.md").write_text("\n".join(lines))


def _build_argparser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="train_eval")
    p.add_argument("--synth-dir", type=Path, required=True)
    p.add_argument("--out", type=Path, required=True)
    p.add_argument(
        "--horizons-ms",
        type=str,
        default=",".join(str(h) for h in DEFAULT_HORIZONS_MS),
    )
    p.add_argument("--adverse-threshold-pt", type=float, default=DEFAULT_ADVERSE_THRESHOLD_PT)
    p.add_argument("--train-frac", type=float, default=DEFAULT_TRAIN_FRAC)
    return p


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s"
    )
    args = _build_argparser().parse_args(argv)
    horizons = tuple(int(x) for x in args.horizons_ms.split(",") if x.strip())
    cfg = TrainConfig(
        horizons_ms=horizons,
        adverse_threshold_pt=args.adverse_threshold_pt,
        train_frac=args.train_frac,
    )
    summary = train_all(args.synth_dir, args.out, cfg)
    log.info(
        "done; %d results -> %s",
        len(summary["results"]),
        (args.out / "REPORT.md").as_posix(),
    )
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
