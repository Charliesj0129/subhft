"""Screen-only Gate A-C adapter for promoted SMMA-family DRAFTs.

The platform's generic Gate C streams LOB statistics, not OHLC bars. Routing a
bar-based SMMA alpha through that adapter would silently produce a zero signal.
This module keeps the public ``hft alpha screen`` entry point while evaluating
the family against its governed bar/BBO dataset and frozen execution contract.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Mapping

import numpy as np
import yaml

from hft_platform.contracts.alpha import AlphaManifest, AlphaStatus
from research.combinatorial.expression_lang import compile_expression
from research.combinatorial.smma import (
    build_smma_family_features,
    evaluate_smma_expression,
    smma_lengths_from_expression,
    validate_stationary_signal,
)
from research.combinatorial.smma_alpha_adapter import SMMACompiledAlpha
from research.combinatorial.smma_dataset import ROOTS, BarDataset, load_governed_dataset
from research.combinatorial.smma_runner import (
    SUPPORTED_PRIMARY_TIMEFRAMES_MINUTES,
    Candidate,
    CandidateResult,
    _candidate_id,
    _evaluate_candidate,
    evaluate_robustness_slices,
)
from research.combinatorial.smma_validation import (
    build_split_plan,
    metrics_to_dict,
    monotonic_horizon_pollution,
)

_ALPHA_ID_RE = re.compile(r"^[a-z][a-z0-9_]*$")


class SMMAScreenError(RuntimeError):
    """Raised when a promoted SMMA package or its evidence fails closed."""


def _payload_hash(payload: Mapping[str, Any]) -> str:
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _atomic_json(path: Path, payload: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        mode="w",
        dir=path.parent,
        suffix=".json",
        delete=False,
        encoding="utf-8",
    ) as handle:
        json.dump(payload, handle, indent=2, sort_keys=True, default=str)
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())
        temporary = Path(handle.name)
    temporary.replace(path)


def is_smma_screen_package(alpha_id: str, *, project_root: str | Path = ".") -> bool:
    if _ALPHA_ID_RE.fullmatch(str(alpha_id)) is None:
        return False
    manifest_path = Path(project_root) / "research" / "alphas" / str(alpha_id) / "manifest.yaml"
    if not manifest_path.is_file():
        return False
    payload = yaml.safe_load(manifest_path.read_text(encoding="utf-8")) or {}
    metadata = payload.get("experiment_metadata")
    return bool(
        isinstance(metadata, Mapping)
        and metadata.get("family") == "smma"
        and metadata.get("screen_only_required") is True
    )


def _load_package(alpha_id: str, project_root: Path) -> tuple[dict[str, Any], AlphaManifest, Candidate]:
    if _ALPHA_ID_RE.fullmatch(alpha_id) is None:
        raise SMMAScreenError(f"invalid alpha id: {alpha_id}")
    manifest_path = project_root / "research" / "alphas" / alpha_id / "manifest.yaml"
    if not manifest_path.is_file():
        raise SMMAScreenError(f"SMMA manifest not found: {manifest_path}")
    payload = yaml.safe_load(manifest_path.read_text(encoding="utf-8")) or {}
    metadata = payload.get("experiment_metadata")
    if not isinstance(metadata, Mapping):
        raise SMMAScreenError("SMMA experiment_metadata is missing")
    if (
        metadata.get("family") != "smma"
        or metadata.get("screen_only_required") is not True
        or metadata.get("promotion_eligible") is not False
    ):
        raise SMMAScreenError("SMMA package is not marked screen-only")
    manifest = AlphaManifest.from_dict(payload)
    if manifest.status is not AlphaStatus.DRAFT:
        raise SMMAScreenError("SMMA screen package must remain DRAFT")
    candidate_raw = metadata.get("candidate_spec")
    if not isinstance(candidate_raw, Mapping):
        raise SMMAScreenError("SMMA candidate_spec is missing")
    candidate = Candidate(**dict(candidate_raw))
    if (
        candidate.root not in ROOTS
        or candidate.timeframe_min not in SUPPORTED_PRIMARY_TIMEFRAMES_MINUTES
        or candidate.horizon not in {"1h", "4h", "session"}
        or candidate.direction not in {-1, 1}
        or candidate.threshold not in {0.0, 0.5, 1.0, 1.5}
    ):
        raise SMMAScreenError("SMMA candidate specification is outside the frozen family")
    expected_id = _candidate_id(
        {
            "family": "smma",
            "root": candidate.root,
            "timeframe_min": candidate.timeframe_min,
            "expression": candidate.expression,
            "horizon": candidate.horizon,
            "direction": candidate.direction,
            "threshold": candidate.threshold,
        }
    )
    if candidate.candidate_id != expected_id:
        raise SMMAScreenError("SMMA candidate identity mismatch")
    if manifest.formula != candidate.expression:
        raise SMMAScreenError("SMMA manifest formula does not match candidate")
    compiled = compile_expression(candidate.expression, max_depth=3)
    if set(manifest.data_fields) != set(compiled.variables):
        raise SMMAScreenError("SMMA manifest data_fields do not match expression")
    expected_instrument = "TXFD6" if candidate.root == "TXF" else "TMFD6"
    if manifest.instrument != expected_instrument or tuple(manifest.cost_profile_refs) != (expected_instrument,):
        raise SMMAScreenError("SMMA manifest cost profile does not match candidate root")
    return payload, manifest, candidate


def _family_context(
    dataset: BarDataset,
    candidate: Candidate,
) -> tuple[BarDataset, dict[str, np.ndarray], np.ndarray]:
    bars = dataset.group(candidate.root, candidate.timeframe_min)
    features = build_smma_family_features(
        open_=bars.open,
        high=bars.high,
        low=bars.low,
        close=bars.close,
        reset_mask=bars.reset,
        lengths=smma_lengths_from_expression(candidate.expression),
    )
    compiled = compile_expression(candidate.expression, max_depth=3)
    missing = sorted(set(compiled.variables) - set(features))
    if missing:
        raise SMMAScreenError(f"SMMA expression uses unknown features: {missing}")
    signal = evaluate_smma_expression(candidate.expression, features, bars.reset)
    valid, reason = validate_stationary_signal(signal)
    if not valid:
        raise SMMAScreenError(f"SMMA expression is unusable: {reason}")
    return bars, features, signal


def _batch_stream_parity(
    manifest: AlphaManifest,
    candidate: Candidate,
    bars: BarDataset,
    batch_signal: np.ndarray,
) -> tuple[bool, float]:
    alpha = SMMACompiledAlpha(candidate.expression, manifest)
    streamed: np.ndarray = np.empty(len(bars), dtype=np.float64)
    for index in range(len(bars)):
        streamed[index] = alpha.update(
            open=float(bars.open[index]),
            high=float(bars.high[index]),
            low=float(bars.low[index]),
            close=float(bars.close[index]),
            reset=bool(bars.reset[index]),
        )
    difference = float(np.max(np.abs(streamed - batch_signal))) if streamed.size else 0.0
    return bool(np.allclose(streamed, batch_signal, rtol=1e-12, atol=1e-12)), difference


def run_smma_screen(
    *,
    alpha_id: str,
    data_path: str | Path,
    experiments_dir: str | Path,
    project_root: str | Path = ".",
    skip_gate_b_tests: bool = False,
) -> dict[str, Any]:
    """Produce immutable screen-only Gate A-C evidence for one SMMA DRAFT."""
    root = Path(project_root).resolve()
    manifest_payload, manifest, candidate = _load_package(alpha_id, root)
    dataset = load_governed_dataset(data_path)
    bars, _features, signal = _family_context(dataset, candidate)

    gate_a = {
        "gate": "Gate A",
        "passed": True,
        "details": {
            "family": "smma",
            "status": manifest.status.value,
            "data_fields": list(manifest.data_fields),
            "governed_dataset": str(Path(data_path).resolve()),
            "screen_only": True,
        },
    }
    parity_passed, max_abs_difference = _batch_stream_parity(manifest, candidate, bars, signal)
    gate_b = {
        "gate": "Gate B",
        "passed": parity_passed,
        "details": {
            "batch_stream_parity": parity_passed,
            "max_abs_difference": max_abs_difference,
            "external_tests_skipped": bool(skip_gate_b_tests),
        },
    }

    split_plan = build_split_plan(bars.trading_day)
    labels = np.where(split_plan.labels == "final_holdout", "excluded", "screen")
    horizon_results: dict[str, CandidateResult] = {}
    for horizon in ("1h", "4h", "session"):
        horizon_candidate = Candidate(
            candidate_id=candidate.candidate_id,
            root=candidate.root,
            timeframe_min=candidate.timeframe_min,
            expression=candidate.expression,
            horizon=horizon,
            direction=candidate.direction,
            threshold=candidate.threshold,
            seed=candidate.seed,
            complexity=candidate.complexity,
        )
        horizon_results[horizon] = _evaluate_candidate(
            horizon_candidate,
            dataset=dataset,
            bars=bars,
            signal=signal,
            split_name="screen",
            split_labels=labels,
        )
    primary = horizon_results[candidate.horizon]
    trend_pollution = monotonic_horizon_pollution({name: result.kill for name, result in horizon_results.items()})
    gate_c_passed = bool(primary.kill.passed and not trend_pollution)
    gate_c = {
        "gate": "Gate C",
        "passed": gate_c_passed,
        "details": {
            "primary_horizon": candidate.horizon,
            "primary_metrics": metrics_to_dict(primary.kill),
            "horizon_metrics": {name: metrics_to_dict(result.kill) for name, result in horizon_results.items()},
            "trend_pollution": trend_pollution,
            "cost_profile": manifest.instrument,
            "execution": "next-bar executable bid/ask; one position; no pyramiding",
            "evaluation_scope": "pre_final_screen",
            "final_holdout_excluded": True,
        },
    }
    robustness = evaluate_robustness_slices(
        dataset,
        CandidateResult(
            candidate=candidate,
            kill=primary.kill,
            stage="screen",
            status="passed" if gate_c_passed else "killed",
        ),
        evaluation_scope="pre_final_screen",
    )
    run_id = f"smma-screen-{candidate.candidate_id[:16]}-{datetime.now(UTC).strftime('%Y%m%dT%H%M%SZ')}"
    run_dir = Path(experiments_dir).resolve() / "runs" / run_id
    scorecard_path = run_dir / "scorecard.json"
    scorecard = {
        "schema": "smma_screen_scorecard.v1",
        "alpha_id": alpha_id,
        "run_id": run_id,
        "screen_only": True,
        "screen_profile": "smma_governed_bars",
        "promotion_eligible": False,
        "passed": bool(gate_a["passed"] and gate_b["passed"] and gate_c["passed"]),
        "candidate": asdict(candidate),
        "gate_a": gate_a,
        "gate_b": gate_b,
        "gate_c": gate_c,
        "robustness_slices": robustness,
        "manifest_screen_metadata": dict(manifest_payload["experiment_metadata"]),
        "recorded_at": datetime.now(UTC).isoformat(),
    }
    scorecard["scorecard_hash"] = _payload_hash(scorecard)
    _atomic_json(scorecard_path, scorecard)
    _atomic_json(run_dir / "feasibility_report.json", gate_a)
    _atomic_json(run_dir / "correctness_report.json", gate_b)
    _atomic_json(run_dir / "backtest_report.json", gate_c)
    return {
        "alpha_id": alpha_id,
        "passed": scorecard["passed"],
        "screen_only": True,
        "promotion_eligible": False,
        "gate_a": gate_a,
        "gate_b": gate_b,
        "gate_c": gate_c,
        "scorecard_path": str(scorecard_path),
        "scorecard_hash": scorecard["scorecard_hash"],
        "run_id": run_id,
    }
