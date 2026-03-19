"""Verify zero test collection errors. Exit 1 if ERROR lines found."""

from __future__ import annotations
import subprocess, sys


def main():
    r = subprocess.run(
        [sys.executable, "-m", "pytest", "--collect-only", "-q", "--no-header"],
        capture_output=True,
        text=True,
        timeout=120,
    )
    errs = [l for l in (r.stdout + r.stderr).splitlines() if l.strip().startswith("ERROR")]
    if errs:
        print(f"Test collection errors ({len(errs)}):")
        for l in errs:
            print(f"  {l}")
        return 1
    print("Test collection OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
