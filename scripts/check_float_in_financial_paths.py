#!/usr/bin/env python3
"""Float gate for the money-touching packages (Law 4: prices and accounting values are scaled int x10000).

Flags ``: float`` annotations and ``float(`` calls in ``order/``, ``risk/``, ``execution/`` and
``feed_adapter/normalizer.py``. A flagged line passes only when

* ``scripts/float_gate_exemptions.yaml`` lists it (file + exact code text with the comment removed, at most
  ``count`` lines, a ``kind`` and a non-empty ``reason``), or
* it carries ``# precision-ok: <reason>`` with a non-empty reason.

An exemption that matches fewer lines than it claims fails the gate, so the list cannot go stale silently, and a
new float line fails until somebody writes down why it is fine. This replaces a shell pipeline of 49
``grep -v`` substrings that were matched against ``file:line:text``: a word in a path or a free-text comment
(``precision-``, ``normalizer``) exempted a whole line, or a whole file.

Exit codes: 0 clean, 1 findings or a bad exemption file, 2 unreadable input.
"""

from __future__ import annotations

import argparse
import re
import sys
import tokenize
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_EXEMPTIONS = Path(__file__).resolve().with_name("float_gate_exemptions.yaml")
SCAN = (
    "src/hft_platform/order",
    "src/hft_platform/risk",
    "src/hft_platform/execution",
    "src/hft_platform/feed_adapter/normalizer.py",
)
PATTERN = re.compile(r":\s*float\b|float\(")
MARKER = re.compile(r"#\s*precision-ok\b(?P<rest>.*)$")
KINDS = frozenset({"not-money", "offline-only", "boundary-conversion", "known-violation"})
NEEDS_TRACKING = frozenset({"boundary-conversion", "known-violation"})


@dataclass(frozen=True)
class Hit:
    path: str
    line: int
    code: str
    marker: str | None  # reason text of a valid inline marker
    bare_marker: bool  # ``# precision-ok`` without a reason


class InputError(Exception):
    pass


def _comments(path: Path) -> dict[int, tuple[int, str]]:
    """Line number -> (column, text) of the comment on that line, from the tokenizer (strings are not comments)."""
    found: dict[int, tuple[int, str]] = {}
    try:
        with tokenize.open(path) as fh:  # text mode: same newline handling and BOM stripping as scan() below
            for tok in tokenize.generate_tokens(fh.readline):
                if tok.type == tokenize.COMMENT:
                    found[tok.start[0]] = (tok.start[1], tok.string)
    except (OSError, UnicodeDecodeError, tokenize.TokenError, SyntaxError) as exc:
        raise InputError(f"cannot tokenize {path}: {exc}") from exc
    return found


def scan(root: Path, targets: tuple[str, ...] = SCAN) -> list[Hit]:
    hits: list[Hit] = []
    for target in targets:
        base = root / target
        if base.is_file():
            files = [base]
        elif base.is_dir():
            files = sorted(base.rglob("*.py"))
        else:
            raise InputError(f"scan target does not exist: {target}")
        for file in files:
            rel = file.relative_to(root).as_posix()
            comments = _comments(file)
            try:
                # Not str.splitlines(): it also breaks on \x0b, \x0c, \x1c-\x1e, \x85, U+2028 and U+2029, which the
                # tokenizer does not, so every later line would be numbered differently from the comments above.
                with file.open(encoding="utf-8-sig", newline=None) as fh:
                    lines = fh.read().split("\n")
            except (OSError, UnicodeDecodeError) as exc:
                raise InputError(f"cannot read {rel}: {exc}") from exc
            for number, line in enumerate(lines, 1):
                comment = comments.get(number)
                code = (line[: comment[0]] if comment else line).rstrip()
                if not PATTERN.search(code):
                    continue
                marker = None
                bare = False
                if comment:
                    match = MARKER.search(comment[1])
                    if match:
                        rest = match.group("rest").strip()
                        marker = rest[1:].strip() or None if rest.startswith(":") else None
                        bare = marker is None
                hits.append(Hit(rel, number, code.strip(), marker, bare))
    return hits


def load_exemptions(path: Path) -> tuple[dict[tuple[str, str], dict], list[str]]:
    """Return ({(file, code): entry}, errors)."""
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError) as exc:
        raise InputError(f"cannot read exemptions {path}: {exc}") from exc
    entries = data.get("exemptions") if isinstance(data, dict) else None
    if not isinstance(entries, list):
        raise InputError(f"{path}: top level must be a mapping with an 'exemptions' list")
    errors: list[str] = []
    table: dict[tuple[str, str], dict] = {}
    for index, entry in enumerate(entries):
        where = f"{path.name} entry {index + 1}"
        if not isinstance(entry, dict):
            errors.append(f"{where}: not a mapping")
            continue
        file, code = entry.get("file"), entry.get("code")
        if not isinstance(file, str) or not isinstance(code, str) or not file or not code.strip():
            errors.append(f"{where}: 'file' and 'code' must be non-empty strings")
            continue
        where = f"{file}: {code.strip()[:60]!r}"
        kind = entry.get("kind")
        if kind not in KINDS:
            errors.append(f"{where}: kind must be one of {sorted(KINDS)}, got {kind!r}")
        reason = entry.get("reason")
        if not isinstance(reason, str) or not reason.strip():
            errors.append(f"{where}: reason must not be empty")
        if kind in NEEDS_TRACKING and not str(entry.get("tracked_by") or "").strip():
            errors.append(f"{where}: kind {kind} needs tracked_by")
        count = entry.get("count", 1)
        if not isinstance(count, int) or isinstance(count, bool) or count < 1:
            errors.append(f"{where}: count must be a positive integer")
            count = 1
        key = (file, code.strip())
        if key in table:
            errors.append(f"{where}: listed twice (use count)")
            continue
        table[key] = {**entry, "count": count}
    return table, errors


def check(root: Path, exemptions: Path, targets: tuple[str, ...] = SCAN) -> tuple[list[str], list[str], dict[str, int]]:
    """Return (findings, errors, per-kind counts of exempted lines)."""
    hits = scan(root, targets)
    table, errors = load_exemptions(exemptions)
    findings: list[str] = []
    kinds: dict[str, int] = defaultdict(int)
    covered: dict[tuple[str, str], int] = defaultdict(int)

    for hit in hits:
        if hit.marker:
            kinds["inline-marker"] += 1
            continue
        key = (hit.path, hit.code)
        entry = table.get(key)
        if entry is not None and covered[key] < entry["count"]:
            covered[key] += 1
            kinds[str(entry.get("kind"))] += 1
            continue
        why = "'# precision-ok' needs ': <reason>'" if hit.bare_marker else "not exempt"
        if entry is not None:
            why = f"exemption covers {entry['count']} line(s), this is another one"
        findings.append(f"{hit.path}:{hit.line}: {hit.code}  [{why}]")

    for (file, code), entry in table.items():
        if covered[(file, code)] < entry["count"]:
            errors.append(
                f"{file}: exemption {code[:60]!r} matches {covered[(file, code)]} line(s), expects {entry['count']}"
                " (the code changed or was removed: update or delete the entry)"
            )
    return findings, errors, dict(kinds)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--root", type=Path, default=ROOT)
    parser.add_argument("--exemptions", type=Path, default=DEFAULT_EXEMPTIONS)
    args = parser.parse_args(argv)
    try:
        findings, errors, kinds = check(args.root, args.exemptions)
    except InputError as exc:
        print(f"float-gate: {exc}", file=sys.stderr)
        return 2
    if findings:
        print("float-gate: float in a money path without an exemption (Law 4: scaled int x10000):", file=sys.stderr)
        for item in findings:
            print(f"  {item}", file=sys.stderr)
        print(
            "Use a scaled int, or add '# precision-ok: <reason>' to the line, or list it in "
            f"{args.exemptions.name} with a reason.",
            file=sys.stderr,
        )
    for item in errors:
        print(f"float-gate: {item}", file=sys.stderr)
    summary = ", ".join(f"{name}={count}" for name, count in sorted(kinds.items())) or "none"
    if findings or errors:
        print(f"float-gate: FAILED ({len(findings)} finding(s), {len(errors)} exemption error(s)); exempted: {summary}")
        return 1
    print(f"float-gate: OK; exempted lines by kind: {summary}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
