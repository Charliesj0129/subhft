#!/usr/bin/env python3
"""Report metrics that are defined in the registry but never used, and the alerts that depend on them.

Report mode by default (exit 0, findings listed). ``--strict`` exits 1 when anything but the informational
``dynamic_only_metric`` rows is found.

A metric is *defined* by ``self.<attr> = Counter|Gauge|Histogram|...(_pn("<name>"), ...)`` in
``observability/metrics.py``. It is *used* when ``<attr>`` appears as an attribute anywhere else in
``src/hft_platform`` (including a pre-bound ``child = metrics.<attr>.labels(...)``) or inside
``metrics.py`` outside its own assignment.

Categories (a finding names one):
  unreferenced_metric           defined, but nothing reads the attribute, ``getattr``s it or names it in a string
  dynamic_only_metric           reached only through ``getattr(obj, "<attr>")`` or another string literal; usually
                                alive (informational), listed so a reader can confirm it is really written
  alert_on_unreferenced_metric  an active (not commented out) alert or recording rule whose ``expr`` uses an
                                ``unreferenced_metric``: the series never moves, so the rule cannot do its job

What it cannot see (so a clean report is not proof): a metric that is bound to a variable and then never
``inc``/``set``/``observe``d, attributes built with ``getattr(obj, f"...")``, series produced outside the registry
(textfile collectors, exporters), and ``HFT_METRICS_PREFIX`` (alert expressions are compared unprefixed).
"""

from __future__ import annotations

import argparse
import ast
import json
import re
import sys
from collections import defaultdict
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Iterable

import yaml

CATEGORIES = ("unreferenced_metric", "dynamic_only_metric", "alert_on_unreferenced_metric")
INFORMATIONAL = ("dynamic_only_metric",)  # listed for a reader to confirm; never fails --strict
METRIC_KINDS = {"Counter", "Gauge", "Histogram", "Summary", "Info", "Enum"}
DYNAMIC_ACCESSORS = {"getattr", "hasattr", "setattr"}
METRICS_RELPATH = Path("src/hft_platform/observability/metrics.py")
SRC_RELPATH = Path("src/hft_platform")
RULE_DIRS = ("config/monitoring/alerts", "config/monitoring/recording_rules")
DASHBOARD_DIRS = ("config/monitoring/dashboards", "config/monitoring/grafana")
SERIES_SUFFIXES = ("_bucket", "_sum", "_count", "_total", "_created")


@dataclass(frozen=True, slots=True)
class Finding:
    category: str
    file: str
    line: int
    detail: str


@dataclass(frozen=True, slots=True)
class Definition:
    attr: str
    name: str
    kind: str
    line: int


@dataclass(slots=True)
class Refs:
    attribute: int = 0
    dynamic: int = 0
    string: int = 0
    files: set[str] = field(default_factory=set)


@dataclass(frozen=True, slots=True)
class Consumer:
    kind: str  # "alert" | "record" | "dashboard"
    name: str
    file: str
    line: int


def parse_definitions(metrics_py: Path) -> dict[str, Definition]:
    """Return ``{attr: Definition}`` for every ``self.<attr> = <MetricKind>(...)`` assignment."""
    tree = ast.parse(metrics_py.read_text(encoding="utf-8"))
    defs: dict[str, Definition] = {}
    for node in ast.walk(tree):
        if not (isinstance(node, ast.Assign) and len(node.targets) == 1 and isinstance(node.value, ast.Call)):
            continue
        target = node.targets[0]
        if not (isinstance(target, ast.Attribute) and isinstance(target.value, ast.Name) and target.value.id == "self"):
            continue
        func = node.value.func
        kind = func.id if isinstance(func, ast.Name) else (func.attr if isinstance(func, ast.Attribute) else "")
        if kind not in METRIC_KINDS:
            continue
        defs[target.attr] = Definition(target.attr, _exported_name(node.value, target.attr), kind, node.lineno)
    return defs


def _exported_name(call: ast.Call, fallback: str) -> str:
    if not call.args:
        return fallback
    first = call.args[0]
    if isinstance(first, ast.Call) and first.args and isinstance(first.args[0], ast.Constant):
        value = first.args[0].value  # _pn("name")
        return value if isinstance(value, str) else fallback
    if isinstance(first, ast.Constant) and isinstance(first.value, str):
        return first.value
    return fallback


def scan_references(src_root: Path, metrics_py: Path, attrs: Iterable[str], root: Path) -> dict[str, Refs]:
    """Count, per metric attribute, attribute loads, ``getattr``-style string arguments and other strings."""
    wanted = set(attrs)
    refs: dict[str, Refs] = defaultdict(Refs)
    metrics_resolved = metrics_py.resolve()
    for path in sorted(src_root.rglob("*.py")):
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except (SyntaxError, UnicodeDecodeError):
            continue
        is_registry = path.resolve() == metrics_resolved
        rel = _rel(path, root)
        dynamic_nodes: set[int] = set()
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Name)
                and node.func.id in DYNAMIC_ACCESSORS
                and len(node.args) >= 2
                and isinstance(node.args[1], ast.Constant)
                and node.args[1].value in wanted
            ):
                dynamic_nodes.add(id(node.args[1]))
                if not is_registry:
                    ref = refs[node.args[1].value]
                    ref.dynamic += 1
                    ref.files.add(rel)
        for node in ast.walk(tree):
            if isinstance(node, ast.Attribute) and node.attr in wanted:
                if is_registry and isinstance(node.ctx, ast.Store):
                    continue
                ref = refs[node.attr]
                ref.attribute += 1
                ref.files.add(rel)
            elif (
                not is_registry
                and isinstance(node, ast.Constant)
                and isinstance(node.value, str)
                and node.value in wanted
                and id(node) not in dynamic_nodes
            ):
                ref = refs[node.value]
                ref.string += 1
                ref.files.add(rel)
    return refs


def _series_pattern(name: str) -> re.Pattern[str]:
    return re.compile(
        r"(?<![A-Za-z0-9_:])" + re.escape(name) + r"(?:" + "|".join(SERIES_SUFFIXES) + r")?(?![A-Za-z0-9_:])"
    )


def _rule_files(root: Path) -> Iterable[Path]:
    for rel in RULE_DIRS:
        base = root / rel
        if base.is_dir():
            yield from sorted(p for p in base.iterdir() if p.suffix in (".yml", ".yaml"))


def scan_rule_consumers(root: Path, names: Iterable[str]) -> dict[str, list[Consumer]]:
    """Active alert and recording rules (parsed YAML, so commented-out rules do not count) using each name."""
    patterns = {n: _series_pattern(n) for n in names}
    found: dict[str, list[Consumer]] = defaultdict(list)
    for path in _rule_files(root):
        try:
            doc = yaml.safe_load(path.read_text(encoding="utf-8"))
        except (yaml.YAMLError, UnicodeDecodeError):
            continue
        if not isinstance(doc, dict) or not isinstance(doc.get("groups"), list):
            continue
        text = path.read_text(encoding="utf-8").splitlines()
        for group in doc["groups"]:
            for rule in (group or {}).get("rules") or []:
                expr = rule.get("expr") if isinstance(rule, dict) else None
                if not isinstance(expr, str):
                    continue
                kind, label = ("alert", rule["alert"]) if "alert" in rule else ("record", rule.get("record", "?"))
                line = _line_of(text, kind, str(label))
                for name, pat in patterns.items():
                    if pat.search(expr):
                        found[name].append(Consumer(kind, str(label), _rel(path, root), line))
    return found


def scan_dashboard_consumers(root: Path, names: Iterable[str]) -> dict[str, list[Consumer]]:
    patterns = {n: _series_pattern(n) for n in names}
    found: dict[str, list[Consumer]] = defaultdict(list)
    for rel in DASHBOARD_DIRS:
        base = root / rel
        if not base.is_dir():
            continue
        for path in sorted(base.rglob("*.json")):
            text = path.read_text(encoding="utf-8", errors="replace")
            for name, pat in patterns.items():
                if pat.search(text):
                    found[name].append(Consumer("dashboard", path.stem, _rel(path, root), 1))
    return found


def _line_of(lines: list[str], kind: str, label: str) -> int:
    needle = re.compile(rf"^\s*-?\s*{kind}:\s*{re.escape(label)}\s*$")
    for i, line in enumerate(lines, start=1):
        if needle.match(line):
            return i
    return 1


def _rel(path: Path, root: Path) -> str:
    try:
        return path.resolve().relative_to(root.resolve()).as_posix()
    except ValueError:
        return path.as_posix()


def evaluate(root: Path) -> tuple[list[Finding], dict[str, Any]]:
    metrics_py = root / METRICS_RELPATH
    defs = parse_definitions(metrics_py)
    refs = scan_references(root / SRC_RELPATH, metrics_py, defs, root)

    unreferenced = sorted(a for a in defs if a not in refs)
    dynamic_only = sorted(a for a, r in refs.items() if r.attribute == 0 and a in defs)

    names = {defs[a].name for a in unreferenced}
    rule_users = scan_rule_consumers(root, names)
    dash_users = scan_dashboard_consumers(root, names)

    findings: list[Finding] = []
    rel_metrics = METRICS_RELPATH.as_posix()
    for attr in unreferenced:
        d = defs[attr]
        users = [*rule_users.get(d.name, []), *dash_users.get(d.name, [])]
        used_by = ", ".join(f"{u.kind} {u.name}" for u in users) or "no alert, rule or dashboard"
        findings.append(
            Finding(
                "unreferenced_metric", rel_metrics, d.line, f"{d.name} ({d.kind}): never touched; used by {used_by}"
            )
        )
        for u in rule_users.get(d.name, []):
            findings.append(
                Finding(
                    "alert_on_unreferenced_metric",
                    u.file,
                    u.line,
                    f"{u.kind} {u.name} uses {d.name}, which nothing in src/ ever updates",
                )
            )
    for attr in dynamic_only:
        d = defs[attr]
        r = refs[attr]
        findings.append(
            Finding(
                "dynamic_only_metric",
                rel_metrics,
                d.line,
                f"{d.name} ({d.kind}): only {r.dynamic} getattr and {r.string} string reference(s) in "
                f"{', '.join(sorted(r.files)[:3])}",
            )
        )
    summary = {"defined": len(defs), **{c: sum(1 for f in findings if f.category == c) for c in CATEGORIES}}
    return findings, summary


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n", 1)[0])
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--output-dir", type=Path, default=None, help="default: <root>/outputs/dead_scan")
    parser.add_argument(
        "--strict", action="store_true", help="exit 1 when any finding but a dynamic_only_metric is reported"
    )
    parser.add_argument(
        "--show-dynamic",
        action="store_true",
        help="also list dynamic_only_metric rows on the console (they are always in the JSON report)",
    )
    args = parser.parse_args(argv)

    root = args.root.resolve()
    findings, summary = evaluate(root)
    out_dir = (args.output_dir or root / "outputs" / "dead_scan").resolve()
    out_dir.mkdir(parents=True, exist_ok=True)
    report = out_dir / "dead_metrics_report.json"
    report.write_text(
        json.dumps({"summary": summary, "findings": [asdict(f) for f in findings]}, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )

    for category in CATEGORIES:
        rows = [f for f in findings if f.category == category]
        if not rows:
            continue
        print(f"== {category} ({len(rows)})")
        if category == "dynamic_only_metric" and not args.show_dynamic:
            print("  (informational; --show-dynamic lists them, the JSON report always does)")
            continue
        for f in rows:
            print(f"  {f.file}:{f.line}: {f.detail}")
    print(f"dead_metrics_scan: {summary}  report={_rel(report, root)}")
    actionable = [f for f in findings if f.category not in INFORMATIONAL]
    return 1 if args.strict and actionable else 0


if __name__ == "__main__":
    sys.exit(main())
