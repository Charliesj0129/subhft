"""Audit the complete public Python dependency lock without installing extras."""

from __future__ import annotations

import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def main() -> int:
    if not (ROOT / "uv.lock").is_file():
        print("Security audit failed: repository uv.lock is missing.", flush=True)
        return 1

    with tempfile.TemporaryDirectory(prefix="hft-security-audit-") as directory:
        requirements = Path(directory) / "requirements.txt"
        export = [
            "uv",
            "export",
            "--locked",
            "--format",
            "requirements-txt",
            "--no-hashes",
            "--no-emit-project",
            "--group",
            "dev",
            "--group",
            "research",
            "--extra",
            "bot",
            "--extra",
            "monitor",
            "--output-file",
            str(requirements),
        ]
        try:
            result = subprocess.run(export, cwd=ROOT, capture_output=True, text=True, check=False)
        except OSError as error:
            print(f"Security audit failed: cannot run uv export: {error}", flush=True)
            return 1
        if result.returncode:
            print("Security audit failed: locked dependency export failed.", flush=True)
            print(result.stdout, end="", flush=True)
            print(result.stderr, end="", flush=True)
            return result.returncode
        if not requirements.is_file() or not requirements.read_text().strip():
            print("Security audit failed: locked dependency export is empty or missing.", flush=True)
            return 1

        print("Auditing locked runtime, dev, research, bot and monitor dependencies.", flush=True)
        print("The private Fubon SDK is outside the public lock and is not audited.", flush=True)
        audit = [
            sys.executable,
            "-m",
            "pip_audit",
            "--requirement",
            str(requirements),
            "--strict",
            "--no-deps",
            "--disable-pip",
            "--progress-spinner",
            "off",
        ]
        try:
            return subprocess.run(audit, cwd=ROOT, stderr=subprocess.STDOUT, check=False).returncode
        except OSError as error:
            print(f"Security audit failed: cannot run pip-audit: {error}", flush=True)
            return 1


if __name__ == "__main__":
    raise SystemExit(main())
