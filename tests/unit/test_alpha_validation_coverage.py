"""Coverage tests for alpha/validation.py — uncovered orchestrator and batch paths."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from hft_platform.alpha._validation_types import GateReport, ValidationConfig

# ---------------------------------------------------------------------------
# _update_manifest_status — write error path (lines 272-280)
# ---------------------------------------------------------------------------


class TestUpdateManifestStatusWriteError:
    def test_write_error_returns_false(self, tmp_path: Path):
        from hft_platform.alpha.validation import _update_manifest_status

        alpha_id = "write_err_alpha"
        impl_dir = tmp_path / "research" / "alphas" / alpha_id
        impl_dir.mkdir(parents=True)
        impl_file = impl_dir / "impl.py"
        impl_file.write_text("manifest = AlphaManifest(status=AlphaStatus.RESEARCH)\n")

        with patch.object(Path, "write_text", side_effect=OSError("disk full")):
            result = _update_manifest_status(alpha_id, "GATE_A", tmp_path)
        assert result is False

    def test_read_error_returns_false(self, tmp_path: Path):
        from hft_platform.alpha.validation import _update_manifest_status

        alpha_id = "read_err_alpha"
        impl_dir = tmp_path / "research" / "alphas" / alpha_id
        impl_dir.mkdir(parents=True)
        impl_file = impl_dir / "impl.py"
        impl_file.write_text("some content")

        with patch.object(Path, "read_text", side_effect=OSError("permission denied")):
            result = _update_manifest_status(alpha_id, "GATE_A", tmp_path)
        assert result is False

    def test_no_pattern_match_returns_false(self, tmp_path: Path):
        """File exists but does not contain the status=AlphaStatus pattern."""
        from hft_platform.alpha.validation import _update_manifest_status

        alpha_id = "no_pattern_alpha"
        impl_dir = tmp_path / "research" / "alphas" / alpha_id
        impl_dir.mkdir(parents=True)
        impl_file = impl_dir / "impl.py"
        impl_file.write_text("# no status pattern here\nx = 1\n")

        result = _update_manifest_status(alpha_id, "GATE_A", tmp_path)
        assert result is False


# ---------------------------------------------------------------------------
# batch_validate — gate_key branches (lines 317-328, 339-342)
# ---------------------------------------------------------------------------


class TestBatchValidateGateKeys:
    def test_batch_validate_gate_a_only(self):
        """gate='a' should set passed based on gate_a result only."""
        from hft_platform.alpha.validation import batch_validate

        mock_result = MagicMock()
        mock_result.passed = False
        mock_result.gate_a = MagicMock(passed=True)
        mock_result.gate_b = MagicMock(passed=False)
        mock_result.gate_c = MagicMock(passed=False)

        with patch("hft_platform.alpha.validation.run_alpha_validation", return_value=mock_result):
            result = batch_validate(
                alpha_ids=["test_alpha"],
                data_paths=[],
                gate="a",
                project_root="/tmp",
            )
        assert result["gate"] == "a"
        assert result["total"] == 1
        assert result["results"][0]["passed"] is True

    def test_batch_validate_gate_b_checks_a_and_b(self):
        """gate='b' should set passed based on gate_a AND gate_b."""
        from hft_platform.alpha.validation import batch_validate

        mock_result = MagicMock()
        mock_result.passed = False
        mock_result.gate_a = MagicMock(passed=True)
        mock_result.gate_b = MagicMock(passed=True)
        mock_result.gate_c = MagicMock(passed=False)

        with patch("hft_platform.alpha.validation.run_alpha_validation", return_value=mock_result):
            result = batch_validate(
                alpha_ids=["test_alpha"],
                data_paths=[],
                gate="b",
                project_root="/tmp",
            )
        assert result["results"][0]["passed"] is True

    def test_batch_validate_gate_b_fails_when_a_fails(self):
        """gate='b' should fail if gate_a fails."""
        from hft_platform.alpha.validation import batch_validate

        mock_result = MagicMock()
        mock_result.passed = False
        mock_result.gate_a = MagicMock(passed=False)
        mock_result.gate_b = MagicMock(passed=True)
        mock_result.gate_c = MagicMock(passed=False)

        with patch("hft_platform.alpha.validation.run_alpha_validation", return_value=mock_result):
            result = batch_validate(
                alpha_ids=["test_alpha"],
                data_paths=[],
                gate="b",
                project_root="/tmp",
            )
        assert result["results"][0]["passed"] is False

    def test_batch_validate_gate_c_uses_overall_passed(self):
        """gate='c' (default) should use overall passed."""
        from hft_platform.alpha.validation import batch_validate

        mock_result = MagicMock()
        mock_result.passed = True
        mock_result.gate_a = MagicMock(passed=True)
        mock_result.gate_b = MagicMock(passed=True)
        mock_result.gate_c = MagicMock(passed=True)

        with patch("hft_platform.alpha.validation.run_alpha_validation", return_value=mock_result):
            result = batch_validate(
                alpha_ids=["test_alpha"],
                data_paths=[],
                gate="c",
                project_root="/tmp",
            )
        assert result["results"][0]["passed"] is True

    def test_batch_validate_exception_returns_error_entry(self):
        """Exception during validation produces an error entry."""
        from hft_platform.alpha.validation import batch_validate

        with patch(
            "hft_platform.alpha.validation.run_alpha_validation",
            side_effect=RuntimeError("boom"),
        ):
            result = batch_validate(
                alpha_ids=["fail_alpha"],
                data_paths=[],
                gate="a",
                project_root="/tmp",
            )
        assert result["total"] == 1
        assert result["failed"] == 1
        assert "RuntimeError" in result["results"][0]["error"]

    def test_batch_validate_multiple_alphas_sorted(self):
        """Results should be sorted by alpha_id."""
        from hft_platform.alpha.validation import batch_validate

        mock_result = MagicMock()
        mock_result.passed = True
        mock_result.gate_a = MagicMock(passed=True)
        mock_result.gate_b = MagicMock(passed=True)
        mock_result.gate_c = MagicMock(passed=True)

        with patch("hft_platform.alpha.validation.run_alpha_validation", return_value=mock_result):
            result = batch_validate(
                alpha_ids=["beta", "alpha"],
                data_paths=[],
                gate="a",
                project_root="/tmp",
            )
        ids = [r["alpha_id"] for r in result["results"]]
        assert ids == ["alpha", "beta"]

    def test_batch_validate_workers_gt_1_uses_process_pool(self):
        """parallel > 1 should use ProcessPoolExecutor."""
        from unittest.mock import MagicMock

        from hft_platform.alpha.validation import batch_validate

        # Mock the ProcessPoolExecutor to avoid actual multiprocessing
        mock_future = MagicMock()
        mock_future.result.return_value = {
            "alpha_id": "a1",
            "passed": False,
            "error": "RuntimeError: boom",
        }
        mock_pool = MagicMock()
        mock_pool.__enter__ = MagicMock(return_value=mock_pool)
        mock_pool.__exit__ = MagicMock(return_value=False)
        mock_pool.submit.return_value = mock_future

        with patch(
            "concurrent.futures.ProcessPoolExecutor",
            return_value=mock_pool,
        ):
            with patch("concurrent.futures.as_completed", return_value=[mock_future]):
                result = batch_validate(
                    alpha_ids=["a1"],
                    data_paths=[],
                    gate="a",
                    parallel=2,
                    project_root="/tmp",
                )
        assert result["total"] == 1


# ---------------------------------------------------------------------------
# run_alpha_validation — orchestrator (lines 125-225)
# ---------------------------------------------------------------------------


class TestRunAlphaValidation:
    def test_unknown_alpha_raises_value_error(self, tmp_path: Path):
        """run_alpha_validation raises ValueError for unknown alpha_id."""
        from hft_platform.alpha.validation import run_alpha_validation

        config = ValidationConfig(
            alpha_id="totally_unknown_alpha",
            data_paths=[],
            project_root=str(tmp_path),
            experiments_dir=str(tmp_path / "exp"),
        )
        # Mock discover to return an empty registry
        with patch("hft_platform.alpha.validation._ensure_project_root_on_path"):
            mock_registry_cls = MagicMock()
            mock_registry_instance = MagicMock()
            mock_registry_instance.discover.return_value = {}
            mock_registry_cls.return_value = mock_registry_instance
            with patch.dict(
                "sys.modules", {"research.registry.alpha_registry": MagicMock(AlphaRegistry=mock_registry_cls)}
            ):
                with pytest.raises(ValueError, match="Unknown alpha_id"):
                    run_alpha_validation(config)

    def test_orchestrator_gate_c_skipped_when_gate_a_fails(self, tmp_path: Path):
        """When gate A fails, gate C is skipped with appropriate details."""
        from hft_platform.alpha.validation import run_alpha_validation

        config = ValidationConfig(
            alpha_id="test_alpha",
            data_paths=[],
            project_root=str(tmp_path),
            experiments_dir=str(tmp_path / "exp"),
        )

        failed_gate_a = GateReport(gate="Gate A", passed=False, details={"reason": "missing fields"})
        passed_gate_b = GateReport(gate="Gate B", passed=True, details={"skipped": True})

        mock_alpha = MagicMock()
        mock_alpha.manifest = MagicMock()

        mock_registry_cls = MagicMock()
        mock_registry_instance = MagicMock()
        mock_registry_instance.discover.return_value = {"test_alpha": mock_alpha}
        mock_registry_cls.return_value = mock_registry_instance

        with (
            patch("hft_platform.alpha.validation._ensure_project_root_on_path"),
            patch("hft_platform.alpha.validation._make_validation_artifact_dir", return_value=tmp_path / "val"),
            patch("hft_platform.alpha.validation._write_json"),
            patch("hft_platform.alpha.validation.run_gate_a", return_value=failed_gate_a),
            patch("hft_platform.alpha.validation.run_gate_b", return_value=passed_gate_b),
            patch("hft_platform.alpha.validation._resolve_data_path", side_effect=lambda root, p: p),
            patch("hft_platform.alpha.validation._update_manifest_status"),
            patch.dict("sys.modules", {"research.registry.alpha_registry": MagicMock(AlphaRegistry=mock_registry_cls)}),
        ):
            result = run_alpha_validation(config)

        assert result.passed is False
        assert result.gate_c.passed is False
        assert result.gate_c.details["skipped"] is True
        assert result.gate_c.details["gate_a_passed"] is False

    def test_orchestrator_all_gates_pass(self, tmp_path: Path):
        """When all gates pass, overall result is passed."""
        from hft_platform.alpha.validation import run_alpha_validation

        config = ValidationConfig(
            alpha_id="test_alpha",
            data_paths=[],
            project_root=str(tmp_path),
            experiments_dir=str(tmp_path / "exp"),
        )

        passed_a = GateReport(gate="Gate A", passed=True, details={})
        passed_b = GateReport(gate="Gate B", passed=True, details={})
        passed_c = GateReport(gate="Gate C", passed=True, details={})

        mock_alpha = MagicMock()
        mock_alpha.manifest = MagicMock()

        mock_registry_cls = MagicMock()
        mock_registry_instance = MagicMock()
        mock_registry_instance.discover.return_value = {"test_alpha": mock_alpha}
        mock_registry_cls.return_value = mock_registry_instance

        with (
            patch("hft_platform.alpha.validation._ensure_project_root_on_path"),
            patch("hft_platform.alpha.validation._make_validation_artifact_dir", return_value=tmp_path / "val"),
            patch("hft_platform.alpha.validation._write_json"),
            patch("hft_platform.alpha.validation.run_gate_a", return_value=passed_a),
            patch("hft_platform.alpha.validation.run_gate_b", return_value=passed_b),
            patch(
                "hft_platform.alpha.validation.run_gate_c",
                return_value=(passed_c, "run-1", "hash-1", str(tmp_path / "sc.json"), str(tmp_path / "meta.json")),
            ),
            patch("hft_platform.alpha.validation._resolve_data_path", side_effect=lambda root, p: p),
            patch("hft_platform.alpha.validation._update_manifest_status"),
            patch.dict("sys.modules", {"research.registry.alpha_registry": MagicMock(AlphaRegistry=mock_registry_cls)}),
        ):
            result = run_alpha_validation(config)

        assert result.passed is True
        assert result.gate_a.passed is True
        assert result.gate_b.passed is True
        assert result.gate_c.passed is True
        assert result.run_id == "run-1"

    def test_orchestrator_audit_log_failure_does_not_propagate(self, tmp_path: Path):
        """Audit log failure is swallowed silently."""
        from hft_platform.alpha.validation import run_alpha_validation

        config = ValidationConfig(
            alpha_id="test_alpha",
            data_paths=[],
            project_root=str(tmp_path),
            experiments_dir=str(tmp_path / "exp"),
        )

        passed_a = GateReport(gate="Gate A", passed=True, details={})
        passed_b = GateReport(gate="Gate B", passed=True, details={})
        passed_c = GateReport(gate="Gate C", passed=True, details={})

        mock_alpha = MagicMock()
        mock_alpha.manifest = MagicMock()

        mock_registry_cls = MagicMock()
        mock_registry_instance = MagicMock()
        mock_registry_instance.discover.return_value = {"test_alpha": mock_alpha}
        mock_registry_cls.return_value = mock_registry_instance

        def _audit_boom(*args, **kwargs):
            raise RuntimeError("audit storage down")

        audit_mod = MagicMock()
        audit_mod.log_gate_result = _audit_boom

        with (
            patch("hft_platform.alpha.validation._ensure_project_root_on_path"),
            patch("hft_platform.alpha.validation._make_validation_artifact_dir", return_value=tmp_path / "val"),
            patch("hft_platform.alpha.validation._write_json"),
            patch("hft_platform.alpha.validation.run_gate_a", return_value=passed_a),
            patch("hft_platform.alpha.validation.run_gate_b", return_value=passed_b),
            patch(
                "hft_platform.alpha.validation.run_gate_c",
                return_value=(passed_c, "run-1", "hash-1", str(tmp_path / "sc.json"), str(tmp_path / "meta.json")),
            ),
            patch("hft_platform.alpha.validation._resolve_data_path", side_effect=lambda root, p: p),
            patch("hft_platform.alpha.validation._update_manifest_status"),
            patch.dict(
                "sys.modules",
                {
                    "research.registry.alpha_registry": MagicMock(AlphaRegistry=mock_registry_cls),
                    "hft_platform.alpha.audit": audit_mod,
                },
            ),
        ):
            result = run_alpha_validation(config)

        # Should complete despite audit failure
        assert result.passed is True
