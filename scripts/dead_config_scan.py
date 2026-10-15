#!/usr/bin/env python3
"""Report configuration that nothing reads, loads or evaluates.

Report mode by default (exit 0, findings listed). ``--strict`` exits 1 when anything is found.

Categories (a finding names one):
  dead_config_key         a leaf key in ``config/env/*/*.yaml`` that no string literal, attribute name,
                          annotated field or keyword in ``src/hft_platform`` ever spells; the value is
                          set by an operator and silently ignored. A key read through a prefix constant
                          (``"max_price_cap_root_" + root``) counts as read
  unreferenced_config_file  a tracked file under ``config/`` that no source, script, build, deploy or
                          other config file names (by path, by ``parent/name`` or, when unique, by name)
  rule_file_not_loaded    a Prometheus rule file (a YAML with ``groups:``) that ``prometheus.yml``
                          ``rule_files`` does not match, or whose directory the compose file does not mount
                          into the Prometheus container: its alerts and recording rules never run

What it cannot see (so a clean report is not proof): a key read by a name built at run time with no
literal prefix, a file loaded through a path assembled from environment variables, a key whose name is
common enough to be spelled somewhere unrelated, and a rule file loaded by a Prometheus other than the
one the compose files start. ``config/monitoring/`` (other than rule files), ``config/agent_system/`` and
``config/symbols.examples/`` are not checked for unreferenced files: dashboards and provisioning are
mounted as directories, agent prompts are not platform runtime, and the examples are copied by hand.
"""

from __future__ import annotations

import argparse
import ast
import fnmatch
import json
import re
import subprocess
import sys
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Iterable

import yaml

CATEGORIES = ("dead_config_key", "unreferenced_config_file", "rule_file_not_loaded")
ENV_YAML_GLOB = "config/env/*/*.yaml"
SRC_RELPATH = Path("src/hft_platform")
UNREFERENCED_EXCLUDED_PREFIXES = ("config/monitoring/", "config/agent_system/", "config/symbols.examples/")
READER_ROOTS = ("src", "scripts", "research", "tests", ".github", "config")
READER_FILES = ("Makefile", "Dockerfile", "pyproject.toml")
CODE_PREFIXES = ("src/", "scripts/", "research/", "tests/")
READER_SUFFIXES = {".py", ".sh", ".yml", ".yaml", ".toml", ".json", ".env", ".sql", ".tpl", ".rs", ""}
PROMETHEUS_YML = "config/monitoring/prometheus.yml"
RULE_DIRS = ("config/monitoring/alerts", "config/monitoring/recording_rules")
PATH_TOKEN_RE = re.compile(r"[A-Za-z0-9_./{}*$()-]*\.(?:ya?ml|json|env|list|sql|sh|tpl|toml)\b")


@dataclass(frozen=True, slots=True)
class Finding:
    category: str
    file: str
    line: int
    detail: str


# ---------------------------------------------------------------------------------------- dead keys


def spelled_names(src_root: Path) -> tuple[set[str], set[str]]:
    """Return ``(names, prefixes)``: every identifier or string the code spells, and string prefixes.

    ``names`` holds string constants, attribute names, annotated class fields, keyword names and function
    parameters. ``prefixes`` holds the string constants that end in ``_`` (a key family read as
    ``prefix + suffix``).
    """
    names: set[str] = set()
    prefixes: set[str] = set()
    for path in sorted(src_root.rglob("*.py")):
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except (SyntaxError, UnicodeDecodeError):
            continue
        for node in ast.walk(tree):
            if isinstance(node, ast.Constant) and isinstance(node.value, str):
                names.add(node.value)
                if node.value.endswith("_") and len(node.value) > 3:
                    prefixes.add(node.value)
            elif isinstance(node, ast.Attribute):
                names.add(node.attr)
            elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
                names.add(node.target.id)
            elif isinstance(node, ast.keyword) and node.arg:
                names.add(node.arg)
            elif isinstance(node, ast.arg):
                names.add(node.arg)
    return names, prefixes


def iter_leaf_keys(node: yaml.Node, path: tuple[str, ...] = ()) -> Iterable[tuple[tuple[str, ...], int]]:
    """Yield ``(key_path, line)`` for each leaf of a YAML node tree (lists of mappings are entered)."""
    if isinstance(node, yaml.MappingNode):
        for key_node, value_node in node.value:
            key = str(getattr(key_node, "value", key_node))
            child = (*path, key)
            if isinstance(value_node, yaml.MappingNode):
                yield from iter_leaf_keys(value_node, child)
            elif isinstance(value_node, yaml.SequenceNode) and any(
                isinstance(item, yaml.MappingNode) for item in value_node.value
            ):
                for item in value_node.value:
                    yield from iter_leaf_keys(item, child)
            else:
                yield child, key_node.start_mark.line + 1
    elif isinstance(node, yaml.SequenceNode):
        for item in node.value:
            yield from iter_leaf_keys(item, path)


def find_dead_keys(root: Path, yaml_glob: str = ENV_YAML_GLOB) -> list[Finding]:
    names, prefixes = spelled_names(root / SRC_RELPATH)
    findings: list[Finding] = []
    for path in sorted(root.glob(yaml_glob)):
        try:
            doc = yaml.compose(path.read_text(encoding="utf-8"))
        except (yaml.YAMLError, UnicodeDecodeError):
            continue
        if doc is None:
            continue
        for key_path, line in iter_leaf_keys(doc):
            key = key_path[-1]
            if key in names or any(key.startswith(p) for p in prefixes):
                continue
            findings.append(
                Finding(
                    "dead_config_key",
                    _rel(path, root),
                    line,
                    f"{'.'.join(key_path)}: nothing in src/ spells '{key}'",
                )
            )
    return findings


# ----------------------------------------------------------------------------- unreferenced files


def _tracked_config_files(root: Path) -> list[str]:
    try:
        out = subprocess.run(
            ["git", "-C", str(root), "ls-files", "config"], capture_output=True, text=True, check=True
        ).stdout.split("\n")
        files = [f for f in out if f]
        if files:
            return sorted(files)
    except (OSError, subprocess.CalledProcessError):
        pass
    base = root / "config"
    return sorted(_rel(p, root) for p in base.rglob("*") if p.is_file()) if base.is_dir() else []


def _reader_texts(root: Path) -> dict[str, str]:
    texts: dict[str, str] = {}

    def add(path: Path) -> None:
        if path.suffix in READER_SUFFIXES and path.is_file():
            try:
                texts[_rel(path, root)] = path.read_text(encoding="utf-8", errors="replace")
            except OSError:
                pass

    for name in READER_ROOTS:
        base = root / name
        if base.is_dir():
            for p in base.rglob("*"):
                if ".venv" not in p.parts and "node_modules" not in p.parts:
                    add(p)
    for name in READER_FILES:
        add(root / name)
    for p in root.glob("docker-compose*.yml"):
        add(p)
    return texts


def _is_code(rel: str) -> bool:
    return rel.startswith(CODE_PREFIXES) or rel in READER_FILES or rel.startswith("docker-compose")


Tokens = tuple[dict[str, set[str]], dict[str, set[str]], dict[str, set[str]]]


def _path_tokens(texts: dict[str, str]) -> Tokens:
    """Return ``(slash, bare, wild)``: path-like tokens found in the reader files, each with the files naming it.

    ``slash`` tokens (``env/prod/main.yaml``, ``/etc/prometheus/alerts/rules.yaml``) count from any reader.
    ``bare`` names (``risk.yaml``) and ``wild`` globs (``config/env/*/*.yaml``) count only from code and build
    files: config files and CI path filters name each other in comments and over-broad globs, and that is not
    loading. A glob needs at least two literal path segments, so ``**/*.yaml`` loads nothing in particular.
    """
    slash: dict[str, set[str]] = {}
    bare: dict[str, set[str]] = {}
    wild: dict[str, set[str]] = {}
    for rel, text in texts.items():
        code = _is_code(rel)
        for m in PATH_TOKEN_RE.finditer(text):
            tok = re.sub(r"\{[^}]*\}|\$\([^)]*\)|\$\{[^}]*\}|\$[A-Za-z_]+", "*", m.group(0)).lstrip("./").lstrip("/")
            if not tok:
                continue
            if "*" in tok:
                literal = [seg for seg in tok.split("/") if seg and "*" not in seg]
                if code and len(literal) >= 2:
                    wild.setdefault(tok, set()).add(rel)
            elif "/" in tok:
                slash.setdefault(tok, set()).add(rel)
            elif code:
                bare.setdefault(tok, set()).add(rel)
    return slash, bare, wild


def find_unreferenced_files(root: Path) -> list[Finding]:
    all_config = _tracked_config_files(root)
    files = [f for f in all_config if not f.startswith(UNREFERENCED_EXCLUDED_PREFIXES)]
    base_counts: dict[str, int] = {}
    for f in all_config:
        base_counts[Path(f).name] = base_counts.get(Path(f).name, 0) + 1
    tokens = _path_tokens(_reader_texts(root))
    return [
        Finding("unreferenced_config_file", rel, 1, "no source, script, build, deploy or config file names it")
        for rel in files
        if not _is_referenced(rel, tokens, base_counts)
    ]


def _named_elsewhere(readers: set[str], rel: str) -> bool:
    """A file naming itself (a header comment) is not a reader of itself."""
    return bool(readers - {rel})


def _is_referenced(rel: str, tokens: Tokens, base_counts: dict[str, int]) -> bool:
    slash, bare, wild = tokens
    base = Path(rel).name
    tail = "/".join(rel.split("/")[-2:])
    for tok, readers in wild.items():
        if (fnmatch.fnmatchcase(rel, tok) or fnmatch.fnmatchcase(rel, "*/" + tok)) and _named_elsewhere(readers, rel):
            return True
    for tok, readers in slash.items():
        if not _named_elsewhere(readers, rel):
            continue
        if rel == tok or ("/" + rel).endswith("/" + tok):
            return True  # a full or partial path: ``env/prod/main.yaml``
        if tok.endswith("/" + tail):
            return True  # a container path whose last two parts match: ``/etc/prometheus/alerts/rules.yaml``
    # a bare name, only when no other config file shares it
    return base_counts.get(base, 0) == 1 and _named_elsewhere(bare.get(base, set()), rel)


# ---------------------------------------------------------------------------- Prometheus rule files


def _compose_mounts(service: dict[str, Any]) -> list[tuple[str, str]]:
    mounts: list[tuple[str, str]] = []
    for vol in service.get("volumes") or []:
        if isinstance(vol, str):
            parts = vol.split(":")
            if len(parts) >= 2:
                mounts.append((parts[0].removeprefix("./"), parts[1]))
        elif isinstance(vol, dict) and "source" in vol and "target" in vol:
            mounts.append((str(vol["source"]).removeprefix("./"), str(vol["target"])))
    return mounts


def _rule_file_globs(root: Path) -> list[str]:
    path = root / PROMETHEUS_YML
    if not path.is_file():
        return []
    doc = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    return [str(g) for g in doc.get("rule_files") or []]


def _host_globs(container_globs: list[str], mounts: list[tuple[str, str]]) -> list[str]:
    out: list[str] = []
    for glob in container_globs:
        for src, tgt in mounts:
            if glob == tgt:
                out.append(src)
            elif glob.startswith(tgt.rstrip("/") + "/"):
                out.append(src.rstrip("/") + glob[len(tgt.rstrip("/")) :])
    return out


def _rule_files(root: Path) -> list[tuple[str, int]]:
    """Return ``(relative path, rule count)`` for every YAML with a ``groups`` list under the rule dirs."""
    out: list[tuple[str, int]] = []
    for rel in RULE_DIRS:
        base = root / rel
        if not base.is_dir():
            continue
        for path in sorted(p for p in base.iterdir() if p.suffix in (".yml", ".yaml")):
            try:
                doc = yaml.safe_load(path.read_text(encoding="utf-8"))
            except (yaml.YAMLError, UnicodeDecodeError):
                continue
            if isinstance(doc, dict) and isinstance(doc.get("groups"), list):
                count = sum(len((g or {}).get("rules") or []) for g in doc["groups"])
                out.append((_rel(path, root), count))
    return out


def find_unloaded_rule_files(root: Path) -> list[Finding]:
    rule_files = _rule_files(root)
    if not rule_files:
        return []
    container_globs = _rule_file_globs(root)
    findings: list[Finding] = []
    for compose in sorted(root.glob("docker-compose*.yml")):
        try:
            doc = yaml.safe_load(compose.read_text(encoding="utf-8")) or {}
        except yaml.YAMLError:
            continue
        for name, service in (doc.get("services") or {}).items():
            mounts = _compose_mounts(service or {})
            if not any(src == PROMETHEUS_YML for src, _ in mounts):
                continue
            host_globs = _host_globs(container_globs, mounts)
            for rel, count in rule_files:
                if any(fnmatch.fnmatchcase(rel, g) for g in host_globs):
                    continue
                parent = rel.rsplit("/", 1)[0]
                mounted = any(src.rstrip("/") == parent or rel.startswith(src.rstrip("/") + "/") for src, _ in mounts)
                why = "no rule_files entry matches it" if mounted else f"{parent} is not mounted into the container"
                findings.append(
                    Finding(
                        "rule_file_not_loaded",
                        rel,
                        1,
                        f"{count} rule(s) never run under {compose.name} service {name}: {why}",
                    )
                )
    return findings


# --------------------------------------------------------------------------------------------- main


def _rel(path: Path, root: Path) -> str:
    try:
        return path.resolve().relative_to(root.resolve()).as_posix()
    except ValueError:
        return path.as_posix()


def evaluate(root: Path, yaml_glob: str = ENV_YAML_GLOB) -> tuple[list[Finding], dict[str, int]]:
    findings = [
        *find_dead_keys(root, yaml_glob),
        *find_unreferenced_files(root),
        *find_unloaded_rule_files(root),
    ]
    return findings, {c: sum(1 for f in findings if f.category == c) for c in CATEGORIES}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n", 1)[0])
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--yaml-glob", default=ENV_YAML_GLOB, help="config files whose keys are checked")
    parser.add_argument("--output-dir", type=Path, default=None, help="default: <root>/outputs/dead_scan")
    parser.add_argument("--strict", action="store_true", help="exit 1 when any finding is reported")
    args = parser.parse_args(argv)

    root = args.root.resolve()
    findings, summary = evaluate(root, args.yaml_glob)
    out_dir = (args.output_dir or root / "outputs" / "dead_scan").resolve()
    out_dir.mkdir(parents=True, exist_ok=True)
    report = out_dir / "dead_config_report.json"
    report.write_text(
        json.dumps({"summary": summary, "findings": [asdict(f) for f in findings]}, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    for category in CATEGORIES:
        rows = [f for f in findings if f.category == category]
        if rows:
            print(f"== {category} ({len(rows)})")
            for f in rows:
                print(f"  {f.file}:{f.line}: {f.detail}")
    print(f"dead_config_scan: {summary}  report={_rel(report, root)}")
    return 1 if args.strict and findings else 0


if __name__ == "__main__":
    sys.exit(main())
