"""Verify zero test collection errors. Warn-only (exit 0)."""

from __future__ import annotations

import subprocess
import sys


def main() -> int:
    result = subprocess.run(
        [sys.executable, "-m", "pytest", "--collect-only", "-q", "--no-header"],
        capture_output=True,
        text=True,
        timeout=120,
    )
    output = result.stdout + result.stderr
    error_lines = [line for line in output.splitlines() if line.strip().startswith("ERROR")]
    if error_lines:
        print(f"Test collection warnings ({len(error_lines)}):")
        for line in error_lines:
            print(f"  {line}")
        print("WARNING: test collection has errors (non-blocking)")
    else:
        print("Test collection OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
