"""Shared helpers for Claude Code hooks. stdlib-only; never print secrets."""

import json
import os
import re
import shlex
import sys
from typing import Iterator

_ENV_ASSIGN = re.compile(r"^[A-Za-z_][A-Za-z_0-9]*=")
# Wrapper commands, mapped to the options of theirs that take a SEPARATE value.
# Arity matters: `env -u GIT_CONFIG git commit` used to stop parsing at
# GIT_CONFIG and report no git invocation at all, which let a subagent run
# `timeout -s TERM 5 git reset --hard HEAD^` straight past the mutation guard.
# Skipping "anything that looks like a flag" is not enough -- the value is the
# token that does not look like one.
_WRAPPERS = {
    "command": {},
    "env": {"-u", "--unset", "-C", "--chdir", "-S", "--split-string"},
    "timeout": {"-s", "--signal", "-k", "--kill-after"},
    "nice": {"-n", "--adjustment"},
}
# Global options that take a separate value argument, per tool.
_GIT_OPTS_WITH_VALUE = {"-C", "-c", "--namespace", "--work-tree", "--git-dir", "--exec-path"}
_GH_OPTS_WITH_VALUE = {"-R", "--repo", "--hostname"}


def read_event() -> dict:
    """The hook event, or {} when stdin is not a JSON object.

    Valid JSON that is not an object (`"not json at all"` parses to a str) got
    past the except: clause and every caller then died on `.get`. An unreadable
    event must degrade to "no event", not to a traceback -- a hook that crashes
    on its own input is a hook that gets uninstalled.
    """
    try:
        e = json.load(sys.stdin)
    except Exception:
        return {}
    return e if isinstance(e, dict) else {}


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
    """Index of the program name, past env assignments and wrapper commands.

    Loops rather than unwrapping once: `env LANG=C git push` put a NAME=value
    operand where the old code expected a flag, so it stopped there and reported
    no git invocation at all -- a bypass of BOTH this gate and the subagent
    mutation guard. `timeout 5 env A=b git push` nests the same trap.
    """
    i = 0
    while i < len(toks):
        if _ENV_ASSIGN.match(toks[i]):
            i += 1
            continue
        wrapper = _WRAPPERS.get(os.path.basename(toks[i]))
        if wrapper is not None:
            i += 1
            # The wrapper's own options, then `env -i` / `timeout 5` operands.
            # `--opt=value` carries its value inside the token; `--opt value`
            # does not, so the next token is consumed rather than inspected.
            while i < len(toks):
                tok = toks[i]
                if tok == "--":
                    i += 1
                    break
                if tok in wrapper:
                    i += 2
                    continue
                if tok.startswith("-") or tok.replace(".", "").isdigit():
                    i += 1
                    continue
                break
            continue
        break
    return i


def unattributed_segments(cmd: str) -> list[str]:
    """Segments naming git/gh somewhere that the parser could not attribute.

    The parser is a floor, not a sandbox, and its docstring says so. But there
    is a difference between "this command does not run git" and "this command
    runs git in a shape I could not read", and returning an empty iterator for
    both is what turned an arity bug into a silent bypass. Callers decide what
    to do; both of ours block, because not knowing what is about to run is the
    state that must not pass.
    """
    out: list[str] = []
    for toks in _segments(cmd):
        i = _skip_prefix(toks)
        if i < len(toks) and os.path.basename(toks[i]) in ("git", "gh"):
            continue
        if any(os.path.basename(t) in ("git", "gh") for t in toks[i:]):
            out.append(" ".join(toks))
    return out


def _skip_global_options(toks: list[str], i: int, with_value: set[str]) -> tuple[int, str | None]:
    """(index of the first non-option token, value of a -C/--repo style option).

    The `-C <dir>` value is returned rather than discarded: a push resolved in
    the session's cwd while git publishes another worktree's HEAD would attest
    the wrong objects entirely.
    """
    cwd: str | None = None
    while i < len(toks) and toks[i].startswith("-"):
        if toks[i] == "-C" and i + 1 < len(toks):
            cwd = toks[i + 1]
        elif toks[i].startswith("-C") and len(toks[i]) > 2:
            cwd = toks[i][2:]
        i += 2 if toks[i] in with_value else 1
    return i, cwd


def flag_value(toks: list[str], names: tuple[str, ...]) -> str | None:
    """Value of the first of `names` found ANYWHERE in `toks`.

    Positional-then-option is a valid gh form (`gh pr merge 460 --repo o/n`), so
    the repository cannot be recovered by walking leading options alone -- which
    is how `--repo` came back None from a command that plainly carried one.
    """
    for j, tok in enumerate(toks):
        if tok in names and j + 1 < len(toks):
            return toks[j + 1]
        for n in names:
            if tok.startswith(f"{n}="):
                return tok.split("=", 1)[1]
    return None


def git_invocations(cmd: str) -> Iterator[tuple[str, list[str], str | None]]:
    """Yield (subcommand, rest_tokens, cwd_override) for each git invocation.

    Global options are skipped, so `git -C /repo push` and `git -c k=v push`
    both yield a "push" -- a raw `\\bgit\\s+push\\b` regex sees neither. `-C`'s
    value comes back with it, because the directory git operates on decides
    which HEAD is being published.

    This is a floor, not a sandbox: indirection like `g=git; $g push` is out of
    scope. The orchestrator reviews diffs regardless.
    """
    for toks in _segments(cmd):
        i = _skip_prefix(toks)
        if i >= len(toks) or os.path.basename(toks[i]) != "git":
            continue
        i, cwd = _skip_global_options(toks, i + 1, _GIT_OPTS_WITH_VALUE)
        if i < len(toks):
            yield toks[i], toks[i + 1 :], cwd


def gh_invocations(cmd: str) -> Iterator[tuple[str, str, list[str], str | None]]:
    """Yield (group, subcommand, rest_tokens, repo) for each `gh` invocation.

    `gh --repo owner/name pr merge 460` yields ("pr", "merge", ["460"],
    "owner/name"). A bare `gh pr` with no subcommand yields ("pr", "", [], None).

    The repository is RETURNED, not discarded. `gh` acts server-side on whatever
    `--repo` names, while the gate resolves the PR head in the session's cwd: a
    discarded `--repo` let a reviewed PR in this repository authorize the merge
    of an entirely different, unreviewed PR somewhere else.
    """
    for toks in _segments(cmd):
        i = _skip_prefix(toks)
        if i >= len(toks) or os.path.basename(toks[i]) != "gh":
            continue
        repo = flag_value(toks[i + 1 :], ("-R", "--repo"))
        i, _ = _skip_global_options(toks, i + 1, _GH_OPTS_WITH_VALUE)
        if i >= len(toks):
            continue
        group = toks[i]
        j, _ = _skip_global_options(toks, i + 1, _GH_OPTS_WITH_VALUE)
        sub = toks[j] if j < len(toks) else ""
        yield group, sub, toks[j + 1 :], repo
