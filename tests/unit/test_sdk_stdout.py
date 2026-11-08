"""fd-1 isolation for broker-SDK prints (utils/sdk_stdout.py).

Each case runs in a child process: redirecting fd 1 inside the pytest process
would swallow pytest's own output. The child writes to fd 1 with ``os.write``,
the way the SDK's native core does, so ``sys.stdout`` buffering is not what is
being tested.

The wiring tests parse the entry points with ``ast`` instead of searching the
source text, so a commented-out call or a call moved behind the SDK-creating
call does not satisfy them.
"""

from __future__ import annotations

import ast
import os
import subprocess
import sys
from pathlib import Path

_SRC_ROOT = Path(__file__).resolve().parents[2] / "src" / "hft_platform"
_ISOLATE_NAMES = frozenset({"isolate_sdk_stdout", "sdk_stdout.isolate_sdk_stdout"})

_CHILD = """
import os
from hft_platform.utils.sdk_stdout import isolate_sdk_stdout
mode = isolate_sdk_stdout()
os.write(1, b"Info: #P2P/v:x/y/PYAPI/A123456789/1023/ | Event: Session up\\n")
os.write(2, ("mode=" + mode + "\\n").encode())
"""


def _run_child(env_value: str | None) -> subprocess.CompletedProcess[bytes]:
    env = {k: v for k, v in os.environ.items() if k != "HFT_SDK_STDOUT"}
    if env_value is not None:
        env["HFT_SDK_STDOUT"] = env_value
    return subprocess.run([sys.executable, "-c", _CHILD], capture_output=True, env=env, timeout=20, check=True)


def test_sdk_stdout_default_drops_fd1_writes():
    proc = _run_child(None)
    assert proc.stdout == b""
    assert b"mode=devnull" in proc.stderr


def test_sdk_stdout_inherit_keeps_fd1_writes():
    proc = _run_child("inherit")
    assert b"PYAPI/A123456789" in proc.stdout
    assert b"mode=inherit" in proc.stderr


def test_sdk_stdout_unknown_value_fails_closed_to_devnull():
    proc = _run_child("off")
    assert proc.stdout == b""
    assert b"mode=devnull" in proc.stderr


def _dotted(node: ast.AST) -> str | None:
    """``asyncio.run`` -> "asyncio.run", ``f`` -> "f"; anything else -> None."""
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        base = _dotted(node.value)
        return f"{base}.{node.attr}" if base else None
    return None


def _isolate_stmt_index(body: list[ast.stmt]) -> int | None:
    """Index of the first statement that is an unconditional isolate call (bare or assigned)."""
    for i, stmt in enumerate(body):
        if (
            isinstance(stmt, (ast.Expr, ast.Assign, ast.AnnAssign))
            and isinstance(stmt.value, ast.Call)
            and _dotted(stmt.value.func) in _ISOLATE_NAMES
        ):
            return i
    return None


def _first_stmt_index_calling(body: list[ast.stmt], names: frozenset[str]) -> int | None:
    """Index of the first statement that contains a call to any of ``names``."""
    for i, stmt in enumerate(body):
        for node in ast.walk(stmt):
            if isinstance(node, ast.Call) and _dotted(node.func) in names:
                return i
    return None


def _isolate_precedes(body: list[ast.stmt], names: frozenset[str]) -> bool:
    iso = _isolate_stmt_index(body)
    target = _first_stmt_index_calling(body, names)
    return iso is not None and target is not None and iso < target


def _main_block(tree: ast.Module) -> list[ast.stmt]:
    for node in tree.body:
        test = node.test if isinstance(node, ast.If) else None
        if (
            isinstance(test, ast.Compare)
            and isinstance(test.left, ast.Name)
            and test.left.id == "__name__"
            and len(test.comparators) == 1
            and isinstance(test.comparators[0], ast.Constant)
            and test.comparators[0].value == "__main__"
        ):
            return node.body
    raise AssertionError('no `if __name__ == "__main__":` block')


def _function_body(tree: ast.Module, name: str) -> list[ast.stmt]:
    for node in tree.body:
        if isinstance(node, ast.FunctionDef) and node.name == name:
            return node.body
    raise AssertionError(f"no top-level function {name}()")


def _parse_src(rel: str) -> ast.Module:
    path = _SRC_ROOT / rel
    return ast.parse(path.read_text(encoding="utf-8"), filename=str(path))


def test_sdk_stdout_main_entry_isolates_before_running():
    body = _main_block(_parse_src("main.py"))
    assert _isolate_precedes(body, frozenset({"asyncio.run"}))


def test_sdk_stdout_cli_run_isolates_before_system_construction():
    body = _function_body(_parse_src("cli/_run.py"), "cmd_run")
    assert _isolate_precedes(body, frozenset({"HFTSystem"}))


def test_sdk_stdout_wiring_check_rejects_commented_out_and_late_calls():
    commented = ast.parse("# isolate_sdk_stdout()\nasyncio.run(main())\n").body
    assert not _isolate_precedes(commented, frozenset({"asyncio.run"}))
    late = ast.parse("asyncio.run(main())\nisolate_sdk_stdout()\n").body
    assert not _isolate_precedes(late, frozenset({"asyncio.run"}))
    conditional = ast.parse("if debug:\n    isolate_sdk_stdout()\nasyncio.run(main())\n").body
    assert not _isolate_precedes(conditional, frozenset({"asyncio.run"}))
    ok = ast.parse("isolate_sdk_stdout()\nasyncio.run(main())\n").body
    assert _isolate_precedes(ok, frozenset({"asyncio.run"}))
