"""Shared helpers for Claude Code hooks. stdlib-only; never print secrets."""

import json
import sys


def read_event() -> dict:
    try:
        return json.load(sys.stdin)
    except Exception:
        return {}


def block(reason: str) -> None:
    """PreToolUse: exit 2 denies the call; stderr goes back to the model."""
    print(reason, file=sys.stderr)
    sys.exit(2)


def warn(reason: str) -> None:
    """PostToolUse: exit 2 is non-blocking feedback to the model."""
    print(reason, file=sys.stderr)
    sys.exit(2)


def is_subagent(event: dict) -> bool:
    """Probe verdict 2026-07-14 (variant a): subagent tool calls carry
    `agent_type` (e.g. "hft-docs") and `agent_id` in the hook input; main-session
    calls have neither. session_id is SHARED between a session and its subagents,
    so session-identity comparison (variant b) is not usable."""
    return bool(event.get("agent_type"))
