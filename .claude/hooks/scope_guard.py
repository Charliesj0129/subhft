#!/usr/bin/env python3
"""PreToolUse[Edit|Write|NotebookEdit]: during a delegation window, writes outside
the packet allowlist are denied (fail-closed). No window -> no-op.

Window = .agent/runtime/active-packet.json, written by the orchestrator before
spawning an executor and deleted at LAND (pipeline-implement skill). Subagents
can never write the runtime markers themselves (reviewer finding F2 2026-07-14).
Unreadable stdin falls through to the orchestrator bypass (harness always sends
valid JSON; probe 2026-07-14) — a floor, not a sandbox."""

import fnmatch
import json
import os
import sys

from hook_common import block, is_subagent, read_event

MARKER = ".agent/runtime/active-packet.json"


def _allowed(rel: str, patterns: list[str]) -> bool:
    """Exact fnmatch per pattern; directory grants must end with '/' or '/*'.
    A bare file pattern never grants prefix siblings (test_foo.py.orig)."""
    for p in patterns:
        if fnmatch.fnmatch(rel, p):
            return True
        if p.endswith("/*") and rel.startswith(p[:-1]):
            return True
        if p.endswith("/") and rel.startswith(p):
            return True
    return False


def main() -> None:
    if not os.path.exists(MARKER):
        sys.exit(0)
    e = read_event()
    ti = e.get("tool_input") or {}
    path = os.path.normpath(ti.get("file_path") or ti.get("notebook_path") or "")
    if path.startswith("/tmp/claude-"):  # scratchpad (normalized: traversal cannot fake this)
        sys.exit(0)
    rel = os.path.relpath(path) if os.path.isabs(path) else path
    rel = rel.replace("\\", "/")
    try:
        with open(MARKER) as f:
            m = json.load(f)
    except Exception:
        block("[scope-guard] active-packet.json unreadable during delegation window; refusing writes (fail-closed).")
        return
    subagent = is_subagent(e)
    if subagent and rel.startswith(".agent/runtime"):
        block("[scope-guard] runtime markers are orchestrator-owned; subagents never modify them.")
    if _allowed(rel, list(m.get("allowed", []))):
        sys.exit(0)
    if m.get("orchestrator_bypass", True) and not subagent:
        sys.exit(0)
    block(
        f"[scope-guard] delegation window '{m.get('id')}' active: '{rel}' is outside ALLOWED FILES. "
        "Report this as a blocker per your packet; do not edit around it."
    )


main()
