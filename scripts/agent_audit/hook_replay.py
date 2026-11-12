#!/usr/bin/env python3
"""Differential replay of recorded Bash commands through two versions of the hooks.

Use before merging a change to `.claude/hooks/`: it answers "which real commands
does this change decide differently?" with the commands agents actually ran,
instead of with the cases someone thought to write down.

    python3 scripts/agent_audit/hook_replay.py OLD_HOOKS_DIR NEW_HOOKS_DIR [TRANSCRIPTS_DIR]

OLD_HOOKS_DIR is usually a copy of the hooks at the merge base:

    mkdir -p /tmp/old_hooks && for f in hook_common git_guard codex_review_gate review_attestation; do
        git show origin/main:.claude/hooks/$f.py > /tmp/old_hooks/$f.py; done

TRANSCRIPTS_DIR defaults to this checkout's Claude Code project directory.

Nothing is executed: each hook is handed the command as text and returns a
decision. `gh pr merge` commands are not sent to the review gate, because
judging them makes the gate ask GitHub for the PR head. Output is counts and
subcommand names only, never the recorded commands themselves.

Exit status is 1 when a command moves from allow to block under git_guard, or
when the review gate newly allows a command that contains a publish verb. Either
is something to read, not automatically a defect: a push the gate can now
resolve by refspec is judged by its real rule instead of being called
unreadable. Expect several minutes for ~20k commands (one process per verdict).
"""

from __future__ import annotations

import collections
import importlib.util
import json
import re
import subprocess
import sys
from pathlib import Path
from types import ModuleType

REPO = Path(__file__).resolve().parents[2]
GH_MERGE = re.compile(r"\bgh\b[^\n;|&]*\bpr\s+merge\b")


def _load(hooks: Path, name: str) -> ModuleType:
    spec = importlib.util.spec_from_file_location(name, hooks / "hook_common.py")
    if spec is None or spec.loader is None:
        raise SystemExit(f"cannot load {hooks / 'hook_common.py'}")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _parse(mod: ModuleType, cmd: str) -> tuple:
    try:
        git = tuple((sub, tuple(rest)) for sub, rest, _cwd in mod.git_invocations(cmd))
        gh = tuple((group, sub) for group, sub, _rest, _repo in mod.gh_invocations(cmd))
        return git, gh, tuple(mod.unattributed_segments(cmd))
    except Exception as exc:  # a parser crash is itself a difference worth seeing
        return ("crash", type(exc).__name__)


def _decide(hooks: Path, script: str, cmd: str, *, subagent: bool) -> int:
    event: dict = {"tool_name": "Bash", "tool_input": {"command": cmd}, "cwd": str(REPO)}
    if subagent:
        event["agent_type"] = "replay"
    done = subprocess.run(
        [sys.executable, str(hooks / script)],
        input=json.dumps(event),
        capture_output=True,
        text=True,
        cwd=REPO,
        timeout=60,
        check=False,
    )
    return done.returncode


def _commands(transcripts: Path) -> set[str]:
    found: set[str] = set()
    for path in [*transcripts.glob("*.jsonl"), *transcripts.glob("*/subagents/*.jsonl")]:
        with path.open(errors="replace") as fh:
            for line in fh:
                if '"Bash"' not in line:
                    continue
                try:
                    record = json.loads(line)
                except ValueError:
                    continue
                message = record.get("message")
                if record.get("type") != "assistant" or not isinstance(message, dict):
                    continue
                for block in message.get("content") or []:
                    if isinstance(block, dict) and block.get("type") == "tool_use" and block.get("name") == "Bash":
                        cmd = (block.get("input") or {}).get("command")
                        if cmd:
                            found.add(cmd)
    return found


def main() -> int:
    if len(sys.argv) < 3:
        print(__doc__)
        return 2
    old_dir, new_dir = Path(sys.argv[1]).resolve(), Path(sys.argv[2]).resolve()
    default = Path.home() / ".claude" / "projects" / str(REPO).replace("/", "-").replace("_", "-")
    transcripts = Path(sys.argv[3]) if len(sys.argv) > 3 else default
    old, new = _load(old_dir, "hook_common_old"), _load(new_dir, "hook_common_new")

    cmds = _commands(transcripts)
    changed = [c for c in cmds if _parse(old, c) != _parse(new, c)]
    print(f"commands replayed: {len(cmds)}; parsed differently: {len(changed)}")

    guard: collections.Counter = collections.Counter()
    gate: collections.Counter = collections.Counter()
    allowed_subs: collections.Counter = collections.Counter()
    failures = 0
    for cmd in changed:
        before = _decide(old_dir, "git_guard.py", cmd, subagent=True)
        after = _decide(new_dir, "git_guard.py", cmd, subagent=True)
        guard[(before, after)] += 1
        subs = {sub for sub, _rest, _cwd in new.git_invocations(cmd)}
        if (before, after) == (2, 0):
            allowed_subs.update(subs)
        if (before, after) == (0, 2):
            failures += 1
            print(f"  git_guard allow->block, subcommands now visible: {sorted(subs)}")
        if GH_MERGE.search(cmd):
            gate["not sent (gh pr merge)"] += 1
            continue
        before = _decide(old_dir, "codex_review_gate.py", cmd, subagent=False)
        after = _decide(new_dir, "codex_review_gate.py", cmd, subagent=False)
        gate[(before, after)] += 1
        gh = {f"{group} {sub}" for group, sub, _rest, _repo in new.gh_invocations(cmd)}
        if (before, after) == (2, 0) and ("push" in subs or "pr create" in gh):
            failures += 1
            print(f"  review gate block->allow WITH a publish verb: git {sorted(subs)} gh {sorted(gh)}")

    print("git_guard as a subagent, (old, new) exit codes:", dict(guard))
    print("  subcommands in block->allow commands:", allowed_subs.most_common())
    print("review gate, (old, new) exit codes:", dict(gate))

    # A verdict can also change with the parse unchanged (an edited allow list).
    unchanged: collections.Counter = collections.Counter()
    for cmd in cmds.difference(changed):
        if not any(True for _ in new.git_invocations(cmd)):
            continue
        before = _decide(old_dir, "git_guard.py", cmd, subagent=True)
        after = _decide(new_dir, "git_guard.py", cmd, subagent=True)
        if before != after:
            unchanged[(before, after)] += 1
            if (before, after) == (0, 2):
                failures += 1
    print("git_guard verdict changes with the parse unchanged:", dict(unchanged))
    print(f"needs a look: {failures}")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
