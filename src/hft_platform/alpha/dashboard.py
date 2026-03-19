"""Alpha pipeline status dashboard.

Scans research artifacts, experiment runs, and canary configs to build a
unified status report for all known alphas.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import yaml
from structlog import get_logger

logger = get_logger("alpha.dashboard")


def build_alpha_status_report( # noqa: C901
    *,
    alphas_dir: str = "research/alphas",
    experiments_dir: str = "research/experiments",
    promotions_dir: str = "config/strategy_promotions",
) -> dict[str, Any]:
    """Build a unified status report across all alphas.

    Scans:
    - ``research/alphas/*/manifest.yaml`` for alpha inventory
    - ``research/experiments/`` for gate results
    - ``config/strategy_promotions/`` for canary state

    Returns:
        Summary dict with per-alpha status and aggregate counts.
    """
    alphas_path = Path(alphas_dir)
    experiments_path = Path(experiments_dir)
    promotions_path = Path(promotions_dir)

    # 1. Scan manifests
    alphas: dict[str, dict[str, Any]] = {}
    if alphas_path.exists():
        for manifest_path in sorted(alphas_path.glob("*/manifest.yaml")):
            try:
                manifest = yaml.safe_load(manifest_path.read_text())
                if not isinstance(manifest, dict):
                    continue
                alpha_id = str(manifest.get("alpha_id", manifest_path.parent.name))
                alphas[alpha_id] = {
                    "alpha_id": alpha_id,
                    "status": str(manifest.get("status", "unknown")),
                    "tier": str(manifest.get("tier", "")),
                    "complexity": str(manifest.get("complexity", "")),
                    "latency_profile": manifest.get("latency_profile"),
                    "manifest_path": str(manifest_path),
                }
            except Exception as _exc:  # noqa: BLE001
                logger.debug(
                    "dashboard: skipping manifest",
                    path=str(manifest_path),
                    exc_info=True,
                )

    # 2. Scan experiment runs for latest gate status per alpha
    runs_dir = experiments_path / "runs"
    gate_status: dict[str, dict[str, Any]] = {}
    if runs_dir.exists():
        for meta_path in sorted(runs_dir.glob("*/meta.json")):
            try:
                meta = json.loads(meta_path.read_text())
                alpha_id = str(meta.get("alpha_id", ""))
                if not alpha_id:
                    continue
                # Keep latest run per alpha (sorted gives us chronological order)
                gate_status[alpha_id] = {
                    "run_id": meta.get("run_id"),
                    "timestamp": meta.get("timestamp"),
                    "gate_status": meta.get("gate_status", {}),
                    "metrics": meta.get("metrics", {}),
                }
            except Exception as _exc:  # noqa: BLE001
                continue

    # 3. Scan validation artifacts
    validations_dir = experiments_path / "validations"
    validation_status: dict[str, dict[str, Any]] = {}
    if validations_dir.exists():
        for alpha_dir in sorted(validations_dir.iterdir()):
            if not alpha_dir.is_dir():
                continue
            alpha_id = alpha_dir.name
            # Find latest validation run (sorted by timestamp dir name)
            run_dirs = sorted(alpha_dir.iterdir(), reverse=True)
            for run_dir in run_dirs:
                if not run_dir.is_dir():
                    continue
                gate_results: dict[str, bool | None] = {
                    "gate_a": None,
                    "gate_b": None,
                    "gate_c": None,
                }
                feasibility = run_dir / "feasibility_report.json"
                if feasibility.exists():
                    try:
                        fr = json.loads(feasibility.read_text())
                        gate_results["gate_a"] = fr.get("passed")
                    except Exception as _exc:  # noqa: BLE001
                        pass
                correctness = run_dir / "correctness_report.json"
                if correctness.exists():
                    try:
                        cr = json.loads(correctness.read_text())
                        gate_results["gate_b"] = cr.get("passed")
                    except Exception as _exc:  # noqa: BLE001
                        pass
                backtest = run_dir / "backtest_report.json"
                if backtest.exists():
                    try:
                        br = json.loads(backtest.read_text())
                        gate_results["gate_c"] = br.get("passed")
                    except Exception as _exc:  # noqa: BLE001
                        pass
                validation_status[alpha_id] = {
                    "run_dir": str(run_dir),
                    "gates": gate_results,
                }
                break  # only latest

    # 4. Scan canary configs
    canaries: dict[str, dict[str, Any]] = {}
    if promotions_path.exists():
        for yaml_path in sorted(promotions_path.rglob("*.yaml")):
            try:
                payload = yaml.safe_load(yaml_path.read_text())
                if not isinstance(payload, dict):
                    continue
                alpha_id = str(payload.get("alpha_id", ""))
                if not alpha_id:
                    continue
                canaries[alpha_id] = {
                    "enabled": payload.get("enabled", False),
                    "weight": payload.get("weight", 0.0),
                    "path": str(yaml_path),
                }
            except Exception as _exc:  # noqa: BLE001
                continue

    # 5. Merge into unified report
    all_ids = sorted(
        set(alphas.keys()) | set(gate_status.keys()) | set(canaries.keys()) | set(validation_status.keys())
    )
    entries: list[dict[str, Any]] = []
    for alpha_id in all_ids:
        entry: dict[str, Any] = {"alpha_id": alpha_id}
        if alpha_id in alphas:
            entry.update(alphas[alpha_id])
        if alpha_id in gate_status:
            entry["latest_run"] = gate_status[alpha_id]
        if alpha_id in validation_status:
            entry["validation"] = validation_status[alpha_id]
        if alpha_id in canaries:
            entry["canary"] = canaries[alpha_id]
        entries.append(entry)

    # Aggregate counts
    total = len(entries)
    with_canary = sum(1 for e in entries if "canary" in e and e["canary"].get("enabled"))
    with_runs = sum(1 for e in entries if "latest_run" in e)

    return {
        "total_alphas": total,
        "with_experiment_runs": with_runs,
        "active_canaries": with_canary,
        "alphas": entries,
    }
