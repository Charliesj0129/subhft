#!/usr/bin/env python3
"""Report documentation claims that the code and config do not back.

Report mode by default (exit 0, findings listed). ``--strict`` exits 1 when anything is found.

Categories (a finding names one):
  cli_command   ``hft <group> <sub>`` that the argparse parser (``cli/_parser.py``) does not define
  cli_flag      ``--flag`` after a resolved ``hft`` command that the leaf parser does not accept
  make_target   ``make <target>`` that has no rule in the Makefile
  metric        PromQL-looking metric name in a doc that nothing in ``src/`` or the recording rules defines
  path          repo path in code formatting, or a relative markdown link, that does not exist
  runbook_url   alert annotation ``runbook_url`` that points at a missing file
  alert_metric  metric name in an alert ``expr`` that nothing defines

What it cannot see (so a clean report is not proof): metric names built at run time, flags set in a
different position than the command it follows, anchors inside links, prose that is not in code
formatting, and anything under a doc root it was not pointed at.
"""

from __future__ import annotations

import argparse
import ast
import json
import re
import shlex
import subprocess
import sys
from collections import Counter
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Iterable

CATEGORIES = ("cli_command", "cli_flag", "make_target", "metric", "path", "runbook_url", "alert_metric")

DEFAULT_DOC_ROOTS = ("docs/runbooks", "docs/operations")
PATH_ROOTS = ("scripts", "docs", "config", "src", "tests", "research", "rust_core", ".agent", ".claude", ".github")
# Runtime or generated locations that legitimately do not exist in a checkout.
PATH_IGNORED_PREFIXES = ("research/data/", "research/experiments/runs/", "docs/generated/")
METRIC_DEF_CALLS = {"Counter", "Gauge", "Histogram", "Summary", "Info", "Enum"}
METRIC_SUFFIXES = ("_total", "_bucket", "_sum", "_count", "_created")
EXTERNAL_METRIC_PREFIXES = (
    "node_",
    "process_",
    "python_",
    "go_",
    "container_",
    "scrape_",
    "prometheus_",
    "alertmanager_",
    "probe_",
    "ClickHouse",
)
EXTERNAL_METRICS = {"up", "ALERTS", "ALERTS_FOR_STATE"}
PROMQL_WORDS = {
    "and", "or", "unless", "by", "without", "on", "ignoring", "bool", "offset", "group_left", "group_right",
    "inf", "nan", "start", "end",
}  # fmt: skip

FENCE_RE = re.compile(r"^\s*(```|~~~)")
INLINE_RE = re.compile(r"`([^`\n]+)`")
LINK_RE = re.compile(r"(?<!!)\[[^\]\n]*\]\(([^)\s]+)(?:\s+\"[^\"]*\")?\)")
HFT_CMD_RE = re.compile(r"(?<![\w./-])hft(?=\s)(?P<rest>[^\n#|;&`]*)")
MAKE_CMD_RE = re.compile(r"(?<![\w./-])make(?=\s)(?P<rest>[^\n#|;&`]*)")
PATH_RE = re.compile(r"(?<![\w./-])((?:%s)/[A-Za-z0-9_.@/\-*{}<>$\[\]]+)" % "|".join(re.escape(r) for r in PATH_ROOTS))
SELECTOR_RE = re.compile(r"\b([a-z_][a-z0-9_]*)\{(?=\s*(?:[a-zA-Z_][a-zA-Z0-9_]*\s*(?:=~|!~|!=|=)|\}))")
RANGE_RE = re.compile(r"\b([a-z_][a-z0-9_]*)\s*\[\d+[smhdwy]\]")
HFT_METRIC_SPAN_RE = re.compile(r"^hft_[a-z0-9]+(?:_[a-z0-9]+)*$")
NOT_METRICS = {"hft_platform"}
MAKE_RULE_RE = re.compile(r"^([A-Za-z0-9_][A-Za-z0-9_./% -]*?)\s*:(?![=:])")
TEXTFILE_RE = re.compile(r"# (?:TYPE|HELP) ([a-zA-Z_][a-zA-Z0-9_]*)")
PROMQL_IDENT_RE = re.compile(r"[a-zA-Z_:][a-zA-Z0-9_:]*")
SKIPPED_FENCE_LANGS = {"logql", "loki"}
PLACEHOLDER_START = ("<", "$", "[", "{", "(", "'", '"', "…", "...", "%")


@dataclass(frozen=True, slots=True)
class Finding:
    category: str
    file: str
    line: int
    detail: str


# --------------------------------------------------------------------------------------- doc scanning


def iter_code_spans(text: str) -> Iterable[tuple[int, str]]:
    """Yield ``(line_number, code)`` for each fenced-block line and each inline backtick span.

    LogQL fences are skipped: ``| json [1h]`` has the shape of a PromQL range selector but names no metric.
    """
    fence: str | None = None
    skipped = False
    for lineno, line in enumerate(text.splitlines(), start=1):
        m = FENCE_RE.match(line)
        if m:
            if fence is None:
                fence = m.group(1)
                skipped = line.strip().lstrip("`~").strip().lower() in SKIPPED_FENCE_LANGS
            elif m.group(1) == fence:
                fence = None
                skipped = False
            continue
        if fence is not None:
            if not skipped:
                yield lineno, line
        else:
            for im in INLINE_RE.finditer(line):
                yield lineno, im.group(1)


def iter_prose_links(text: str) -> Iterable[tuple[int, str]]:
    """Yield ``(line_number, target)`` for markdown links outside fenced blocks."""
    fenced = False
    for lineno, line in enumerate(text.splitlines(), start=1):
        if FENCE_RE.match(line):
            fenced = not fenced
            continue
        if fenced:
            continue
        stripped = INLINE_RE.sub("", line)
        for m in LINK_RE.finditer(stripped):
            yield lineno, m.group(1)


# ------------------------------------------------------------------------------------------- CLI check


def _subparsers(parser: argparse.ArgumentParser) -> dict[str, argparse.ArgumentParser]:
    for action in parser._actions:
        if isinstance(action, argparse._SubParsersAction):
            return dict(action.choices)
    return {}


def _accepts_flag(parser: argparse.ArgumentParser, flag: str) -> bool:
    if flag in ("--help", "-h"):
        return True
    options = [o for o in parser._option_string_actions if o.startswith("--")]
    if flag in options:
        return True
    return any(o.startswith(flag) for o in options)  # argparse accepts unambiguous prefixes


def check_cli_line(parser: argparse.ArgumentParser, rest: str) -> list[tuple[str, str]]:
    """Return ``[(category, detail)]`` for one ``hft ...`` command tail."""
    try:
        tokens = shlex.split(rest, posix=True)
    except ValueError:
        tokens = rest.split()
    out: list[tuple[str, str]] = []
    path = ["hft"]
    current = parser
    descending = True
    skip_value = False
    for tok in tokens:
        if skip_value:
            skip_value = False
            continue
        if tok.startswith("-"):
            flag, has_value, _ = tok.partition("=")
            if flag.startswith("--"):
                if not _accepts_flag(current, flag):
                    out.append(("cli_flag", f"`{' '.join(path)}` has no option {flag}"))
                else:
                    action = current._option_string_actions.get(flag)
                    skip_value = bool(action is not None and action.nargs != 0 and not has_value)
            continue
        if not descending or tok.startswith(PLACEHOLDER_START):
            descending = False
            continue
        subs = _subparsers(current)
        if tok in subs:
            current = subs[tok]
            path.append(tok)
        else:
            if subs and re.fullmatch(r"[a-z][a-z0-9-]*", tok):
                known = ", ".join(sorted(subs))
                out.append(("cli_command", f"`{' '.join(path)}` has no subcommand `{tok}` (defined: {known})"))
                break
            descending = False
    return out


def check_cli(spans: Iterable[tuple[int, str]], parser: argparse.ArgumentParser, rel: str) -> list[Finding]:
    found: list[Finding] = []
    for lineno, code in spans:
        for m in HFT_CMD_RE.finditer(code):
            for category, detail in check_cli_line(parser, m.group("rest")):
                found.append(Finding(category, rel, lineno, detail))
    return found


# ------------------------------------------------------------------------------------------ make check


def makefile_targets(makefile_text: str) -> set[str]:
    targets: set[str] = set()
    for line in makefile_text.splitlines():
        if line.startswith((".", "\t", " ", "#")):
            continue
        m = MAKE_RULE_RE.match(line)
        if m:
            targets.update(m.group(1).split())
    return targets


def check_make(spans: Iterable[tuple[int, str]], targets: set[str], rel: str) -> list[Finding]:
    found: list[Finding] = []
    for lineno, code in spans:
        for m in MAKE_CMD_RE.finditer(code):
            try:
                tokens = shlex.split(m.group("rest"), posix=True)
            except ValueError:
                tokens = m.group("rest").split()
            for tok in tokens:
                if tok.startswith("-") or "=" in tok:
                    continue
                if tok.startswith(PLACEHOLDER_START) or not re.fullmatch(r"[a-z][a-z0-9_-]*", tok):
                    break
                if tok not in targets:
                    found.append(Finding("make_target", rel, lineno, f"`make {tok}` has no rule in the Makefile"))
                break
    return found


# ---------------------------------------------------------------------------------------- metric check


def _strip_suffix(name: str) -> str:
    for suffix in METRIC_SUFFIXES:
        if name.endswith(suffix):
            return name[: -len(suffix)]
    return name


def collect_defined_metrics(root: Path) -> set[str]:
    """Names from ``_pn("x")`` / ``Counter("x", ...)`` string literals in ``src/`` plus recording-rule names."""
    names: set[str] = set()
    for py in sorted((root / "src").rglob("*.py")):
        try:
            tree = ast.parse(py.read_text(encoding="utf-8", errors="replace"))
        except SyntaxError:
            continue
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call) or not node.args:
                continue
            func = node.func
            fname = func.id if isinstance(func, ast.Name) else func.attr if isinstance(func, ast.Attribute) else ""
            first = node.args[0]
            if (
                fname in ({"_pn"} | METRIC_DEF_CALLS)
                and isinstance(first, ast.Constant)
                and isinstance(first.value, str)
            ):
                names.add(first.value)
    scripts_dir = root / "scripts"
    if scripts_dir.is_dir():
        for script in sorted(scripts_dir.rglob("*")):
            if script.suffix in (".sh", ".py") and script.is_file():
                names.update(TEXTFILE_RE.findall(script.read_text(encoding="utf-8", errors="replace")))
    rec_dir = root / "config" / "monitoring" / "recording_rules"
    if rec_dir.is_dir():
        for path in sorted(rec_dir.glob("*.y*ml")):
            for rec in _yaml_values(path, "record"):
                names.add(rec)
    return names


def _yaml_values(path: Path, key: str) -> list[str]:
    import yaml

    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError):
        return []
    out: list[str] = []

    def walk(node: Any) -> None:
        if isinstance(node, dict):
            for k, v in node.items():
                if k == key and isinstance(v, str):
                    out.append(v)
                else:
                    walk(v)
        elif isinstance(node, list):
            for item in node:
                walk(item)

    walk(data)
    return out


def metric_known(name: str, defined_stripped: set[str]) -> bool:
    if name in EXTERNAL_METRICS or name.startswith(EXTERNAL_METRIC_PREFIXES) or ":" in name:
        return True
    return _strip_suffix(name) in defined_stripped


def _metric_message(name: str, defined_stripped: set[str]) -> str:
    base = _strip_suffix(name)
    if base.startswith("hft_") and base[4:] in defined_stripped:
        return f"`{name}`: defined without the `hft_` prefix (HFT_METRICS_PREFIX defaults to empty), as `{name[4:]}`"
    return f"`{name}` is not defined by any `_pn(...)`, Counter/Gauge/Histogram or recording rule"


def check_metrics(spans: Iterable[tuple[int, str]], defined: set[str], rel: str) -> list[Finding]:
    stripped = {_strip_suffix(n) for n in defined}
    found: list[Finding] = []
    seen: set[tuple[int, str]] = set()
    for lineno, code in spans:
        names = {m.group(1) for m in SELECTOR_RE.finditer(code)} | {m.group(1) for m in RANGE_RE.finditer(code)}
        if HFT_METRIC_SPAN_RE.match(code.strip()):
            names.add(code.strip())
        for name in sorted(names):
            if name in PROMQL_WORDS or name in NOT_METRICS or (lineno, name) in seen:
                continue
            seen.add((lineno, name))
            if not metric_known(name, stripped):
                found.append(Finding("metric", rel, lineno, _metric_message(name, stripped)))
    return found


# ------------------------------------------------------------------------------------------ path check


def _clean_path(raw: str) -> str:
    raw = raw.rstrip(".,:;)'\"")
    return re.split(r"[#:]", raw, maxsplit=1)[0]


def git_ignored(root: Path, paths: Iterable[str]) -> set[str]:
    """Paths git ignores (generated or local-only files). Names only: no file is opened. Empty if git is unusable."""
    wanted = sorted(set(paths))
    if not wanted:
        return set()
    try:
        proc = subprocess.run(
            ["git", "-C", str(root), "check-ignore", "--stdin"],
            input="\n".join(wanted),
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return set()
    return set(proc.stdout.splitlines())


def check_paths(spans: Iterable[tuple[int, str]], root: Path, rel: str) -> list[Finding]:
    missing: list[tuple[int, str]] = []
    for lineno, code in spans:
        for m in PATH_RE.finditer(code):
            cand = _clean_path(m.group(1))
            if not cand or any(c in cand for c in "*{}<>$[]") or cand.endswith("/"):
                continue
            if cand.startswith(PATH_IGNORED_PREFIXES):
                continue
            if not (root / cand).exists():
                missing.append((lineno, cand))
    ignored = git_ignored(root, (c for _, c in missing))
    return [Finding("path", rel, ln, f"`{c}` does not exist") for ln, c in missing if c not in ignored]


def check_links(links: Iterable[tuple[int, str]], doc: Path, root: Path, rel: str) -> list[Finding]:
    found: list[Finding] = []
    for lineno, target in links:
        if re.match(r"^[a-zA-Z][a-zA-Z0-9+.-]*:", target) or target.startswith("#"):
            continue
        file_part = target.split("#", 1)[0]
        if not file_part:
            continue
        base = root if file_part.startswith("/") else doc.parent
        resolved = (base / file_part.lstrip("/")).resolve()
        if not resolved.exists():
            found.append(Finding("path", rel, lineno, f"link target `{target}` does not exist"))
    return found


# ----------------------------------------------------------------------------------------- alert check


def _promql_metric_names(expr: str) -> set[str]:
    text = re.sub(r'"(?:[^"\\]|\\.)*"', '""', expr)
    text = re.sub(r"\{[^{}]*\}", "", text)
    text = re.sub(r"\b(?:by|without|on|ignoring|group_left|group_right)\s*\([^)]*\)", " ", text)
    text = re.sub(r"\[[^\]]*\]", " ", text)
    names: set[str] = set()
    for m in PROMQL_IDENT_RE.finditer(text):
        name = m.group(0)
        tail = text[m.end() :].lstrip()
        before = text[: m.start()]
        if tail.startswith("(") or name in PROMQL_WORDS or re.search(r"[\d.]$", before) or name[0].isdigit():
            continue
        names.add(name)
    return names


def _collect_rules(node: Any, rules: list[dict[str, Any]]) -> None:
    if isinstance(node, dict):
        if "alert" in node and "expr" in node:
            rules.append(node)
        for value in node.values():
            _collect_rules(value, rules)
    elif isinstance(node, list):
        for item in node:
            _collect_rules(item, rules)


def _check_rule(rule: dict[str, Any], rel: str, root: Path, stripped: set[str]) -> list[Finding]:
    found: list[Finding] = []
    alert = str(rule.get("alert"))
    url = (rule.get("annotations") or {}).get("runbook_url")
    if isinstance(url, str) and not url.startswith(("http://", "https://")):
        target = url.split("#", 1)[0]
        if target and not (root / target).exists():
            found.append(Finding("runbook_url", rel, 0, f"alert {alert}: runbook_url `{url}` does not exist"))
    for name in sorted(_promql_metric_names(str(rule.get("expr")))):
        if not metric_known(name, stripped):
            found.append(Finding("alert_metric", rel, 0, f"alert {alert}: {_metric_message(name, stripped)}"))
    return found


def check_alert_files(root: Path, defined: set[str]) -> list[Finding]:
    import yaml

    stripped = {_strip_suffix(n) for n in defined}
    found: list[Finding] = []
    alerts_dir = root / "config" / "monitoring" / "alerts"
    if not alerts_dir.is_dir():
        return found
    for path in sorted(alerts_dir.glob("*.y*ml")):
        if path.name.startswith(("alertmanager", "rules_test")):
            continue
        try:
            data = yaml.safe_load(path.read_text(encoding="utf-8"))
        except (OSError, yaml.YAMLError):
            continue
        rules: list[dict[str, Any]] = []
        _collect_rules(data, rules)
        for rule in rules:
            found += _check_rule(rule, str(path.relative_to(root)), root, stripped)
    return found


# ------------------------------------------------------------------------------------------------ run


def collect_docs(root: Path, doc_roots: Iterable[str]) -> list[Path]:
    files: list[Path] = []
    for entry in doc_roots:
        base = root / entry
        if base.is_file():
            files.append(base)
        elif base.is_dir():
            files.extend(sorted(base.rglob("*.md")))
    return files


def run_checks(
    root: Path,
    doc_roots: Iterable[str] = DEFAULT_DOC_ROOTS,
    parser: argparse.ArgumentParser | None = None,
    include_alerts: bool = True,
) -> list[Finding]:
    if parser is None:
        from hft_platform.cli._parser import build_parser

        parser = build_parser()
    makefile = root / "Makefile"
    targets = makefile_targets(makefile.read_text(encoding="utf-8")) if makefile.is_file() else set()
    defined = collect_defined_metrics(root)
    findings: list[Finding] = []
    for doc in collect_docs(root, doc_roots):
        rel = str(doc.relative_to(root))
        text = doc.read_text(encoding="utf-8", errors="replace")
        spans = list(iter_code_spans(text))
        findings += check_cli(spans, parser, rel)
        findings += check_make(spans, targets, rel)
        findings += check_metrics(spans, defined, rel)
        findings += check_paths(spans, root, rel)
        findings += check_links(iter_prose_links(text), doc, root, rel)
    if include_alerts:
        findings += check_alert_files(root, defined)
    return findings


def summarize(findings: list[Finding]) -> dict[str, int]:
    counts = Counter(f.category for f in findings)
    return {c: counts.get(c, 0) for c in CATEGORIES}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--root", default=".", help="repository root")
    ap.add_argument("--docs", nargs="*", default=list(DEFAULT_DOC_ROOTS), help="doc roots, relative to --root")
    ap.add_argument("--output-dir", default="outputs/docs_contract", help="JSON report directory ('' to skip)")
    ap.add_argument("--limit", type=int, default=40, help="findings to print per category (0 = all)")
    ap.add_argument("--strict", action="store_true", help="exit 1 when any finding exists")
    args = ap.parse_args(argv)

    root = Path(args.root).resolve()
    findings = run_checks(root, args.docs)
    counts = summarize(findings)

    print(f"docs-contract-check: {len(findings)} finding(s) in {', '.join(args.docs)}")
    for category in CATEGORIES:
        rows = [f for f in findings if f.category == category]
        print(f"\n[{category}] {len(rows)}")
        for f in rows if args.limit == 0 else rows[: args.limit]:
            where = f"{f.file}:{f.line}" if f.line else f.file
            print(f"  {where}: {f.detail}")
        if args.limit and len(rows) > args.limit:
            print(f"  ... {len(rows) - args.limit} more (use --limit 0 or the JSON report)")

    if args.output_dir:
        out = Path(args.output_dir)
        if not out.is_absolute():
            out = root / out
        out.mkdir(parents=True, exist_ok=True)
        payload = {"counts": counts, "total": len(findings), "findings": [asdict(f) for f in findings]}
        (out / "docs_contract_report.json").write_text(
            json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
        )
    return 1 if args.strict and findings else 0


if __name__ == "__main__":
    sys.exit(main())
