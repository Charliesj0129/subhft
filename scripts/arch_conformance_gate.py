"""Architecture conformance gate.

Exit 0 if no errors, exit 1 if errors found. Warnings don't cause failure.
"""

from __future__ import annotations

import re, sys
from pathlib import Path

SRC_ROOT = Path(__file__).resolve().parent.parent / "src" / "hft_platform"


def _is_comment_or_string(line):
    s = line.lstrip()
    return s.startswith("#") or s.startswith('"""') or s.startswith("'''") or s.startswith('"') or s.startswith("'")


def _in_docstring(lines, idx):
    in_ds, qc = False, ""
    for i, l in enumerate(lines):
        s = l.strip()
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
            return True
    return False


def ck_mb02(e, w):
    for f in SRC_ROOT.rglob("*.py"):
        r = f.relative_to(SRC_ROOT)
        if r.parts and r.parts[0] == "feed_adapter":
            continue
        for n, line in enumerate(open(f, errors="replace"), 1):
            for sdk in ("shioaji", "fubon_neo"):
                if re.search(rf"\bimport\s+{sdk}\b", line) and not _is_comment_or_string(line):
                    e.append(f"MB-02: {r}:{n} — import {sdk} outside feed_adapter/")


def ck_slots(e, w):
    for f in SRC_ROOT.rglob("*.py"):
        r = f.relative_to(SRC_ROOT)
        if not r.parts or r.parts[0] not in {"execution", "risk", "order", "strategy", "gateway"}:
            continue
        lines = f.read_text(errors="replace").splitlines()
        for i, line in enumerate(lines):
            s = line.strip()
            if s == "@dataclass" and i + 1 < len(lines) and lines[i + 1].strip().startswith("class "):
                cn = lines[i + 1].strip().split("(")[0].replace("class ", "").rstrip(":")
                e.append(f"SLOTS: {r}:{i + 1} — {cn} missing slots=True")


def ck_datetime(e, w):
    pat = re.compile(r"datetime\.now\(\)")
    for f in SRC_ROOT.rglob("*.py"):
        r = f.relative_to(SRC_ROOT)
        if r.parts and r.parts[0] in {"alpha", "config", "scripts", "monitor", "backtest", "research"}:
            continue
        lines = f.read_text(errors="replace").splitlines()
        for i, line in enumerate(lines):
            if pat.search(line) and not _is_comment_or_string(line) and not _in_docstring(lines, i):
                e.append(f"NO-DATETIME: {r}:{i + 1} — datetime.now() usage")


def main():
    errors, warnings = [], []
    ck_mb02(errors, warnings)
    ck_slots(errors, warnings)
    ck_datetime(errors, warnings)
    for w_ in warnings:
        print(f"  WARN: {w_}")
    for e_ in errors:
        print(f"  ERROR: {e_}")
    print(f"\nArch conformance: {len(errors)} errors, {len(warnings)} warnings")
    if errors:
        print("FAIL")
        return 1
    print("PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
