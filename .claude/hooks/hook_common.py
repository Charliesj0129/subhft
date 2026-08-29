"""Shared helpers for Claude Code hooks. stdlib-only; never print secrets."""

import json
import os
import re
import shlex
import sys
from typing import Iterator

_ENV_ASSIGN = re.compile(r"^[A-Za-z_][A-Za-z_0-9]*=")
_WRAPPERS = {"command", "env", "timeout", "nice"}
# Global options that take a separate value argument, per tool.
_GIT_OPTS_WITH_VALUE = {"-C", "-c", "--namespace", "--work-tree", "--git-dir", "--exec-path"}
_GH_OPTS_WITH_VALUE = {"-R", "--repo", "--hostname"}


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


def _segments(cmd: str) -> Iterator[list[str]]:
    """Yield the token list of each shell segment of a command string.

    Splitting on shell operators is what lets `a && git push` be seen as a git
    invocation while `echo "git push"` is not -- shlex drops the quotes, so a
    quoted mention lands inside a single token rather than at position 0.
    """
    for seg in re.split(r"(?:\|\||&&|;|\||\n|\$\(|`)", cmd):
        try:
            toks = shlex.split(seg)
        except ValueError:
            toks = seg.split()
        if toks:
            yield toks


def _skip_prefix(toks: list[str]) -> int:
    """Index of the program name, past env assignments and wrapper commands."""
    i = 0
    while i < len(toks) and _ENV_ASSIGN.match(toks[i]):
        i += 1
    if i < len(toks) and toks[i] in _WRAPPERS:
        i += 1
        while i < len(toks) and (toks[i].startswith("-") or toks[i].replace(".", "").isdigit()):
            i += 1
    return i


def _skip_global_options(toks: list[str], i: int, with_value: set[str]) -> int:
    """Index of the first non-option token, past `git -C <dir>` / `gh --repo <x>`.

    An option written `--repo=x` carries its value in the same token, so only the
    bare form consumes the next one.
    """
    while i < len(toks) and toks[i].startswith("-"):
        i += 2 if toks[i] in with_value else 1
    return i


def git_invocations(cmd: str) -> Iterator[tuple[str, list[str]]]:
    """Yield (subcommand, rest_tokens) for each shell segment whose command is git.

    Global options are skipped, so `git -C /repo push` and `git -c k=v push` both
    yield ("push", [...]) -- a raw `\\bgit\\s+push\\b` regex sees neither.

    This is a floor, not a sandbox: indirection like `g=git; $g push` is out of
    scope. The orchestrator reviews diffs regardless.
    """
    for toks in _segments(cmd):
        i = _skip_prefix(toks)
        if i >= len(toks) or os.path.basename(toks[i]) != "git":
            continue
        i = _skip_global_options(toks, i + 1, _GIT_OPTS_WITH_VALUE)
        if i < len(toks):
            yield toks[i], toks[i + 1 :]


def gh_invocations(cmd: str) -> Iterator[tuple[str, str, list[str]]]:
    """Yield (group, subcommand, rest_tokens) for each `gh` invocation.

    `gh --repo owner/name pr merge 460` yields ("pr", "merge", ["460"]).
    A bare `gh pr` with no subcommand yields ("pr", "", []).
    """
    for toks in _segments(cmd):
        i = _skip_prefix(toks)
        if i >= len(toks) or os.path.basename(toks[i]) != "gh":
            continue
        i = _skip_global_options(toks, i + 1, _GH_OPTS_WITH_VALUE)
        if i >= len(toks):
            continue
        group = toks[i]
        j = _skip_global_options(toks, i + 1, _GH_OPTS_WITH_VALUE)
        sub = toks[j] if j < len(toks) else ""
        yield group, sub, toks[j + 1 :]
