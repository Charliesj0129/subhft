"""Gate F evaluation — Rust readiness gate."""

from __future__ import annotations

import shlex
import subprocess
from pathlib import Path
from typing import Any

from hft_platform.alpha._promotion_types import PromotionConfig


def _evaluate_gate_f(config: PromotionConfig, root: Path) -> tuple[bool, dict[str, Any]]:
    if not bool(config.enable_rust_readiness_gate):
        return True, {"skipped": True, "reason": "enable_rust_readiness_gate=false", "checks": {}}

    rust_module = str(config.rust_module_name or "").strip() or _load_rust_module_name(root, config.alpha_id)
    checks: dict[str, dict[str, Any]] = {
        "rust_module_declared": {
            "value": rust_module or None,
            "required": True,
            "pass": bool(rust_module),
        }
    }
    if not rust_module:
        checks["rust_parity_tests"] = {
            "pass": False,
            "detail": "Skipped because rust_module is not declared in manifest",
        }
        if config.enforce_rust_benchmark_gate:
            checks["rust_perf_regression_gate"] = {
                "pass": False,
                "detail": "Skipped because rust_module is not declared in manifest",
            }
        return False, {"checks": checks, "rust_module": None}

    parity_path = Path(config.rust_parity_test_path)
    if not parity_path.is_absolute():
        parity_path = root / parity_path
    parity_cmd = ["uv", "run", "pytest", "-q", "--no-cov", str(parity_path)]
    try:
        parity_proc = subprocess.run(
            parity_cmd,
            cwd=str(root),
            capture_output=True,
            text=True,
            timeout=max(10, int(config.rust_parity_timeout_s)),
            check=False,
        )
        checks["rust_parity_tests"] = {
            "command": " ".join(parity_cmd),
            "returncode": int(parity_proc.returncode),
            "stdout_tail": parity_proc.stdout[-2000:],
            "stderr_tail": parity_proc.stderr[-2000:],
            "pass": parity_proc.returncode == 0,
        }
    except subprocess.TimeoutExpired as exc:
        checks["rust_parity_tests"] = {
            "command": " ".join(parity_cmd),
            "returncode": 124,
            "stdout_tail": (exc.stdout or "")[-2000:],
            "stderr_tail": (exc.stderr or "")[-2000:],
            "pass": False,
            "detail": f"timeout after {int(config.rust_parity_timeout_s)}s",
        }

    if bool(config.enforce_rust_benchmark_gate):
        bench_cmd = shlex.split(str(config.rust_benchmark_cmd))
        try:
            bench_proc = subprocess.run(
                bench_cmd,
                cwd=str(root),
                capture_output=True,
                text=True,
                timeout=max(10, int(config.rust_parity_timeout_s)),
                check=False,
            )
            checks["rust_perf_regression_gate"] = {
                "command": " ".join(bench_cmd),
                "returncode": int(bench_proc.returncode),
                "stdout_tail": bench_proc.stdout[-2000:],
                "stderr_tail": bench_proc.stderr[-2000:],
                "pass": bench_proc.returncode == 0,
            }
        except subprocess.TimeoutExpired as exc:
            checks["rust_perf_regression_gate"] = {
                "command": " ".join(bench_cmd),
                "returncode": 124,
                "stdout_tail": (exc.stdout or "")[-2000:],
                "stderr_tail": (exc.stderr or "")[-2000:],
                "pass": False,
                "detail": f"timeout after {int(config.rust_parity_timeout_s)}s",
            }

    passed = all(bool(v.get("pass", False)) for v in checks.values())
    return passed, {"checks": checks, "rust_module": rust_module}


def _load_rust_module_name(root: Path, alpha_id: str) -> str:
    try:
        from research.registry.alpha_registry import AlphaRegistry

        registry = AlphaRegistry()
        loaded = registry.discover(str(root / "research" / "alphas"))
        alpha = loaded.get(alpha_id)
        if alpha is None:
            return ""
        rust_module = getattr(alpha.manifest, "rust_module", None)
        return str(rust_module or "").strip()
    except Exception as _exc:  # noqa: BLE001
        return ""
