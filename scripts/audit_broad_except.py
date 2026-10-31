#!/usr/bin/env python3
"""Read-only inventory of broad ``except`` handlers under ``src/hft_platform`` (candidate inventory N89, N11).

``scripts/check_discipline.py`` HFT-D001 only reports a handler whose body is a single ``pass``/``...``. This script
looks at every handler that catches *everything* (bare, ``Exception``, ``BaseException``, or a tuple holding one of
them) and says what the handler does with the failure: nothing, a debug line, a log line, a counter, a re-raise.

It never edits a file and never imports the code it scans (``ast`` only). It is an audit aid, not a gate: it exits 0
unless the root does not exist. The categories under ``--list`` are heuristics read off the ``try`` body; treat them as
a starting point for review, not as a verdict.

    python scripts/audit_broad_except.py                     # markdown summary of src/hft_platform
    python scripts/audit_broad_except.py --format json
    python scripts/audit_broad_except.py --list money        # one line per handler in a group
"""

from __future__ import annotations

import argparse
import ast
import json
import sys
from collections import Counter
from collections.abc import Iterable, Iterator
from dataclasses import asdict, dataclass
from pathlib import Path

BROAD_NAMES = frozenset({"Exception", "BaseException"})
LOG_METHODS = frozenset({"debug", "info", "warning", "warn", "error", "critical", "exception", "log"})
METRIC_METHODS = frozenset({"inc", "dec", "set", "observe", "labels", "remove"})
CLEANUP_METHODS = frozenset(
    {"close", "cancel", "shutdown", "unlink", "stop", "wait_closed", "logout", "disconnect", "kill", "terminate"}
    | {"flush", "release", "join", "rmdir", "rmtree", "remove_done_callback"}
)
PARSE_NAMES = frozenset({"int", "float", "str", "loads", "decode", "parse", "fromisoformat", "strptime", "Decimal"})

# Top-level package -> group. Hot path per AGENTS.md: ingestion, normalizer, LOB, feature engine, event bus,
# strategy dispatch, risk, gateway, order/execution. ``recorder`` is off the hot path but holds the WAL.
GROUPS = {
    "order": "money",
    "risk": "money",
    "gateway": "money",
    "execution": "money",
    "feed_adapter": "market-data",
    "engine": "market-data",
    "feature": "market-data",
    "strategy": "market-data",
    "recorder": "recorder",
    "services": "services",
}
SHAPES = ("silent", "fallback", "debug-only", "logs", "counts", "reraises", "other")


@dataclass(frozen=True, slots=True)
class Handler:
    file: str
    line: int
    group: str
    caught: str  # bare | Exception | BaseException | tuple
    shape: str  # one of SHAPES
    commented: bool
    try_kind: str  # metrics-guard | cleanup | parse | other  (heuristic)
    body: str  # the handler body as source when it is one statement ("pass", "continue", ...), else "multi"


def group_of(rel: Path) -> str:
    parts = rel.parts
    if len(parts) == 1:
        return "root"
    return GROUPS.get(parts[0], "other")


def caught_kind(handler: ast.ExceptHandler) -> str | None:
    """``None`` when the handler is not broad."""
    exc = handler.type
    if exc is None:
        return "bare"
    if isinstance(exc, ast.Name) and exc.id in BROAD_NAMES:
        return exc.id
    if isinstance(exc, ast.Tuple) and any(isinstance(e, ast.Name) and e.id in BROAD_NAMES for e in exc.elts):
        return "tuple"
    return None


def _is_silent_statement(stmt: ast.stmt) -> bool:
    """``pass`` / ``...`` / ``continue`` / ``break`` / bare ``return`` / ``return None`` (the N89 definition)."""
    if isinstance(stmt, (ast.Pass, ast.Continue, ast.Break)):
        return True
    if isinstance(stmt, ast.Expr) and isinstance(stmt.value, ast.Constant) and stmt.value.value is ...:
        return True
    if isinstance(stmt, ast.Return):
        return stmt.value is None or (isinstance(stmt.value, ast.Constant) and stmt.value.value is None)
    return False


def _is_fallback_statement(stmt: ast.stmt) -> bool:
    """``return <constant>`` other than ``None``: the failure becomes a plausible-looking value."""
    return isinstance(stmt, ast.Return) and isinstance(stmt.value, ast.Constant) and stmt.value.value is not None


def _call_method_names(nodes: Iterable[ast.AST]) -> list[tuple[str, str]]:
    """(receiver-name-lowered, method) for every attribute call under ``nodes``."""
    out: list[tuple[str, str]] = []
    for root in nodes:
        for node in ast.walk(root):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
                receiver = node.func.value
                name = receiver.id if isinstance(receiver, ast.Name) else getattr(receiver, "attr", "")
                out.append((str(name).lower(), node.func.attr))
    return out


def shape_of(body: list[ast.stmt]) -> str:
    if all(_is_silent_statement(s) for s in body):
        return "silent"
    if all(_is_silent_statement(s) or _is_fallback_statement(s) for s in body):
        return "fallback"
    if any(isinstance(n, ast.Raise) for s in body for n in ast.walk(s)):
        return "reraises"
    calls = _call_method_names(body)
    log_levels = {m for r, m in calls if m in LOG_METHODS and ("log" in r or r in {"structlog", "self"})}
    if log_levels:
        return "debug-only" if log_levels == {"debug"} else "logs"
    if any(m in METRIC_METHODS for _, m in calls):
        return "counts"
    return "other"


def try_kind_of(try_body: list[ast.stmt]) -> str:
    methods = {m for _, m in _call_method_names(try_body)}
    names = {
        n.func.id for s in try_body for n in ast.walk(s) if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)
    }
    if methods and methods <= METRIC_METHODS | {"get"}:
        return "metrics-guard"
    if methods & CLEANUP_METHODS:
        return "cleanup"
    if (names | methods) & PARSE_NAMES:
        return "parse"
    return "other"


def body_text(body: list[ast.stmt]) -> str:
    if len(body) != 1:
        return "multi"
    stmt = body[0]
    if isinstance(stmt, ast.Expr) and isinstance(stmt.value, ast.Constant) and stmt.value.value is ...:
        return "..."
    return ast.unparse(stmt)


def is_commented(handler: ast.ExceptHandler, lines: list[str]) -> bool:
    """A ``#`` on the ``except`` line, on the first body line, or a comment line directly above."""
    candidates = [handler.lineno, handler.body[0].lineno]
    if any("#" in lines[n - 1] for n in candidates if 0 < n <= len(lines)):
        return True
    above = handler.lineno - 2
    return 0 <= above < len(lines) and lines[above].strip().startswith("#")


def scan_file(path: Path, root: Path) -> list[Handler]:
    source = path.read_text(encoding="utf-8")
    try:
        tree = ast.parse(source, filename=str(path))
    except SyntaxError:
        return []
    lines = source.splitlines()
    rel = path.relative_to(root)
    group = group_of(rel)
    found: list[Handler] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Try):
            continue
        kind = try_kind_of(node.body)
        for handler in node.handlers:
            caught = caught_kind(handler)
            if caught is None:
                continue
            found.append(
                Handler(
                    file=str(rel),
                    line=handler.lineno,
                    group=group,
                    caught=caught,
                    shape=shape_of(handler.body),
                    commented=is_commented(handler, lines),
                    try_kind=kind,
                    body=body_text(handler.body),
                )
            )
    return found


def iter_sources(root: Path) -> Iterator[Path]:
    for path in sorted(root.rglob("*.py")):
        if "__pycache__" not in path.parts and not path.name.startswith("test_"):
            yield path


def scan(root: Path) -> list[Handler]:
    handlers: list[Handler] = []
    for path in iter_sources(root):
        handlers.extend(scan_file(path, root))
    return sorted(handlers, key=lambda h: (h.file, h.line))


def summarize(handlers: list[Handler]) -> dict[str, dict[str, int]]:
    table: dict[str, Counter[str]] = {}
    for h in handlers:
        row = table.setdefault(h.group, Counter())
        row[h.shape] += 1
        row["total"] += 1
        if h.shape == "silent" and not h.commented:
            row["silent, no comment"] += 1
    return {group: dict(row) for group, row in sorted(table.items())}


def render_markdown(handlers: list[Handler]) -> str:
    table = summarize(handlers)
    columns = [*SHAPES, "total", "silent, no comment"]
    out = [f"Broad handlers (bare / Exception / BaseException): {len(handlers)}", ""]
    out.append("| group | " + " | ".join(columns) + " |")
    out.append("|---|" + "---|" * len(columns))
    totals: Counter[str] = Counter()
    for group, row in table.items():
        out.append(f"| {group} | " + " | ".join(str(row.get(c, 0)) for c in columns) + " |")
        totals.update(row)
    out.append("| **all** | " + " | ".join(str(totals.get(c, 0)) for c in columns) + " |")
    return "\n".join(out)


def render_list(handlers: list[Handler]) -> str:
    return "\n".join(
        f"{h.file}:{h.line}  {h.caught:<13} {h.shape:<10} {'commented' if h.commented else 'NO-COMMENT':<10} "
        f"{h.try_kind:<13} {h.body}"
        for h in handlers
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--root", default="src/hft_platform", help="package directory to scan")
    parser.add_argument("--format", choices=("markdown", "json"), default="markdown")
    parser.add_argument("--list", metavar="GROUP", help="print one line per handler of GROUP (or 'all')")
    parser.add_argument("--shape", choices=SHAPES, help="with --list: only handlers of this shape")
    args = parser.parse_args(argv)
    if args.shape and not args.list:
        parser.error("--shape needs --list")

    root = Path(args.root)
    if not root.is_dir():
        print(f"audit_broad_except: {root} is not a directory", file=sys.stderr)
        return 2
    handlers = scan(root)
    if args.list:
        selected = [h for h in handlers if args.list in ("all", h.group) and (not args.shape or h.shape == args.shape)]
        print(render_list(selected))
    elif args.format == "json":
        print(json.dumps({"handlers": [asdict(h) for h in handlers], "summary": summarize(handlers)}, indent=2))
    else:
        print(render_markdown(handlers))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
