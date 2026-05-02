"""Unit tests for alpha validation/promotion helper functions and types."""

from __future__ import annotations

from dataclasses import FrozenInstanceError
from pathlib import Path

import numpy as np
import pytest

from hft_platform.alpha._validation_helpers import (
    _dataset_metadata_candidates,
    _dataset_row_count,
    _missing_or_blank_metadata_keys,
    _path_under_any,
    _resolve_allowed_data_roots,
    _resolve_data_path,
    _validate_alpha_id,
)
from hft_platform.alpha._validation_types import (
    GateReport,
    ValidationConfig,
    ValidationResult,
)
from hft_platform.alpha.promotion import (
    PromotionChecklist,
    PromotionChecklistItem,
    PromotionConfig,
    PromotionResult,
    _resolve_scorecard_path,
    _to_float,
    build_promotion_checklist,
)

# ---------------------------------------------------------------------------
# _validate_alpha_id
# ---------------------------------------------------------------------------


class TestValidateAlphaId:
    def test_valid_simple(self) -> None:
        result = _validate_alpha_id("ofi")
        assert result is None

    def test_valid_with_underscores_and_digits(self) -> None:
        result = _validate_alpha_id("alpha_v2_rev3")
        assert result is None

    def test_valid_single_char(self) -> None:
        result = _validate_alpha_id("a")
        assert result is None

    def test_invalid_starts_with_digit(self) -> None:
        with pytest.raises(ValueError, match="Invalid alpha_id"):
            _validate_alpha_id("2fast")

    def test_invalid_uppercase(self) -> None:
        with pytest.raises(ValueError, match="Invalid alpha_id"):
            _validate_alpha_id("AlphaOfi")

    def test_invalid_path_injection(self) -> None:
        with pytest.raises(ValueError, match="Invalid alpha_id"):
            _validate_alpha_id("../etc/passwd")

    def test_invalid_empty(self) -> None:
        with pytest.raises(ValueError, match="Invalid alpha_id"):
            _validate_alpha_id("")

    def test_invalid_too_long(self) -> None:
        with pytest.raises(ValueError, match="Invalid alpha_id"):
            _validate_alpha_id("a" * 65)

    def test_invalid_non_string(self) -> None:
        with pytest.raises(ValueError, match="Invalid alpha_id"):
            _validate_alpha_id(123)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# _resolve_data_path
# ---------------------------------------------------------------------------


class TestResolveDataPath:
    def test_relative_path(self, tmp_path: Path) -> None:
        result = _resolve_data_path(tmp_path, "data/file.npz")
        assert result == str((tmp_path / "data" / "file.npz").resolve())

    def test_absolute_path(self, tmp_path: Path) -> None:
        abs_path = str(tmp_path / "abs" / "file.npz")
        result = _resolve_data_path(tmp_path, abs_path)
        assert result == str(Path(abs_path).resolve())


# ---------------------------------------------------------------------------
# _dataset_metadata_candidates
# ---------------------------------------------------------------------------


class TestDatasetMetadataCandidates:
    def test_npz_candidates(self) -> None:
        candidates = _dataset_metadata_candidates(Path("/data/tick.npz"))
        assert len(candidates) == 4
        assert Path("/data/tick.npz.meta.json") in candidates
        assert Path("/data/tick.meta.json") in candidates
        assert Path("/data/tick.npz.metadata.json") in candidates
        assert Path("/data/tick.metadata.json") in candidates

    def test_csv_candidates(self) -> None:
        candidates = _dataset_metadata_candidates(Path("file.csv"))
        assert Path("file.csv.meta.json") in candidates
        assert Path("file.meta.json") in candidates


# ---------------------------------------------------------------------------
# _dataset_row_count
# ---------------------------------------------------------------------------


class TestDatasetRowCount:
    def test_npy_1d(self, tmp_path: Path) -> None:
        arr = np.arange(42)
        p = tmp_path / "data.npy"
        np.save(p, arr)
        assert _dataset_row_count(p) == 42

    def test_npy_2d(self, tmp_path: Path) -> None:
        arr = np.zeros((10, 5))
        p = tmp_path / "data.npy"
        np.save(p, arr)
        assert _dataset_row_count(p) == 10

    def test_npz_with_data_key(self, tmp_path: Path) -> None:
        p = tmp_path / "data.npz"
        np.savez(p, data=np.ones((7, 3)))
        assert _dataset_row_count(p) == 7

    def test_npz_without_data_key(self, tmp_path: Path) -> None:
        p = tmp_path / "data.npz"
        np.savez(p, prices=np.ones((15,)))
        assert _dataset_row_count(p) == 15


# ---------------------------------------------------------------------------
# _missing_or_blank_metadata_keys
# ---------------------------------------------------------------------------


class TestMissingOrBlankMetadataKeys:
    def test_all_present(self) -> None:
        meta = {"source": "exchange", "date": "2026-01-01"}
        assert _missing_or_blank_metadata_keys(meta, ("source", "date")) == []

    def test_missing_key(self) -> None:
        assert _missing_or_blank_metadata_keys({}, ("source",)) == ["source"]

    def test_none_value(self) -> None:
        assert _missing_or_blank_metadata_keys({"source": None}, ("source",)) == ["source"]

    def test_blank_string(self) -> None:
        assert _missing_or_blank_metadata_keys({"source": "  "}, ("source",)) == ["source"]

    def test_empty_collection(self) -> None:
        assert _missing_or_blank_metadata_keys({"tags": []}, ("tags",)) == ["tags"]

    def test_non_empty_collection(self) -> None:
        assert _missing_or_blank_metadata_keys({"tags": ["a"]}, ("tags",)) == []


# ---------------------------------------------------------------------------
# _path_under_any
# ---------------------------------------------------------------------------


class TestPathUnderAny:
    def test_direct_child(self, tmp_path: Path) -> None:
        child = tmp_path / "sub" / "file.txt"
        assert _path_under_any(child, [tmp_path]) is True

    def test_exact_match(self, tmp_path: Path) -> None:
        assert _path_under_any(tmp_path, [tmp_path]) is True

    def test_outside(self, tmp_path: Path) -> None:
        other = tmp_path / "a"
        target = tmp_path / "b" / "file.txt"
        assert _path_under_any(target, [other]) is False

    def test_empty_roots(self, tmp_path: Path) -> None:
        assert _path_under_any(tmp_path / "x", []) is False


# ---------------------------------------------------------------------------
# _resolve_allowed_data_roots
# ---------------------------------------------------------------------------


class TestResolveAllowedDataRoots:
    def test_none_root(self) -> None:
        cfg = ValidationConfig(alpha_id="x", data_paths=[])
        assert _resolve_allowed_data_roots(None, cfg) == []

    def test_none_config(self, tmp_path: Path) -> None:
        assert _resolve_allowed_data_roots(tmp_path, None) == []

    def test_relative_roots(self, tmp_path: Path) -> None:
        cfg = ValidationConfig(
            alpha_id="x",
            data_paths=[],
            allowed_data_roots=("data/raw", "data/processed"),
        )
        roots = _resolve_allowed_data_roots(tmp_path, cfg)
        assert len(roots) == 2
        assert str((tmp_path / "data" / "raw").resolve()) in roots

    def test_blank_entries_skipped(self, tmp_path: Path) -> None:
        cfg = ValidationConfig(
            alpha_id="x",
            data_paths=[],
            allowed_data_roots=("data/raw", "  ", ""),
        )
        roots = _resolve_allowed_data_roots(tmp_path, cfg)
        assert len(roots) == 1


# ---------------------------------------------------------------------------
# ValidationConfig / ValidationResult defaults & frozen
# ---------------------------------------------------------------------------


class TestValidationTypes:
    def test_validation_config_defaults(self) -> None:
        cfg = ValidationConfig(alpha_id="test", data_paths=["a.npz"])
        assert cfg.is_oos_split == 0.7
        assert cfg.skip_gate_b_tests is False
        assert cfg.pytest_timeout_s == 300

    def test_validation_config_frozen(self) -> None:
        cfg = ValidationConfig(alpha_id="test", data_paths=[])
        with pytest.raises(FrozenInstanceError):
            cfg.alpha_id = "other"  # type: ignore[misc]

    def test_validation_result_to_dict(self) -> None:
        gate = GateReport(gate="A", passed=True, details={"ok": True})
        result = ValidationResult(
            alpha_id="test",
            passed=True,
            gate_a=gate,
            gate_b=gate,
            gate_c=gate,
            scorecard_path="/tmp/sc.json",
            run_id="r1",
            config_hash="abc",
            experiment_meta_path=None,
        )
        d = result.to_dict()
        assert d["alpha_id"] == "test"
        assert d["gate_a"]["passed"] is True

    def test_gate_report_frozen(self) -> None:
        gr = GateReport(gate="A", passed=True, details={})
        with pytest.raises(FrozenInstanceError):
            gr.passed = False  # type: ignore[misc]


# ---------------------------------------------------------------------------
# _to_float
# ---------------------------------------------------------------------------


class TestToFloat:
    def test_none(self) -> None:
        assert _to_float(None) is None

    def test_int(self) -> None:
        assert _to_float(42) == 42.0

    def test_str_numeric(self) -> None:
        assert _to_float("3.14") == pytest.approx(3.14)

    def test_str_non_numeric(self) -> None:
        assert _to_float("abc") is None

    def test_bool(self) -> None:
        # bool is subclass of int, float(True) == 1.0
        assert _to_float(True) == 1.0


# ---------------------------------------------------------------------------
# _resolve_scorecard_path
# ---------------------------------------------------------------------------


class TestResolveScorecardPath:
    def test_explicit_relative(self, tmp_path: Path) -> None:
        cfg = PromotionConfig(alpha_id="x", owner="t", scorecard_path="sc.json")
        result = _resolve_scorecard_path(tmp_path, cfg, tmp_path / "alphas" / "x")
        assert result == tmp_path / "sc.json"

    def test_explicit_absolute(self, tmp_path: Path) -> None:
        abs_path = str(tmp_path / "abs" / "sc.json")
        cfg = PromotionConfig(alpha_id="x", owner="t", scorecard_path=abs_path)
        result = _resolve_scorecard_path(tmp_path, cfg, tmp_path / "alphas" / "x")
        assert result == Path(abs_path)

    def test_fallback_to_alpha_dir(self, tmp_path: Path) -> None:
        cfg = PromotionConfig(alpha_id="x", owner="t", scorecard_path=None)
        alpha_dir = tmp_path / "alphas" / "x"
        result = _resolve_scorecard_path(tmp_path, cfg, alpha_dir)
        assert result == alpha_dir / "scorecard.json"


# ---------------------------------------------------------------------------
# build_promotion_checklist
# ---------------------------------------------------------------------------


class TestBuildPromotionChecklist:
    def test_basic_checklist_items(self) -> None:
        cfg = PromotionConfig(alpha_id="x", owner="t")
        gate_d = {
            "sharpe_oos": {"pass": True, "value": 1.5},
            "max_drawdown": {"pass": True, "value": -0.1},
            "turnover": {"pass": True, "value": 0.5},
            "latency_profile": {"pass": True, "detail": "ok"},
        }
        gate_e = {
            "checks": {
                "shadow_sessions": {"pass": True, "value": 10},
                "drift_alerts": {"pass": True, "value": 0},
                "execution_reject_rate": {"pass": True, "value": 0.001},
            }
        }
        checklist = build_promotion_checklist(cfg, gate_d, gate_e)
        assert isinstance(checklist, PromotionChecklist)
        assert len(checklist.items) == 7
        assert checklist.all_passed() is True

    def test_checklist_with_paper_trade_governance(self) -> None:
        cfg = PromotionConfig(alpha_id="x", owner="t", require_paper_trade_governance=True)
        gate_d = {
            "sharpe_oos": {"pass": True, "value": 1.5},
            "max_drawdown": {"pass": True, "value": -0.1},
            "turnover": {"pass": True, "value": 0.5},
            "latency_profile": {"pass": True, "detail": "ok"},
        }
        gate_e = {
            "checks": {
                "shadow_sessions": {"pass": True, "value": 10},
                "drift_alerts": {"pass": True, "value": 0},
                "execution_reject_rate": {"pass": True, "value": 0.001},
                "paper_trade_log_available": {"pass": True, "source": "log"},
                "paper_trade_calendar_days": {"pass": True, "value": 14},
                "paper_trade_trading_days": {"pass": True, "value": 10},
                "paper_trade_session_duration": {"pass": True, "value": 3600},
            }
        }
        checklist = build_promotion_checklist(cfg, gate_d, gate_e)
        # 7 base + 4 paper trade
        assert len(checklist.items) == 11

    def test_checklist_failing_gate(self) -> None:
        cfg = PromotionConfig(alpha_id="x", owner="t")
        gate_d = {
            "sharpe_oos": {"pass": False, "value": 0.3},
            "max_drawdown": {"pass": True, "value": -0.1},
            "turnover": {"pass": True, "value": 0.5},
            "latency_profile": {"pass": True, "detail": "ok"},
        }
        gate_e = {
            "checks": {
                "shadow_sessions": {"pass": True, "value": 10},
                "drift_alerts": {"pass": True, "value": 0},
                "execution_reject_rate": {"pass": True, "value": 0.001},
            }
        }
        checklist = build_promotion_checklist(cfg, gate_d, gate_e)
        assert checklist.all_passed() is False


# ---------------------------------------------------------------------------
# PromotionConfig / PromotionChecklist / PromotionResult types
# ---------------------------------------------------------------------------


class TestPromotionTypes:
    def test_promotion_config_defaults(self) -> None:
        cfg = PromotionConfig(alpha_id="test", owner="me")
        assert cfg.min_sharpe_oos == 1.0
        assert cfg.max_abs_drawdown == 0.2
        assert cfg.force is False
        assert cfg.enable_rust_readiness_gate is False

    def test_promotion_config_frozen(self) -> None:
        cfg = PromotionConfig(alpha_id="test", owner="me")
        with pytest.raises(FrozenInstanceError):
            cfg.alpha_id = "other"  # type: ignore[misc]

    def test_checklist_all_passed_true(self) -> None:
        items = [PromotionChecklistItem(label="a", passed=True, detail="ok")]
        cl = PromotionChecklist(items=items)
        assert cl.all_passed() is True

    def test_checklist_all_passed_false(self) -> None:
        items = [
            PromotionChecklistItem(label="a", passed=True, detail="ok"),
            PromotionChecklistItem(label="b", passed=False, detail="fail"),
        ]
        cl = PromotionChecklist(items=items)
        assert cl.all_passed() is False

    def test_checklist_to_dict(self) -> None:
        items = [PromotionChecklistItem(label="a", passed=True, detail="ok")]
        cl = PromotionChecklist(items=items)
        d = cl.to_dict()
        assert d["all_passed"] is True
        assert len(d["items"]) == 1
        assert d["items"][0]["label"] == "a"

    def test_promotion_result_to_dict(self) -> None:
        items = [PromotionChecklistItem(label="a", passed=True, detail="ok")]
        cl = PromotionChecklist(items=items)
        result = PromotionResult(
            alpha_id="test",
            approved=True,
            forced=False,
            gate_d_passed=True,
            gate_e_passed=True,
            gate_f_passed=True,
            canary_weight=0.05,
            integration_report_path="/tmp/ir.json",
            promotion_decision_path="/tmp/pd.json",
            promotion_config_path=None,
            reasons=[],
            checklist=cl,
        )
        d = result.to_dict()
        assert d["alpha_id"] == "test"
        assert d["checklist"]["all_passed"] is True

    def test_checklist_empty_items(self) -> None:
        cl = PromotionChecklist(items=[])
        assert cl.all_passed() is True
        assert cl.to_dict()["items"] == []
