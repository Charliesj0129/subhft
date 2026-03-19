<<<<<<< HEAD
"""Architecture conformance gate.

Exit 0 if no errors, exit 1 if errors found. Warnings don't cause failure.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

SRC_ROOT = Path(__file__).resolve().parent.parent / "src" / "hft_platform"


def _is_comment_or_string(line: str) -> bool:
    s = line.lstrip()
    return s.startswith("#") or s.startswith('"""') or s.startswith("'''") or s.startswith('"') or s.startswith("'")


def _in_docstring(lines: list[str], idx: int) -> bool:
    in_ds, qc = False, ""
    for i, line in enumerate(lines):
        s = line.strip()
        if not in_ds:
            for q in ('"""', "'''"):
                if q in s:
                    in_ds = s.count(q) == 1
                    if in_ds:
                        qc = q
                    break
        elif qc in s:
            in_ds, qc = False, ""
        if i == idx and in_ds:
=======
"""Arch conformance gate."""

from __future__ import annotations
import re, sys
from pathlib import Path

SRC = Path(__file__).resolve().parent.parent / "src" / "hft_platform"


def _cmt(l):
    s = l.lstrip()
    return s.startswith("#") or s.startswith('"""') or s.startswith("'''") or s.startswith('"') or s.startswith("'")


def _ds(ls, i):
    in_d, qc = False, ""
    for j, l in enumerate(ls):
        s = l.strip()
        if not in_d:
            for q in ('"""', "'''"):
                if q in s:
                    in_d = s.count(q) == 1
                    qc = q if in_d else ""
                    break
        elif qc in s:
            in_d, qc = False, ""
        if j == i and in_d:
>>>>>>> origin/main
            return True
    return False


<<<<<<< HEAD
def ck_mb02(errors: list[str], warnings: list[str]) -> None:
    for f in SRC_ROOT.rglob("*.py"):
        r = f.relative_to(SRC_ROOT)
        if r.parts and r.parts[0] == "feed_adapter":
            continue
        for n, line in enumerate(open(f, errors="replace"), 1):
            for sdk in ("shioaji", "fubon_neo"):
                if re.search(rf"\bimport\s+{sdk}\b", line) and not _is_comment_or_string(line):
                    errors.append(f"MB-02: {r}:{n} — import {sdk} outside feed_adapter/")


def ck_slots(errors: list[str], warnings: list[str]) -> None:
    for f in SRC_ROOT.rglob("*.py"):
        r = f.relative_to(SRC_ROOT)
        if not r.parts or r.parts[0] not in {"execution", "risk", "order", "strategy", "gateway"}:
            continue
        lines = f.read_text(errors="replace").splitlines()
        for i, line in enumerate(lines):
            if line.strip() == "@dataclass" and i + 1 < len(lines) and lines[i + 1].strip().startswith("class "):
                cn = lines[i + 1].strip().split("(")[0].replace("class ", "").rstrip(":")
                errors.append(f"SLOTS: {r}:{i + 1} — {cn} missing slots=True")


def ck_datetime(errors: list[str], warnings: list[str]) -> None:
    pat = re.compile(r"datetime\.now\(\)")
    exempt = {"alpha", "config", "scripts", "monitor", "backtest", "research"}
    for f in SRC_ROOT.rglob("*.py"):
        r = f.relative_to(SRC_ROOT)
        if r.parts and r.parts[0] in exempt:
            continue
        lines = f.read_text(errors="replace").splitlines()
        for i, line in enumerate(lines):
            if pat.search(line) and not _is_comment_or_string(line) and not _in_docstring(lines, i):
                errors.append(f"NO-DATETIME: {r}:{i + 1} — datetime.now() usage")


def main() -> int:
    errors: list[str] = []
    warnings: list[str] = []
    ck_mb02(errors, warnings)
    ck_slots(errors, warnings)
    ck_datetime(errors, warnings)
    for w in warnings:
        print(f"  WARN: {w}")
    for e in errors:
        print(f"  ERROR: {e}")
    print(f"\nArch conformance: {len(errors)} errors, {len(warnings)} warnings")
    if errors:
=======
def ck1(e, w):
    for f in SRC.rglob("*.py"):
        r = f.relative_to(SRC)
        if r.parts and r.parts[0] == "feed_adapter":
            continue
        for n, l in enumerate(open(f, errors="replace"), 1):
            for sdk in ("shioaji", "fubon_neo"):
                if re.search(rf"\\bimport\\s+{sdk}\\b", l) and not _cmt(l):
                    e.append(f"MB-02: {r}:{n}")


def ck2(e, w):
    for f in SRC.rglob("*.py"):
        r = f.relative_to(SRC)
        if not r.parts or r.parts[0] not in {"execution", "risk", "order", "strategy", "gateway"}:
            continue
        ls = f.read_text(errors="replace").splitlines()
        for i, l in enumerate(ls):
            if l.strip() == "@dataclass" and i + 1 < len(ls) and ls[i + 1].strip().startswith("class "):
                cn = ls[i + 1].strip().split("(")[0].replace("class ", "").rstrip(":")
                e.append(f"SLOTS: {r}:{i + 1} — {cn}")


def ck3(e, w):
    p = re.compile(r"datetime\\.now\\(\\)")
    for f in SRC.rglob("*.py"):
        r = f.relative_to(SRC)
        if r.parts and r.parts[0] in {"alpha", "config", "scripts", "monitor", "backtest", "research"}:
            continue
        ls = f.read_text(errors="replace").splitlines()
        for i, l in enumerate(ls):
            if p.search(l) and not _cmt(l) and not _ds(ls, i):
                e.append(f"NO-DATETIME: {r}:{i + 1}")


def main():
    e, w = [], []
    ck1(e, w)
    ck2(e, w)
    ck3(e, w)
    for x in w:
        print(f"  WARN: {x}")
    for x in e:
        print(f"  ERROR: {x}")
    print(f"\nArch: {len(e)} errors, {len(w)} warnings")
    if e:
>>>>>>> origin/main
        print("FAIL")
        return 1
    print("PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
