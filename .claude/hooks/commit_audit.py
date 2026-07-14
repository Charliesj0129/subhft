#!/usr/bin/env python3
"""PostToolUse[Bash]: after a `git commit`, compare HEAD's files against the
declared allowlist marker (.agent/runtime/commit-allowlist.json). Advisory
second line behind the narrow-commit gate; fail-open."""

import json
import os
import re
import subprocess
import sys

from hook_common import read_event, warn

MARKER = ".agent/runtime/commit-allowlist.json"


def main() -> None:
    e = read_event()
    cmd = (e.get("tool_input") or {}).get("command") or ""
    if not re.search(r"\bgit\b.*\bcommit\b", cmd) or not os.path.exists(MARKER):
        sys.exit(0)
    try:
        with open(MARKER) as f:
            allowed = set(json.load(f).get("allowed", []))
        out = subprocess.run(
            ["git", "diff-tree", "--no-commit-id", "--name-only", "-r", "HEAD"],
            capture_output=True,
            text=True,
            timeout=10,
        )
        files = out.stdout.split()
    except Exception:
        sys.exit(0)  # fail-open
    extra = sorted(f for f in files if f not in allowed)
    if extra:
        warn(
            f"[commit-audit] HEAD contains files outside the declared allowlist: {extra}. "
            "Verify against ALLOWED_PATHS; amend/split only with the usual approvals."
        )
    sys.exit(0)


main()
