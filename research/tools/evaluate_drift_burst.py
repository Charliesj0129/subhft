"""Drift-Burst Detector — Stage 4 evaluation script.

Generates synthetic tick data with KNOWN drift bursts injected at specific
timestamps, runs DriftBurstDetector, and evaluates detection accuracy.

Outputs: outputs/team_artifacts/alpha-research/stage4_drift_burst_eval.json
"""

from __future__ import annotations

import json
import math
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import numpy as np
import structlog

# ---------------------------------------------------------------------------
# Path setup
# ---------------------------------------------------------------------------

_REPO_ROOT = Path(__file__).resolve().parents[2]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))
    sys.path.insert(0, str(_REPO_ROOT / "src"))

from hft_platform.risk.drift_burst_detector import DriftBurstDetector  # noqa: E402

logger = structlog.get_logger("eval.drift_burst")

_OUT_PATH = _REPO_ROOT / "outputs" / "team_artifacts" / "alpha-research" / "stage4_drift_burst_eval.json"

# ---------------------------------------------------------------------------
# Synthetic data generation with injected bursts
# ---------------------------------------------------------------------------

_N_TICKS = 50_000
_N_BURSTS = 15
_BURST_DURATION_MIN = 50
_BURST_DURATION_MAX = 100
_BURST_SIGMA_MULT = 3.0  # drift magnitude in sigma units
_BASE_MID_X2 = 2_000_000_00  # 100.00 * 2 * 10000


@dataclass(slots=True)
class InjectedBurst:
    """Ground truth burst injection."""
    start_tick: int
    end_tick: int
    direction: int  # +1 or -1
    toxicity_type: str  # "informed" or "liquidity"
    magnitude_sigma: float


def _generate_tick_data(
    rng: np.random.Generator,
    n_ticks: int = _N_TICKS,
    n_bursts: int = _N_BURSTS,
) -> tuple[np.ndarray, np.ndarray, np.ndarray, list[InjectedBurst]]:
    """Generate random-walk mid_price_x2 with injected drift bursts.

    Returns:
        mid_price_x2: array of int (scaled x10000, doubled)
        spread_scaled: array of int
        imbalance: array of float
        ground_truth: list of InjectedBurst
    """
    # Base random walk (diffusion only)
    base_vol = 50  # per-tick volatility in scaled units
    innovations = rng.standard_normal(n_ticks) * base_vol
    mid_x2 = np.zeros(n_ticks, dtype=np.int64)
    mid_x2[0] = _BASE_MID_X2

    # Spread and imbalance (random with some structure)
    spread_scaled = np.maximum(100, (rng.normal(500, 100, n_ticks)).astype(np.int64))
    imbalance = np.clip(rng.normal(0.0, 0.2, n_ticks), -1.0, 1.0)

    # Inject bursts
    ground_truth: list[InjectedBurst] = []
    burst_mask = np.zeros(n_ticks, dtype=bool)

    # Place bursts with sufficient spacing
    available_starts: list[int] = []
    min_gap = 500
    pos = 500  # skip first 500 ticks for warmup
    while len(available_starts) < n_bursts and pos < n_ticks - _BURST_DURATION_MAX - 200:
        available_starts.append(pos)
        pos += min_gap + int(rng.integers(_BURST_DURATION_MAX, 2 * min_gap))

    for idx, start in enumerate(available_starts[:n_bursts]):
        duration = int(rng.integers(_BURST_DURATION_MIN, _BURST_DURATION_MAX + 1))
        end = min(start + duration, n_ticks)
        direction = 1 if rng.random() > 0.5 else -1
        magnitude = _BURST_SIGMA_MULT + float(rng.uniform(0.0, 1.5))

        # Decide toxicity type: alternate between informed and liquidity
        if idx % 3 == 0:
            toxicity_type = "liquidity"
        else:
            toxicity_type = "informed"

        ground_truth.append(InjectedBurst(
            start_tick=start,
            end_tick=end,
            direction=direction,
            toxicity_type=toxicity_type,
            magnitude_sigma=magnitude,
        ))

        # Inject drift into innovations
        drift_per_tick = direction * magnitude * base_vol / math.sqrt(duration)
        innovations[start:end] += drift_per_tick
        burst_mask[start:end] = True

        # For "informed" bursts, align imbalance with burst direction
        if toxicity_type == "informed":
            # Opposing imbalance (market maker withdrawal) + wider spread
            imbalance[start:end] = -direction * np.abs(rng.normal(0.4, 0.1, end - start))
            spread_scaled[start:end] = np.maximum(
                spread_scaled[start:end],
                (spread_scaled[start:end] * 1.5).astype(np.int64),
            )
        # For "liquidity" bursts, imbalance is random (no alignment)

    # Build cumulative price
    for i in range(1, n_ticks):
        mid_x2[i] = max(1000, mid_x2[i - 1] + int(innovations[i]))

    return mid_x2, spread_scaled, imbalance.astype(np.float64), ground_truth


# ---------------------------------------------------------------------------
# Evaluation
# ---------------------------------------------------------------------------

def _evaluate_detector(
    mid_x2: np.ndarray,
    spread_scaled: np.ndarray,
    imbalance: np.ndarray,
    ground_truth: list[InjectedBurst],
    burst_threshold: float,
    window_size: int = 100,
    cooldown_ticks: int = 50,
) -> dict[str, Any]:
    """Run detector and compute precision, recall, F1, FP rate, detection latency."""
    n = len(mid_x2)
    detector = DriftBurstDetector(
        window_size=window_size,
        burst_threshold=burst_threshold,
        cooldown_ticks=cooldown_ticks,
    )

    detections: list[dict[str, Any]] = []
    for i in range(n):
        result = detector.evaluate(
            mid_price_x2=int(mid_x2[i]),
            spread_scaled=int(spread_scaled[i]),
            imbalance=float(imbalance[i]),
            ts=i * 2_000_000,  # 2ms per tick
        )
        if result.burst_detected and result.burst_event is not None:
            detections.append({
                "tick": i,
                "direction": result.burst_event.direction,
                "magnitude": result.burst_event.magnitude,
                "toxicity_type": result.burst_event.toxicity_type,
            })

    # Match detections to ground truth
    # A detection is a true positive if it falls within [start - margin, end + margin]
    margin = 30
    matched_gt: set[int] = set()
    true_positives = 0
    false_positives = 0
    detection_latencies: list[int] = []

    for det in detections:
        tick = det["tick"]
        matched = False
        for gt_idx, gt in enumerate(ground_truth):
            if gt_idx in matched_gt:
                continue
            if gt.start_tick - margin <= tick <= gt.end_tick + margin:
                matched = True
                matched_gt.add(gt_idx)
                true_positives += 1
                latency = max(0, tick - gt.start_tick)
                detection_latencies.append(latency)
                break
        if not matched:
            false_positives += 1

    false_negatives = len(ground_truth) - len(matched_gt)

    precision = true_positives / max(1, true_positives + false_positives)
    recall = true_positives / max(1, len(ground_truth))
    f1 = 2 * precision * recall / max(1e-12, precision + recall)

    # False positive rate per 1000 ticks
    # (excluding burst regions from denominator)
    burst_ticks = sum(gt.end_tick - gt.start_tick for gt in ground_truth)
    non_burst_ticks = max(1, n - burst_ticks)
    fp_rate_per_1000 = (false_positives / non_burst_ticks) * 1000

    avg_latency = float(np.mean(detection_latencies)) if detection_latencies else float("nan")
    median_latency = float(np.median(detection_latencies)) if detection_latencies else float("nan")

    return {
        "burst_threshold": burst_threshold,
        "window_size": window_size,
        "cooldown_ticks": cooldown_ticks,
        "n_ground_truth": len(ground_truth),
        "n_detections": len(detections),
        "true_positives": true_positives,
        "false_positives": false_positives,
        "false_negatives": false_negatives,
        "precision": round(precision, 4),
        "recall": round(recall, 4),
        "f1": round(f1, 4),
        "fp_rate_per_1000_ticks": round(fp_rate_per_1000, 4),
        "avg_detection_latency_ticks": round(avg_latency, 1) if not math.isnan(avg_latency) else None,
        "median_detection_latency_ticks": round(median_latency, 1) if not math.isnan(median_latency) else None,
    }


# ---------------------------------------------------------------------------
# Toxicity classification accuracy
# ---------------------------------------------------------------------------

def _evaluate_toxicity_classification(
    mid_x2: np.ndarray,
    spread_scaled: np.ndarray,
    imbalance: np.ndarray,
    ground_truth: list[InjectedBurst],
    burst_threshold: float = 3.0,
) -> dict[str, Any]:
    """Evaluate whether detector correctly classifies informed vs liquidity bursts."""
    n = len(mid_x2)
    detector = DriftBurstDetector(
        window_size=100,
        burst_threshold=burst_threshold,
        cooldown_ticks=50,
    )

    detections: list[dict[str, Any]] = []
    for i in range(n):
        result = detector.evaluate(
            mid_price_x2=int(mid_x2[i]),
            spread_scaled=int(spread_scaled[i]),
            imbalance=float(imbalance[i]),
            ts=i * 2_000_000,
        )
        if result.burst_detected and result.burst_event is not None:
            detections.append({
                "tick": i,
                "toxicity_type": result.burst_event.toxicity_type,
            })

    margin = 30
    correct_type = 0
    total_matched = 0
    matched_gt: set[int] = set()

    for det in detections:
        tick = det["tick"]
        for gt_idx, gt in enumerate(ground_truth):
            if gt_idx in matched_gt:
                continue
            if gt.start_tick - margin <= tick <= gt.end_tick + margin:
                matched_gt.add(gt_idx)
                total_matched += 1
                if det["toxicity_type"] == gt.toxicity_type:
                    correct_type += 1
                break

    accuracy = correct_type / max(1, total_matched)
    return {
        "total_matched": total_matched,
        "correct_type_classification": correct_type,
        "toxicity_type_accuracy": round(accuracy, 4),
    }


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> int:
    logger.info("drift_burst_eval_start", n_ticks=_N_TICKS, n_bursts=_N_BURSTS)

    rng = np.random.default_rng(seed=2026)
    mid_x2, spread_scaled, imbalance, ground_truth = _generate_tick_data(rng)

    logger.info(
        "data_generated",
        n_ticks=len(mid_x2),
        n_bursts=len(ground_truth),
        burst_types=[gt.toxicity_type for gt in ground_truth],
    )

    # Threshold sweep: 2.0 to 5.0 in 0.5 increments
    thresholds = [2.0, 2.5, 3.0, 3.5, 4.0, 4.5, 5.0]
    sweep_results: list[dict[str, Any]] = []

    for thresh in thresholds:
        result = _evaluate_detector(
            mid_x2, spread_scaled, imbalance, ground_truth,
            burst_threshold=thresh,
        )
        sweep_results.append(result)
        logger.info(
            "threshold_sweep",
            threshold=thresh,
            precision=result["precision"],
            recall=result["recall"],
            f1=result["f1"],
            fp_rate=result["fp_rate_per_1000_ticks"],
            avg_latency=result["avg_detection_latency_ticks"],
        )

    # Best F1 configuration
    best_by_f1 = max(sweep_results, key=lambda r: r["f1"])

    # Toxicity type classification accuracy at best threshold
    tox_class = _evaluate_toxicity_classification(
        mid_x2, spread_scaled, imbalance, ground_truth,
        burst_threshold=best_by_f1["burst_threshold"],
    )
    logger.info(
        "toxicity_classification",
        accuracy=tox_class["toxicity_type_accuracy"],
        matched=tox_class["total_matched"],
    )

    # Assemble output
    ground_truth_summary = [
        {
            "start": gt.start_tick,
            "end": gt.end_tick,
            "direction": gt.direction,
            "type": gt.toxicity_type,
            "magnitude_sigma": round(gt.magnitude_sigma, 2),
        }
        for gt in ground_truth
    ]

    output: dict[str, Any] = {
        "stage": "4_drift_burst_eval",
        "component": "DriftBurstDetector",
        "data": {
            "n_ticks": _N_TICKS,
            "n_injected_bursts": len(ground_truth),
            "burst_sigma_multiplier": _BURST_SIGMA_MULT,
            "burst_duration_range": [_BURST_DURATION_MIN, _BURST_DURATION_MAX],
            "seed": 2026,
        },
        "ground_truth": ground_truth_summary,
        "threshold_sweep": sweep_results,
        "best_f1_config": {
            "threshold": best_by_f1["burst_threshold"],
            "f1": best_by_f1["f1"],
            "precision": best_by_f1["precision"],
            "recall": best_by_f1["recall"],
            "fp_rate_per_1000": best_by_f1["fp_rate_per_1000_ticks"],
            "avg_detection_latency_ticks": best_by_f1["avg_detection_latency_ticks"],
        },
        "toxicity_classification": tox_class,
    }

    _OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    _OUT_PATH.write_text(json.dumps(output, indent=2, default=str), encoding="utf-8")
    logger.info("results_saved", path=str(_OUT_PATH))

    # Summary
    logger.info(
        "eval_summary",
        best_threshold=best_by_f1["burst_threshold"],
        best_f1=best_by_f1["f1"],
        best_precision=best_by_f1["precision"],
        best_recall=best_by_f1["recall"],
        fp_rate=best_by_f1["fp_rate_per_1000_ticks"],
        toxicity_accuracy=tox_class["toxicity_type_accuracy"],
    )

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
