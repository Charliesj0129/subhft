#!/usr/bin/env python3
"""PreToolUse[Edit|Write|NotebookEdit]: during a delegation window, writes outside
the packet allowlist are denied (fail-closed). No window -> no-op.

Window = .agent/runtime/active-packet.json, written by the orchestrator before
spawning an executor and deleted at LAND (pipeline-implement skill)."""

import fnmatch
import json
import os
import sys

from hook_common import block, is_subagent, read_event

MARKER = ".agent/runtime/active-packet.json"


def _allowed(rel: str, patterns: list[str]) -> bool:
    return any(fnmatch.fnmatch(rel, p) or rel.startswith(p.rstrip("*")) for p in patterns)


def main() -> None:
    if not os.path.exists(MARKER):
        sys.exit(0)
    e = read_event()
    ti = e.get("tool_input") or {}
    path = ti.get("file_path") or ti.get("notebook_path") or ""
    if path.startswith("/tmp/claude-"):  # scratchpad always allowed
        sys.exit(0)
    rel = os.path.relpath(path) if os.path.isabs(path) else path
    try:
        with open(MARKER) as f:
            m = json.load(f)
    except Exception:
        block("[scope-guard] active-packet.json unreadable during delegation window; refusing writes (fail-closed).")
        return
    allowed = list(m.get("allowed", [])) + [".agent/runtime/*"]
    if _allowed(rel.replace("\\", "/"), allowed):
        sys.exit(0)
    if m.get("orchestrator_bypass", True) and not is_subagent(e):
        sys.exit(0)
    block(
        f"[scope-guard] delegation window '{m.get('id')}' active: '{rel}' is outside ALLOWED FILES. "
        "Report this as a blocker per your packet; do not edit around it."
    )


main()
