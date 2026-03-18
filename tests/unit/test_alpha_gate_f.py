"""Unit tests for alpha Gate F — Rust readiness gate."""

from __future__ import annotations

import subprocess
from pathlib import Path
from unittest.mock import MagicMock, patch

from hft_platform.alpha._gate_f import _evaluate_gate_f, _load_rust_module_name
from hft_platform.alpha._promotion_types import PromotionConfig


def _cfg(**overrides: object) -> PromotionConfig:
    defaults = {
        "alpha_id": "test_alpha",
        "owner": "tester",
        "enable_rust_readiness_gate": True,
        "rust_module_name": "rust_test_mod",
        "rust_parity_test_path": "tests/unit/test_rust_hotpath_parity.py",
        "rust_parity_timeout_s": 30,
        "enforce_rust_benchmark_gate": False,
    }
    defaults.update(overrides)
    return PromotionConfig(**defaults)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# Gate F disabled
# ---------------------------------------------------------------------------
class TestGateFDisabled:
    def test_skipped_when_disabled(self, tmp_path: Path) -> None:
        cfg = _cfg(enable_rust_readiness_gate=False)
        passed, details = _evaluate_gate_f(cfg, tmp_path)
        assert passed is True
        assert details["skipped"] is True
        assert details["checks"] == {}


# ---------------------------------------------------------------------------
# No rust module declared
# ---------------------------------------------------------------------------
class TestGateFNoRustModule:
    def test_fails_when_no_rust_module(self, tmp_path: Path) -> None:
        cfg = _cfg(rust_module_name=None)
        with patch(
            "hft_platform.alpha._gate_f._load_rust_module_name", return_value=""
        ):
            passed, details = _evaluate_gate_f(cfg, tmp_path)
        assert passed is False
        checks = details["checks"]
        assert checks["rust_module_declared"]["pass"] is False
        assert checks["rust_parity_tests"]["pass"] is False

    def test_no_module_with_benchmark_gate(self, tmp_path: Path) -> None:
        cfg = _cfg(
            rust_module_name=None,
            enforce_rust_benchmark_gate=True,
        )
        with patch(
            "hft_platform.alpha._gate_f._load_rust_module_name", return_value=""
        ):
            passed, details = _evaluate_gate_f(cfg, tmp_path)
        assert passed is False
        checks = details["checks"]
        assert checks["rust_perf_regression_gate"]["pass"] is False

    def test_empty_string_module_fails(self, tmp_path: Path) -> None:
        cfg = _cfg(rust_module_name="  ")
        with patch(
            "hft_platform.alpha._gate_f._load_rust_module_name", return_value=""
        ):
            passed, details = _evaluate_gate_f(cfg, tmp_path)
        assert passed is False


# ---------------------------------------------------------------------------
# Parity tests
# ---------------------------------------------------------------------------
class TestGateFParityTests:
    def test_parity_tests_pass(self, tmp_path: Path) -> None:
        cfg = _cfg()
        mock_proc = MagicMock()
        mock_proc.returncode = 0
        mock_proc.stdout = "1 passed"
        mock_proc.stderr = ""
        with patch("hft_platform.alpha._gate_f.subprocess.run", return_value=mock_proc):
            passed, details = _evaluate_gate_f(cfg, tmp_path)
        assert passed is True
        checks = details["checks"]
        assert checks["rust_module_declared"]["pass"] is True
        assert checks["rust_parity_tests"]["pass"] is True
        assert checks["rust_parity_tests"]["returncode"] == 0

    def test_parity_tests_fail(self, tmp_path: Path) -> None:
        cfg = _cfg()
        mock_proc = MagicMock()
        mock_proc.returncode = 1
        mock_proc.stdout = "FAILED"
        mock_proc.stderr = "assertion error"
        with patch("hft_platform.alpha._gate_f.subprocess.run", return_value=mock_proc):
            passed, details = _evaluate_gate_f(cfg, tmp_path)
        assert passed is False
        assert details["checks"]["rust_parity_tests"]["pass"] is False
        assert details["checks"]["rust_parity_tests"]["returncode"] == 1

    def test_parity_tests_timeout(self, tmp_path: Path) -> None:
        cfg = _cfg(rust_parity_timeout_s=10)
        exc = subprocess.TimeoutExpired(cmd=["pytest"], timeout=10)
        exc.stdout = "partial output"
        exc.stderr = "timeout stderr"
        with patch(
            "hft_platform.alpha._gate_f.subprocess.run", side_effect=exc
        ):
            passed, details = _evaluate_gate_f(cfg, tmp_path)
        assert passed is False
        check = details["checks"]["rust_parity_tests"]
        assert check["pass"] is False
        assert check["returncode"] == 124
        assert "timeout" in check["detail"]

    def test_parity_path_resolved_relative(self, tmp_path: Path) -> None:
        cfg = _cfg(rust_parity_test_path="tests/parity.py")
        mock_proc = MagicMock()
        mock_proc.returncode = 0
        mock_proc.stdout = "ok"
        mock_proc.stderr = ""
        with patch("hft_platform.alpha._gate_f.subprocess.run", return_value=mock_proc) as mock_run:
            _evaluate_gate_f(cfg, tmp_path)
        cmd = mock_run.call_args[0][0]
        assert str(tmp_path / "tests" / "parity.py") in cmd[-1]

    def test_parity_path_absolute(self, tmp_path: Path) -> None:
        abs_path = str(tmp_path / "abs_parity.py")
        cfg = _cfg(rust_parity_test_path=abs_path)
        mock_proc = MagicMock()
        mock_proc.returncode = 0
        mock_proc.stdout = ""
        mock_proc.stderr = ""
        with patch("hft_platform.alpha._gate_f.subprocess.run", return_value=mock_proc) as mock_run:
            _evaluate_gate_f(cfg, tmp_path)
        cmd = mock_run.call_args[0][0]
        assert abs_path in cmd[-1]


# ---------------------------------------------------------------------------
# Benchmark gate
# ---------------------------------------------------------------------------
class TestGateFBenchmark:
    def test_benchmark_pass(self, tmp_path: Path) -> None:
        cfg = _cfg(enforce_rust_benchmark_gate=True)
        mock_proc = MagicMock()
        mock_proc.returncode = 0
        mock_proc.stdout = "benchmark ok"
        mock_proc.stderr = ""
        with patch(
            "hft_platform.alpha._gate_f.subprocess.run", return_value=mock_proc
        ):
            passed, details = _evaluate_gate_f(cfg, tmp_path)
        assert passed is True
        assert details["checks"]["rust_perf_regression_gate"]["pass"] is True

    def test_benchmark_fail(self, tmp_path: Path) -> None:
        cfg = _cfg(enforce_rust_benchmark_gate=True)
        # First call (parity) passes, second (benchmark) fails
        proc_pass = MagicMock()
        proc_pass.returncode = 0
        proc_pass.stdout = "ok"
        proc_pass.stderr = ""
        proc_fail = MagicMock()
        proc_fail.returncode = 1
        proc_fail.stdout = "regression detected"
        proc_fail.stderr = ""
        with patch(
            "hft_platform.alpha._gate_f.subprocess.run",
            side_effect=[proc_pass, proc_fail],
        ):
            passed, details = _evaluate_gate_f(cfg, tmp_path)
        assert passed is False
        assert details["checks"]["rust_parity_tests"]["pass"] is True
        assert details["checks"]["rust_perf_regression_gate"]["pass"] is False

    def test_benchmark_timeout(self, tmp_path: Path) -> None:
        cfg = _cfg(enforce_rust_benchmark_gate=True, rust_parity_timeout_s=15)
        proc_pass = MagicMock()
        proc_pass.returncode = 0
        proc_pass.stdout = ""
        proc_pass.stderr = ""
        exc = subprocess.TimeoutExpired(cmd=["bench"], timeout=15)
        exc.stdout = ""
        exc.stderr = ""
        with patch(
            "hft_platform.alpha._gate_f.subprocess.run",
            side_effect=[proc_pass, exc],
        ):
            passed, details = _evaluate_gate_f(cfg, tmp_path)
        assert passed is False
        bench = details["checks"]["rust_perf_regression_gate"]
        assert bench["pass"] is False
        assert bench["returncode"] == 124

    def test_no_benchmark_when_not_enforced(self, tmp_path: Path) -> None:
        cfg = _cfg(enforce_rust_benchmark_gate=False)
        mock_proc = MagicMock()
        mock_proc.returncode = 0
        mock_proc.stdout = ""
        mock_proc.stderr = ""
        with patch("hft_platform.alpha._gate_f.subprocess.run", return_value=mock_proc):
            _, details = _evaluate_gate_f(cfg, tmp_path)
        assert "rust_perf_regression_gate" not in details["checks"]


# ---------------------------------------------------------------------------
# _load_rust_module_name
# ---------------------------------------------------------------------------
class TestLoadRustModuleName:
    @staticmethod
    def _mock_registry_module(registry_instance: object | None = None, raise_import: bool = False):
        """Patch the lazy import of research.registry.alpha_registry."""
        mock_mod = MagicMock()
        if raise_import:
            mock_mod.AlphaRegistry.side_effect = RuntimeError("no registry")
        elif registry_instance is not None:
            mock_mod.AlphaRegistry.return_value = registry_instance
        return patch.dict("sys.modules", {"research.registry.alpha_registry": mock_mod})

    def test_returns_empty_when_registry_fails(self, tmp_path: Path) -> None:
        with self._mock_registry_module(raise_import=True):
            result = _load_rust_module_name(tmp_path, "alpha_x")
        assert result == ""

    def test_returns_empty_when_alpha_not_found(self, tmp_path: Path) -> None:
        mock_registry = MagicMock()
        mock_registry.discover.return_value = {}
        with self._mock_registry_module(registry_instance=mock_registry):
            result = _load_rust_module_name(tmp_path, "missing_alpha")
        assert result == ""

    def test_returns_module_name(self, tmp_path: Path) -> None:
        mock_alpha = MagicMock()
        mock_alpha.manifest.rust_module = "rust_core.AlphaOFI"
        mock_registry = MagicMock()
        mock_registry.discover.return_value = {"my_alpha": mock_alpha}
        with self._mock_registry_module(registry_instance=mock_registry):
            result = _load_rust_module_name(tmp_path, "my_alpha")
        assert result == "rust_core.AlphaOFI"

    def test_returns_empty_when_rust_module_none(self, tmp_path: Path) -> None:
        mock_alpha = MagicMock()
        mock_alpha.manifest.rust_module = None
        mock_registry = MagicMock()
        mock_registry.discover.return_value = {"my_alpha": mock_alpha}
        with self._mock_registry_module(registry_instance=mock_registry):
            result = _load_rust_module_name(tmp_path, "my_alpha")
        assert result == ""


# ---------------------------------------------------------------------------
# Integration: rust_module from manifest fallback
# ---------------------------------------------------------------------------
class TestGateFModuleFromManifest:
    def test_uses_load_rust_module_name_when_config_empty(self, tmp_path: Path) -> None:
        cfg = _cfg(rust_module_name=None)
        mock_proc = MagicMock()
        mock_proc.returncode = 0
        mock_proc.stdout = ""
        mock_proc.stderr = ""
        with (
            patch(
                "hft_platform.alpha._gate_f._load_rust_module_name",
                return_value="rust_core.AlphaOFI",
            ),
            patch(
                "hft_platform.alpha._gate_f.subprocess.run",
                return_value=mock_proc,
            ),
        ):
            passed, details = _evaluate_gate_f(cfg, tmp_path)
        assert passed is True
        assert details["rust_module"] == "rust_core.AlphaOFI"
