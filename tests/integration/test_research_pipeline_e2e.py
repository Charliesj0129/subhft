"""E2E integration test for the research pipeline (Gate A through Gate E).

Exercises the full alpha governance pipeline with synthetic data and mock
alphas to verify that all gates integrate correctly end-to-end.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from unittest.mock import patch

import numpy as np
import pytest

from hft_platform.alpha.promotion import PromotionConfig, promote_alpha
from hft_platform.alpha.validation import GateReport, run_gate_a, run_gate_b
from research.registry.schemas import AlphaManifest, AlphaStatus

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_DATA_FIELDS = ("best_bid", "best_ask", "bid_qty", "ask_qty", "volume")

_DATA_DTYPE = np.dtype(
    [
        ("best_bid", np.int64),
        ("best_ask", np.int64),
        ("bid_qty", np.int64),
        ("ask_qty", np.int64),
        ("volume", np.int64),
    ]
)


def _make_synthetic_data(path: Path, n: int = 200) -> Path:
    """Write a minimal structured .npy file with required LOB fields."""
    rng = np.random.default_rng(42)
    arr = np.zeros(n, dtype=_DATA_DTYPE)
    arr["best_bid"] = 1000000 + rng.integers(-100, 100, size=n)
    arr["best_ask"] = arr["best_bid"] + rng.integers(10, 50, size=n)
    arr["bid_qty"] = rng.integers(1, 100, size=n)
    arr["ask_qty"] = rng.integers(1, 100, size=n)
    arr["volume"] = rng.integers(1, 500, size=n)
    npy_path = path / "test_data.npy"
    np.save(str(npy_path), arr)
    # sidecar metadata
    meta = {
        "source": "synthetic_e2e_test",
        "generator_script": "test_research_pipeline_e2e.py",
        "rng_seed": 42,
        "n_rows": n,
        "fields": list(_DATA_FIELDS),
    }
    meta_path = Path(str(npy_path) + ".meta.json")
    meta_path.write_text(json.dumps(meta, indent=2))
    return npy_path


@dataclass(frozen=True)
class _MockAlpha:
    """Minimal AlphaProtocol implementation for testing."""

    manifest: AlphaManifest
    _signal: float = 0.0

    def update(self, *args: Any, **kwargs: Any) -> float:
        return self._signal

    def reset(self) -> None:
        pass

    def get_signal(self) -> float:
        return self._signal


def _build_manifest(alpha_id: str = "e2e_test_alpha") -> AlphaManifest:
    return AlphaManifest(
        alpha_id=alpha_id,
        hypothesis="E2E integration test hypothesis",
        formula="signal = (bid_qty - ask_qty) / (bid_qty + ask_qty)",
        paper_refs=("ref_999",),
        data_fields=_DATA_FIELDS,
        complexity="O(1)",
        status=AlphaStatus.DRAFT,
        roles_used=("planner",),
        skills_used=("iterative-retrieval",),
    )


def _write_scorecard(path: Path, overrides: dict[str, Any] | None = None) -> Path:
    """Write a scorecard JSON that passes Gate D by default."""
    scorecard: dict[str, Any] = {
        "sharpe_is": 3.0,
        "sharpe_oos": 2.0,
        "ic_mean": 0.4,
        "ic_std": 0.1,
        "turnover": 0.5,
        "max_drawdown": -0.1,
        "correlation_pool_max": 0.3,
        "latency_profile": "sim_p95_v2026-02-26",
    }
    if overrides:
        scorecard.update(overrides)
    sc_path = path / "scorecard.json"
    sc_path.write_text(json.dumps(scorecard, indent=2))
    return sc_path


def _write_paper_trade_summary(path: Path) -> Path:
    """Write a paper-trade summary that passes Gate E governance."""
    summary: dict[str, Any] = {
        "session_count": 10,
        "calendar_span_days": 14,
        "distinct_trading_days": 10,
        "min_session_duration_seconds": 7200,
        "invalid_session_duration_count": 0,
        "drift_alerts_total": 0,
        "execution_reject_rate_mean": 0.002,
        "execution_reject_rate_p95": 0.005,
        "regimes_covered": ["trending", "mean_reverting"],
    }
    summary_path = path / "paper_trade_summary.json"
    summary_path.write_text(json.dumps(summary, indent=2))
    return summary_path


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestGateA:
    """Gate A: manifest feasibility + data field availability."""

    def test_gate_a_passes_with_valid_manifest_and_data(self, tmp_path: Path) -> None:
        npy_path = _make_synthetic_data(tmp_path)
        manifest = _build_manifest()
        report = run_gate_a(manifest, [str(npy_path)])
        assert isinstance(report, GateReport)
        assert report.gate == "Gate A"
        assert report.passed is True
        assert report.details["complexity_ok"] is True
        assert report.details["missing_fields"] == []

    def test_gate_a_fails_on_missing_fields(self, tmp_path: Path) -> None:
        manifest = AlphaManifest(
            alpha_id="missing_field_alpha",
            hypothesis="test",
            formula="test",
            paper_refs=(),
            data_fields=("best_bid", "best_ask", "nonexistent_field"),
            complexity="O(1)",
        )
        npy_path = _make_synthetic_data(tmp_path)
        report = run_gate_a(manifest, [str(npy_path)])
        assert report.passed is False
        assert "nonexistent_field" in report.details["missing_fields"]


class TestGateB:
    """Gate B: per-alpha pytest execution (mocked subprocess)."""

    def test_gate_b_passes_with_mocked_pytest(self, tmp_path: Path) -> None:
        with patch("subprocess.run") as mock_run:
            mock_run.return_value.returncode = 0
            mock_run.return_value.stdout = "1 passed"
            mock_run.return_value.stderr = ""
            report = run_gate_b("e2e_test_alpha", tmp_path)
        assert isinstance(report, GateReport)
        assert report.gate == "Gate B"
        assert report.passed is True

    def test_gate_b_fails_with_nonzero_returncode(self, tmp_path: Path) -> None:
        with patch("subprocess.run") as mock_run:
            mock_run.return_value.returncode = 1
            mock_run.return_value.stdout = "FAILED"
            mock_run.return_value.stderr = ""
            report = run_gate_b("e2e_test_alpha", tmp_path)
        assert report.passed is False

    def test_gate_b_skip(self, tmp_path: Path) -> None:
        report = run_gate_b("e2e_test_alpha", tmp_path, skip_tests=True)
        assert report.passed is True
        assert report.details.get("skipped") is True


class TestGateC:
    """Gate C: backtest via hftbacktest (skipped if unavailable)."""

    def test_gate_c_requires_hftbacktest(self) -> None:
        pytest.importorskip("hftbacktest")
        # If hftbacktest is available, we just verify the import succeeds.
        # Full Gate C requires a complete backtest setup which is out of scope
        # for this integration test — covered by test_backtest_flow.py.


class TestGateD:
    """Gate D: quantitative promotion thresholds."""

    def test_gate_d_passes_with_good_scorecard(self, tmp_path: Path) -> None:
        alpha_id = "e2e_test_alpha"
        alpha_dir = tmp_path / "research" / "alphas" / alpha_id
        alpha_dir.mkdir(parents=True)
        sc_path = _write_scorecard(alpha_dir)

        config = PromotionConfig(
            alpha_id=alpha_id,
            owner="test",
            project_root=str(tmp_path),
            scorecard_path=str(sc_path),
            shadow_sessions=10,
            drift_alerts=0,
            execution_reject_rate=0.005,
            require_paper_trade_governance=False,
            write_promotion_config=False,
        )
        result = promote_alpha(config)
        assert result.gate_d_passed is True
        assert result.approved is True

    def test_gate_d_fails_with_low_sharpe(self, tmp_path: Path) -> None:
        alpha_id = "e2e_low_sharpe"
        alpha_dir = tmp_path / "research" / "alphas" / alpha_id
        alpha_dir.mkdir(parents=True)
        sc_path = _write_scorecard(alpha_dir, overrides={"sharpe_oos": 0.3})

        config = PromotionConfig(
            alpha_id=alpha_id,
            owner="test",
            project_root=str(tmp_path),
            scorecard_path=str(sc_path),
            shadow_sessions=10,
            drift_alerts=0,
            execution_reject_rate=0.005,
            require_paper_trade_governance=False,
            write_promotion_config=False,
        )
        result = promote_alpha(config)
        assert result.gate_d_passed is False
        assert result.approved is False


class TestGateE:
    """Gate E: paper-trade governance."""

    def test_gate_e_passes_with_paper_trade_summary(self, tmp_path: Path) -> None:
        alpha_id = "e2e_paper_trade"
        alpha_dir = tmp_path / "research" / "alphas" / alpha_id
        alpha_dir.mkdir(parents=True)
        sc_path = _write_scorecard(alpha_dir)
        summary_path = _write_paper_trade_summary(tmp_path)

        config = PromotionConfig(
            alpha_id=alpha_id,
            owner="test",
            project_root=str(tmp_path),
            scorecard_path=str(sc_path),
            require_paper_trade_governance=True,
            paper_trade_summary_path=str(summary_path),
            write_promotion_config=False,
        )
        result = promote_alpha(config)
        assert result.gate_e_passed is True
        assert result.approved is True

    def test_gate_e_fails_without_summary(self, tmp_path: Path) -> None:
        alpha_id = "e2e_no_paper"
        alpha_dir = tmp_path / "research" / "alphas" / alpha_id
        alpha_dir.mkdir(parents=True)
        sc_path = _write_scorecard(alpha_dir)

        config = PromotionConfig(
            alpha_id=alpha_id,
            owner="test",
            project_root=str(tmp_path),
            scorecard_path=str(sc_path),
            require_paper_trade_governance=True,
            shadow_sessions=0,
            write_promotion_config=False,
        )
        result = promote_alpha(config)
        assert result.gate_e_passed is False


class TestFullPipeline:
    """End-to-end: Gate A + Gate B (mocked) + Gate D + Gate E."""

    def test_full_pipeline_approval(self, tmp_path: Path) -> None:
        alpha_id = "e2e_full_pipeline"
        npy_path = _make_synthetic_data(tmp_path)
        manifest = _build_manifest(alpha_id)

        # Gate A
        gate_a = run_gate_a(manifest, [str(npy_path)])
        assert gate_a.passed is True, f"Gate A failed: {gate_a.details}"

        # Gate B (mocked)
        with patch("subprocess.run") as mock_run:
            mock_run.return_value.returncode = 0
            mock_run.return_value.stdout = "24 passed"
            mock_run.return_value.stderr = ""
            gate_b = run_gate_b(alpha_id, tmp_path)
        assert gate_b.passed is True, f"Gate B failed: {gate_b.details}"

        # Gate D + E via promote_alpha
        alpha_dir = tmp_path / "research" / "alphas" / alpha_id
        alpha_dir.mkdir(parents=True)
        sc_path = _write_scorecard(alpha_dir)
        summary_path = _write_paper_trade_summary(tmp_path)

        config = PromotionConfig(
            alpha_id=alpha_id,
            owner="test",
            project_root=str(tmp_path),
            scorecard_path=str(sc_path),
            require_paper_trade_governance=True,
            paper_trade_summary_path=str(summary_path),
            write_promotion_config=False,
        )
        result = promote_alpha(config)
        assert result.gate_d_passed is True, f"Gate D failed: {result.reasons}"
        assert result.gate_e_passed is True, f"Gate E failed: {result.reasons}"
        assert result.approved is True
        assert result.canary_weight > 0.0
        assert result.checklist is not None
        assert result.checklist.all_passed() is True
