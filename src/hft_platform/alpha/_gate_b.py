from __future__ import annotations

import subprocess
from pathlib import Path

from hft_platform.alpha._validation_types import GateReport


def run_gate_b(alpha_id: str, project_root: Path, skip_tests: bool = False, timeout_s: int = 300) -> GateReport:
    from hft_platform.alpha._validation_helpers import _validate_alpha_id

    _validate_alpha_id(alpha_id)
    if skip_tests:
        return GateReport(
            gate="Gate B",
            passed=True,
            details={"skipped": True, "reason": "skip_gate_b_tests=true"},
        )

    test_path = project_root / "research" / "alphas" / alpha_id / "tests"
    cmd = ["uv", "run", "python", "-m", "pytest", "-q", "--no-cov", str(test_path)]
    try:
        proc = subprocess.run(
            cmd,
            cwd=str(project_root),
            capture_output=True,
            text=True,
            timeout=timeout_s,
            check=False,
        )
        passed = proc.returncode == 0
        return GateReport(
            gate="Gate B",
            passed=passed,
            details={
                "command": " ".join(cmd),
                "returncode": proc.returncode,
                "stdout_tail": proc.stdout[-4000:],
                "stderr_tail": proc.stderr[-2000:],
            },
        )
    except subprocess.TimeoutExpired as exc:
        return GateReport(
            gate="Gate B",
            passed=False,
            details={
                "command": " ".join(cmd),
                "error": f"timeout after {timeout_s}s",
                "stdout_tail": (exc.stdout or "")[-4000:],
            },
        )
