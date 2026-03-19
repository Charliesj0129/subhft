"""Tests for hft_platform.alpha.dashboard module."""

from __future__ import annotations

import json
from pathlib import Path

import yaml

from hft_platform.alpha.dashboard import build_alpha_status_report


def _write_manifest(alphas_dir: Path, alpha_id: str, **kwargs: object) -> Path:
    alpha_dir = alphas_dir / alpha_id
    alpha_dir.mkdir(parents=True, exist_ok=True)
    manifest: dict[str, object] = {
        "alpha_id": alpha_id,
        "status": "research",
        "tier": "T1",
        "complexity": "O1",
    }
    manifest.update(kwargs)
    path = alpha_dir / "manifest.yaml"
    path.write_text(yaml.safe_dump(manifest, sort_keys=False))
    return path


def _write_canary_yaml(
    promotions_dir: Path,
    alpha_id: str,
    weight: float = 0.02,
    enabled: bool = True,
) -> Path:
    promotions_dir.mkdir(parents=True, exist_ok=True)
    payload = {
        "alpha_id": alpha_id,
        "enabled": enabled,
        "weight": weight,
        "owner": "test",
    }
    path = promotions_dir / f"{alpha_id}.yaml"
    path.write_text(yaml.safe_dump(payload, sort_keys=False))
    return path


def _write_experiment_run(
    experiments_dir: Path,
    alpha_id: str,
    run_id: str = "run-1",
) -> Path:
    run_dir = experiments_dir / "runs" / run_id
    run_dir.mkdir(parents=True, exist_ok=True)
    meta = {
        "run_id": run_id,
        "alpha_id": alpha_id,
        "timestamp": "2026-03-01T00:00:00",
        "gate_status": {"gate_c": True},
        "metrics": {"sharpe_oos": 1.2},
    }
    path = run_dir / "meta.json"
    path.write_text(json.dumps(meta))
    return path


class TestBuildAlphaStatusReport:
    def test_empty_dirs(self, tmp_path: Path) -> None:
        result = build_alpha_status_report(
            alphas_dir=str(tmp_path / "alphas"),
            experiments_dir=str(tmp_path / "experiments"),
            promotions_dir=str(tmp_path / "promotions"),
        )
        assert result["total_alphas"] == 0
        assert result["alphas"] == []

    def test_manifest_only(self, tmp_path: Path) -> None:
        alphas_dir = tmp_path / "alphas"
        _write_manifest(alphas_dir, "alpha_a", status="research")
        _write_manifest(alphas_dir, "alpha_b", status="validated")

        result = build_alpha_status_report(
            alphas_dir=str(alphas_dir),
            experiments_dir=str(tmp_path / "experiments"),
            promotions_dir=str(tmp_path / "promotions"),
        )
        assert result["total_alphas"] == 2
        ids = [a["alpha_id"] for a in result["alphas"]]
        assert "alpha_a" in ids
        assert "alpha_b" in ids

    def test_with_canary(self, tmp_path: Path) -> None:
        alphas_dir = tmp_path / "alphas"
        _write_manifest(alphas_dir, "alpha_a")
        promotions_dir = tmp_path / "promotions"
        _write_canary_yaml(promotions_dir, "alpha_a", weight=0.05, enabled=True)

        result = build_alpha_status_report(
            alphas_dir=str(alphas_dir),
            experiments_dir=str(tmp_path / "experiments"),
            promotions_dir=str(promotions_dir),
        )
        assert result["active_canaries"] == 1
        alpha_entry = result["alphas"][0]
        assert alpha_entry["canary"]["weight"] == 0.05

    def test_with_experiment_run(self, tmp_path: Path) -> None:
        alphas_dir = tmp_path / "alphas"
        _write_manifest(alphas_dir, "alpha_a")
        experiments_dir = tmp_path / "experiments"
        _write_experiment_run(experiments_dir, "alpha_a")

        result = build_alpha_status_report(
            alphas_dir=str(alphas_dir),
            experiments_dir=str(experiments_dir),
            promotions_dir=str(tmp_path / "promotions"),
        )
        assert result["with_experiment_runs"] == 1
        alpha_entry = result["alphas"][0]
        assert "latest_run" in alpha_entry

    def test_merged_view(self, tmp_path: Path) -> None:
        """Alpha in manifest + experiment + canary should merge into one entry."""
        alphas_dir = tmp_path / "alphas"
        _write_manifest(alphas_dir, "alpha_a", status="canary")
        experiments_dir = tmp_path / "experiments"
        _write_experiment_run(experiments_dir, "alpha_a")
        promotions_dir = tmp_path / "promotions"
        _write_canary_yaml(promotions_dir, "alpha_a")

        result = build_alpha_status_report(
            alphas_dir=str(alphas_dir),
            experiments_dir=str(experiments_dir),
            promotions_dir=str(promotions_dir),
        )
        assert result["total_alphas"] == 1
        entry = result["alphas"][0]
        assert entry["alpha_id"] == "alpha_a"
        assert "latest_run" in entry
        assert "canary" in entry
