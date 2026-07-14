#!/usr/bin/env python3
"""PostToolUse[Edit|Write]: run the AST discipline gate on the edited platform file.
Advisory: NEVER blocks (fail-open); findings return to the model via stderr so
hot-path law violations surface immediately instead of at `make check`."""

import os
import subprocess
import sys

from hook_common import read_event, warn


def main() -> None:
    e = read_event()
    path = (e.get("tool_input") or {}).get("file_path") or ""
    if "src/hft_platform/" not in path or not path.endswith(".py"):
        sys.exit(0)
    if not os.path.exists("scripts/check_discipline.py"):
        sys.exit(0)  # fail-open outside the repo root
    try:
        r = subprocess.run(
            [sys.executable, "scripts/check_discipline.py", "--files", path],
            capture_output=True,
            text=True,
            timeout=20,
        )
    except Exception:
        sys.exit(0)  # fail-open: advisory hook never blocks work
    if r.returncode != 0:
        warn(f"[discipline] check_discipline flagged {path}:\n{(r.stdout or r.stderr)[-1500:]}")
    sys.exit(0)


main()
