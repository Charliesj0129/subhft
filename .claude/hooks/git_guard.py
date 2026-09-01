#!/usr/bin/env python3
"""PreToolUse[Bash]: subagents never mutate git state (AGENTS.md routing:
git execution = orchestrator only). Read-only git subcommands pass; everything
else is denied for subagents. Main-session (orchestrator) calls are untouched.

Command parsing (shell segments, env assignments, wrappers, global options) is
shared with the review gate in `hook_common.git_invocations`. Unreadable stdin is
treated as main-session (harness always sends valid JSON; probe 2026-07-14)."""

import sys

from hook_common import block, git_invocations, is_subagent, read_event, unattributed_segments

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
    for seg in unattributed_segments(cmd):
        # `env -u GIT_CONFIG git commit --amend` and `timeout -s TERM 5 git
        # reset --hard HEAD^` both parsed to NO invocation at all, so a subagent
        # could destroy user work through a wrapper option that takes a value.
        # An unreadable git command is not a safe one.
        block(
            f"[git-guard] could not read the git command in {seg!r}. Subagents never mutate "
            "git state, and a command this hook cannot parse is not evidence that it is "
            "read-only. Report intent to the orchestrator."
        )
    for sub, rest, _cwd in git_invocations(cmd):
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
