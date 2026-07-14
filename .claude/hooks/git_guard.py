#!/usr/bin/env python3
"""PreToolUse[Bash]: subagents never mutate git state (AGENTS.md routing:
git execution = orchestrator only). Read-only git subcommands pass; everything
else is denied for subagents. Main-session (orchestrator) calls are untouched."""

import re
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


def main() -> None:
    e = read_event()
    if not is_subagent(e):
        sys.exit(0)
    cmd = (e.get("tool_input") or {}).get("command") or ""
    for m in re.finditer(r"\bgit\s+(?:-[^\s]+\s+)*([a-z-]+)([^|;&]*)", cmd):
        sub, rest = m.group(1), m.group(2).strip()
        if sub in READONLY:
            continue
        if sub in LIST_FORMS:
            first = rest.split()[0] if rest.split() else ""
            if first in LIST_FORMS[sub]:
                continue
            block(
                f"[git-guard] 'git {sub} {rest}' is not a read-only form; subagents never "
                "mutate git state. Report intent to the orchestrator."
            )
        block(
            f"[git-guard] subagents never run 'git {sub}' (AGENTS.md: git execution = "
            "orchestrator only). Report intent to the orchestrator."
        )
    sys.exit(0)


main()
