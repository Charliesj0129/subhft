"""Unit tests for alpha Gate A (_gate_a.py) and Gate B (_gate_b.py)."""

from __future__ import annotations

import subprocess
import types
from pathlib import Path
from typing import Any
from unittest.mock import patch

import numpy as np
import pytest

from hft_platform.alpha._gate_a import _FIELD_ALIASES, _field_available, run_gate_a
from hft_platform.alpha._gate_b import run_gate_b
from hft_platform.alpha._validation_types import ValidationConfig

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_npy(tmp_path: Path, name: str, fields: list[tuple[str, str]]) -> str:
    """Create a .npy file with a structured array and return its path string."""
    dtype = np.dtype(fields)
    arr = np.zeros(4, dtype=dtype)
    path = tmp_path / name
    np.save(path, arr)
    return str(path)


def _make_npz(tmp_path: Path, name: str, fields: list[tuple[str, str]]) -> str:
    """Create a .npz file with a 'data' key structured array."""
    dtype = np.dtype(fields)
    arr = np.zeros(4, dtype=dtype)
    path = tmp_path / name
    np.savez(path, data=arr)
    return str(path)


def _manifest(**kwargs: Any) -> types.SimpleNamespace:
    """Build a minimal alpha manifest namespace."""
    defaults: dict[str, Any] = {
        "data_fields": ("bid_px", "ask_px", "trade_vol"),
        "complexity": "O(1)",
        "alpha_id": "test_alpha",
        "paper_refs": (),
        "roles_used": (),
        "skills_used": (),
    }
    defaults.update(kwargs)
    return types.SimpleNamespace(**defaults)


# ===========================================================================
# Gate A Tests
# ===========================================================================


class TestGateAValidManifest:
    """Gate A should pass when all required fields and complexity are valid."""

    def test_passes_with_exact_fields(self, tmp_path: Path) -> None:
        path = _make_npy(tmp_path, "feed.npy", [("bid_px", "i8"), ("ask_px", "i8"), ("trade_vol", "f8")])
        manifest = _manifest()
        report = run_gate_a(manifest, [path])
        assert report.passed is True
        assert report.gate == "Gate A"
        assert report.details["missing_fields"] == []

    def test_passes_with_o_n_complexity(self, tmp_path: Path) -> None:
        path = _make_npy(tmp_path, "feed.npy", [("bid_px", "i8"), ("ask_px", "i8"), ("trade_vol", "f8")])
        manifest = _manifest(complexity="O(N)")
        report = run_gate_a(manifest, [path])
        assert report.passed is True
        assert report.details["complexity_ok"] is True


class TestGateAMissingFields:
    """Gate A should fail when required manifest fields are missing from data."""

    def test_fails_when_required_fields_absent(self, tmp_path: Path) -> None:
        path = _make_npy(tmp_path, "feed.npy", [("px", "i8")])
        manifest = _manifest(data_fields=("bid_px", "ask_px", "nonexistent"))
        report = run_gate_a(manifest, [path])
        assert report.passed is False
        assert len(report.details["missing_fields"]) > 0

    def test_fails_with_no_data_paths_and_required_fields(self) -> None:
        manifest = _manifest(data_fields=("bid_px",))
        report = run_gate_a(manifest, [])
        assert report.passed is False
        assert "<no_data_paths>" in report.details["missing_fields_by_path"]

    def test_passes_with_no_data_paths_and_no_required_fields(self) -> None:
        manifest = _manifest(data_fields=())
        report = run_gate_a(manifest, [])
        assert report.passed is True


class TestGateAFieldAliases:
    """Field alias resolution via _FIELD_ALIASES."""

    def test_bid_px_alias_resolves_to_best_bid(self) -> None:
        available = {"best_bid", "best_ask"}
        assert _field_available("bid_px", available) is True
        assert _field_available("ask_px", available) is True

    def test_trade_vol_alias_resolves_to_qty(self) -> None:
        available = {"qty"}
        assert _field_available("trade_vol", available) is True

    def test_current_mid_special_case_bid_ask(self) -> None:
        available = {"best_bid", "best_ask"}
        assert _field_available("current_mid", available) is True

    def test_current_mid_special_case_bid_px_ask_px(self) -> None:
        available = {"bid_px", "ask_px"}
        assert _field_available("current_mid", available) is True

    def test_current_mid_fails_without_both_sides(self) -> None:
        available = {"best_bid"}
        assert _field_available("current_mid", available) is False

    def test_alias_dict_has_expected_keys(self) -> None:
        expected = {"bid_px", "ask_px", "bid_qty", "ask_qty", "trade_vol", "current_mid", "bids", "asks"}
        assert set(_FIELD_ALIASES.keys()) == expected

    def test_alias_gate_a_integration(self, tmp_path: Path) -> None:
        """Alias resolution works end-to-end through run_gate_a."""
        path = _make_npy(
            tmp_path,
            "feed.npy",
            [("best_bid", "i8"), ("best_ask", "i8"), ("bid_depth", "f8"), ("ask_depth", "f8"), ("qty", "f8")],
        )
        manifest = _manifest(
            data_fields=("bid_px", "ask_px", "bid_qty", "ask_qty", "trade_vol", "current_mid"),
        )
        report = run_gate_a(manifest, [path])
        assert report.passed is True
        assert report.details["missing_fields"] == []


class TestGateAComplexity:
    """Complexity validation accepts O(1), O(N) and normalised forms."""

    @pytest.mark.parametrize("complexity", ["O(1)", "O(N)", "O1", "ON", "o(1)", "o(n)", " O(1) "])
    def test_valid_complexities(self, tmp_path: Path, complexity: str) -> None:
        path = _make_npy(tmp_path, "feed.npy", [("bid_px", "i8"), ("ask_px", "i8"), ("trade_vol", "f8")])
        manifest = _manifest(complexity=complexity)
        report = run_gate_a(manifest, [path])
        assert report.details["complexity_ok"] is True

    @pytest.mark.parametrize("complexity", ["O(N^2)", "O(log N)", "linear", ""])
    def test_invalid_complexities_fail(self, tmp_path: Path, complexity: str) -> None:
        path = _make_npy(tmp_path, "feed.npy", [("bid_px", "i8"), ("ask_px", "i8"), ("trade_vol", "f8")])
        manifest = _manifest(complexity=complexity)
        report = run_gate_a(manifest, [path])
        assert report.passed is False
        assert report.details["complexity_ok"] is False


class TestGateAAOSFormat:
    """AOS format validation for hftbacktest V2."""

    def test_aos_validation_triggered_by_config(self, tmp_path: Path) -> None:
        path = _make_npy(tmp_path, "feed.npy", [("bid_px", "i8"), ("ask_px", "i8"), ("trade_vol", "f8")])
        config = ValidationConfig(
            alpha_id="test",
            data_paths=[path],
            enforce_data_governance=True,
            backtest_engine="hftbacktest_v2",
        )
        manifest = _manifest()
        report = run_gate_a(manifest, [path], config=config)
        # Should have invalid_data_formats since .npy is not .npz
        formats = report.details["data_governance"]["invalid_data_formats"]
        assert path in formats


class TestGateADataGovernance:
    """Data governance: disallowed paths, missing metadata."""

    def test_invalid_data_root_fails(self, tmp_path: Path) -> None:
        data_dir = tmp_path / "rogue_data"
        data_dir.mkdir()
        path = _make_npy(data_dir, "feed.npy", [("bid_px", "i8"), ("ask_px", "i8"), ("trade_vol", "f8")])
        config = ValidationConfig(
            alpha_id="test",
            data_paths=[path],
            enforce_data_governance=True,
            allowed_data_roots=("research/data/raw",),
        )
        manifest = _manifest()
        report = run_gate_a(manifest, [path], config=config, root=tmp_path)
        assert report.details["data_governance"]["passed"] is False
        assert len(report.details["data_governance"]["invalid_data_roots"]) > 0

    def test_governance_not_enforced_by_default(self, tmp_path: Path) -> None:
        path = _make_npy(tmp_path, "feed.npy", [("bid_px", "i8"), ("ask_px", "i8"), ("trade_vol", "f8")])
        manifest = _manifest()
        report = run_gate_a(manifest, [path])
        assert report.details["data_governance"]["passed"] is True
        assert report.details["data_governance"]["enforced"] is False


class TestGateAEmptyNumpy:
    """Gate A handles numpy arrays with zero rows or no structured fields."""

    def test_empty_structured_array(self, tmp_path: Path) -> None:
        dtype = np.dtype([("bid_px", "i8"), ("ask_px", "i8"), ("trade_vol", "f8")])
        arr = np.zeros(0, dtype=dtype)
        path = tmp_path / "empty.npy"
        np.save(path, arr)
        manifest = _manifest()
        report = run_gate_a(manifest, [str(path)])
        assert report.passed is True

    def test_unstructured_array_missing_fields(self, tmp_path: Path) -> None:
        arr = np.zeros((4, 3))
        path = tmp_path / "flat.npy"
        np.save(path, arr)
        manifest = _manifest(data_fields=("bid_px",))
        report = run_gate_a(manifest, [str(path)])
        assert report.passed is False
        assert "bid_px" in report.details["missing_fields"]


class TestGateASkillsGovernance:
    """Skills/roles governance warnings are advisory (non-blocking)."""

    def test_empty_skills_and_roles_generate_warnings(self, tmp_path: Path) -> None:
        path = _make_npy(tmp_path, "feed.npy", [("bid_px", "i8"), ("ask_px", "i8"), ("trade_vol", "f8")])
        manifest = _manifest(skills_used=(), roles_used=())
        report = run_gate_a(manifest, [path])
        warnings = report.details["skills_governance"]["warnings"]
        assert len(warnings) >= 2
        assert report.passed is True  # non-blocking

    def test_populated_skills_and_roles_no_empty_warning(self, tmp_path: Path) -> None:
        path = _make_npy(tmp_path, "feed.npy", [("bid_px", "i8"), ("ask_px", "i8"), ("trade_vol", "f8")])
        manifest = _manifest(skills_used=("hft-backtester",), roles_used=("planner",))
        report = run_gate_a(manifest, [path])
        warnings = report.details["skills_governance"]["warnings"]
        empty_warnings = [w for w in warnings if "empty" in w]
        assert len(empty_warnings) == 0


class TestGateAPrecisionWarnings:
    """Precision warnings for raw price fields."""

    def test_price_field_triggers_warning(self, tmp_path: Path) -> None:
        path = _make_npy(tmp_path, "feed.npy", [("raw_price", "i8")])
        manifest = _manifest(data_fields=("raw_price",))
        report = run_gate_a(manifest, [str(path)])
        assert len(report.details["precision_warnings"]) > 0

    def test_price_diff_field_no_warning(self, tmp_path: Path) -> None:
        path = _make_npy(tmp_path, "feed.npy", [("price_diff", "i8")])
        manifest = _manifest(data_fields=("price_diff",))
        report = run_gate_a(manifest, [str(path)])
        assert len(report.details["precision_warnings"]) == 0


# ===========================================================================
# Gate B Tests
# ===========================================================================


class TestGateBSkipTests:
    """Gate B skip_tests=True bypasses subprocess execution."""

    def test_skip_tests_returns_passed(self, tmp_path: Path) -> None:
        report = run_gate_b("test_alpha", tmp_path, skip_tests=True)
        assert report.passed is True
        assert report.gate == "Gate B"
        assert report.details["skipped"] is True


class TestGateBSubprocessSuccess:
    """Gate B passes when subprocess returns 0."""

    def test_subprocess_success(self, tmp_path: Path) -> None:
        mock_result = subprocess.CompletedProcess(
            args=["pytest"],
            returncode=0,
            stdout="2 passed\n",
            stderr="",
        )
        with patch("hft_platform.alpha._gate_b.subprocess.run", return_value=mock_result):
            report = run_gate_b("test_alpha", tmp_path)
        assert report.passed is True
        assert report.details["returncode"] == 0
        assert "2 passed" in report.details["stdout_tail"]


class TestGateBSubprocessFailure:
    """Gate B fails when subprocess returns non-zero."""

    def test_subprocess_failure(self, tmp_path: Path) -> None:
        mock_result = subprocess.CompletedProcess(
            args=["pytest"],
            returncode=1,
            stdout="1 failed\n",
            stderr="ERRORS\n",
        )
        with patch("hft_platform.alpha._gate_b.subprocess.run", return_value=mock_result):
            report = run_gate_b("test_alpha", tmp_path)
        assert report.passed is False
        assert report.details["returncode"] == 1
        assert "1 failed" in report.details["stdout_tail"]


class TestGateBTimeout:
    """Gate B handles subprocess timeout gracefully."""

    def test_timeout_returns_failed(self, tmp_path: Path) -> None:
        with patch(
            "hft_platform.alpha._gate_b.subprocess.run",
            side_effect=subprocess.TimeoutExpired(cmd="pytest", timeout=10, output="partial output"),
        ):
            report = run_gate_b("test_alpha", tmp_path, timeout_s=10)
        assert report.passed is False
        assert "timeout" in report.details["error"]


class TestGateBAlphaIdValidation:
    """Gate B validates alpha_id format."""

    def test_invalid_alpha_id_raises(self, tmp_path: Path) -> None:
        with pytest.raises(ValueError, match="Invalid alpha_id"):
            run_gate_b("INVALID-ID!", tmp_path)

    def test_empty_alpha_id_raises(self, tmp_path: Path) -> None:
        with pytest.raises(ValueError, match="Invalid alpha_id"):
            run_gate_b("", tmp_path)
