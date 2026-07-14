#!/usr/bin/env python3
"""PreToolUse[Bash]: subagents never mutate git state (AGENTS.md routing:
git execution = orchestrator only). Read-only git subcommands pass; everything
else is denied for subagents. Main-session (orchestrator) calls are untouched.

This is a floor, not a sandbox: it parses command positions (segments split on
shell operators), so indirection like `g=git; $g push` is out of scope — the
orchestrator reviews diffs regardless. Unreadable stdin is treated as
main-session (harness always sends valid JSON; probe 2026-07-14)."""

import os
import re
import shlex
import sys

from hook_common import block, is_subagent, read_event

READONLY = {
    "status",
    "log",
    "diff",
    "show",
    "rev-parse",
    "ls-files",
    "grep",
    "blame",
    "describe",
    "merge-base",
    "for-each-ref",
    "name-rev",
    "shortlog",
    "cat-file",
    "check-ignore",
    "ls-remote",
    "var",
    "help",
}
LIST_FORMS = {
    "stash": ("list", "show"),
    "branch": ("", "--list", "-a", "-r", "--show-current", "-v", "-vv"),
    "tag": ("", "--list", "-l"),
    "remote": ("", "-v", "show", "get-url"),
    "worktree": ("list",),
    "config": ("--get", "--list", "--get-all", "--get-regexp"),
}
_ENV_ASSIGN = re.compile(r"^[A-Za-z_][A-Za-z_0-9]*=")
_WRAPPERS = {"command", "env", "timeout", "nice"}


def _git_invocations(cmd: str):
    """Yield (subcommand, rest_tokens) for each shell segment whose command is git."""
    for seg in re.split(r"(?:\|\||&&|;|\||\n|\$\(|`)", cmd):
        try:
            toks = shlex.split(seg)
        except ValueError:
            toks = seg.split()
        i = 0
        while i < len(toks) and _ENV_ASSIGN.match(toks[i]):
            i += 1
        if i < len(toks) and toks[i] in _WRAPPERS:
            i += 1
            while i < len(toks) and (toks[i].startswith("-") or toks[i].replace(".", "").isdigit()):
                i += 1
        if i >= len(toks) or os.path.basename(toks[i]) != "git":
            continue
        i += 1
        while i < len(toks) and toks[i].startswith("-"):
            i += 2 if toks[i] in ("-C", "-c") else 1
        if i < len(toks):
            yield toks[i], toks[i + 1 :]


def main() -> None:
    e = read_event()
    if not is_subagent(e):
        sys.exit(0)
    cmd = (e.get("tool_input") or {}).get("command") or ""
    for sub, rest in _git_invocations(cmd):
        if sub in READONLY:
            continue
        if sub in LIST_FORMS:
            first = rest[0] if rest else ""
            if first in LIST_FORMS[sub]:
                continue
            block(
                f"[git-guard] 'git {sub} {' '.join(rest)}' is not a read-only form; subagents never "
                "mutate git state. Report intent to the orchestrator."
            )
        block(
            f"[git-guard] subagents never run 'git {sub}' (AGENTS.md: git execution = "
            "orchestrator only). Report intent to the orchestrator."
        )
    sys.exit(0)


main()
