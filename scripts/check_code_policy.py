from __future__ import annotations

import argparse
import ast
import io
import re
import subprocess
import sys
import tokenize
import tomllib
from collections import Counter
from pathlib import Path

import tree_sitter_rust
import yaml
from tree_sitter import Language, Parser

from .check_discipline import check_pytest_in_sys_modules

ROOT = Path(__file__).resolve().parents[1]
RUST = Language(tree_sitter_rust.language())
FLOAT_POLICY = "scripts/float_gate_exemptions.yaml"
IMMUTABLE = ("research/experiments/", "research/archive/", "tests/golden/")
PROTECTED_DIRECTORIES = ("src/hft_platform/contracts/",)
PROTECTED_FILES = {
    "scripts/check_discipline.py",
    "src/hft_platform/core/pricing.py",
    "src/hft_platform/core/timebase.py",
    "src/hft_platform/events.py",
}
TOOL_DIRECTIVE = re.compile(
    r"^#\s*(?:noqa\b|type\s*:|pyright\s*:|mypy\s*:|pylint\s*:|"
    r"pragma\s*:\s*no\s+(?:branch|cover)\b|fmt\s*:|isort\s*:|"
    r"ruff\s*:|nosec\b|precision-ok\b|nosemgrep\b|semgrep\s*:)",
    re.IGNORECASE,
)
SHEBANG = re.compile(r"^#!\s*(?:/usr/bin/env\s+python3?|/usr/bin/python3?)$")
ENCODING = re.compile(r"^#\s*(?:-\*-\s*)?coding[:=]\s*[-\w.]+\s*(?:-\*-)?\s*$")
RUFF_RESTRICTIONS = ("select", "extend-select")
RUFF_SUPPRESSIONS = ("ignore", "extend-ignore", "per-file-ignores", "extend-per-file-ignores", "exclude")


def git(*args: str) -> bytes:
    return subprocess.check_output(["git", *args], cwd=ROOT, stderr=subprocess.PIPE)


def git_paths(*args: str) -> set[str]:
    return {path.decode() for path in git(*args).split(b"\0") if path}


def is_protected(path: str) -> bool:
    return path in PROTECTED_FILES or path.startswith(PROTECTED_DIRECTORIES)


def comments(source: bytes):
    for token in tokenize.tokenize(io.BytesIO(source).readline):
        if token.type != tokenize.COMMENT:
            continue
        line, column = token.start
        if (line == 1 and SHEBANG.fullmatch(token.string)) or (line <= 2 and ENCODING.match(token.string)):
            continue
        if TOOL_DIRECTIVE.match(token.string):
            kind = "directive"
        else:
            kind = "prose"
        yield line, kind, token.line[:column].strip(), token.string


def runtime_pytest_uses(path: str, source: bytes):
    tree = ast.parse(source, filename=path)
    violations = check_pytest_in_sys_modules(tree, Path(path))
    lines = source.splitlines()
    for violation in violations:
        yield violation, lines[violation.line - 1].strip()


def check_python(path: str, current: bytes, previous: bytes):
    protected = is_protected(path)
    existing = Counter((kind, code, comment) for _, kind, code, comment in comments(previous))
    for line, kind, code, comment in comments(current):
        key = (kind, code, comment)
        if (kind == "directive" or protected) and existing[key]:
            existing[key] -= 1
        elif kind == "directive":
            yield f"{path}:{line}: HFT-D005 remove the inline tool directive and fix the underlying violation"
        else:
            yield f"{path}:{line}: HFT-D004 express intent in code or move the rationale to documentation"

    if path.startswith("src/hft_platform/") and current != previous:
        existing_pytest_uses = Counter(code for _, code in runtime_pytest_uses(path, previous))
        for violation, code in runtime_pytest_uses(path, current):
            if existing_pytest_uses[code]:
                existing_pytest_uses[code] -= 1
            else:
                yield f"{path}:{violation.line}: HFT-D002 remove runtime pytest detection from production code"


def walk_rust(node):
    pending = [node]
    while pending:
        node = pending.pop()
        yield node
        pending.extend(reversed(node.named_children))


def rust_nodes(path: str, source: bytes):
    root = Parser(RUST).parse(source).root_node
    if root.has_error:
        raise ValueError(f"invalid Rust syntax: {path}")
    return walk_rust(root)


def rust_suppression(node) -> bool:
    if node.type != "attribute" or not node.named_children:
        return False
    name = node.named_children[0].text
    if name in (b"allow", b"expect"):
        return True
    if name != b"cfg_attr":
        return False
    arguments = node.child_by_field_name("arguments")
    if arguments is None:
        return False
    return any(
        child.type == "identifier"
        and child.text in (b"allow", b"expect")
        and child.next_named_sibling is not None
        and child.next_named_sibling.type == "token_tree"
        for child in walk_rust(arguments)
    )


def check_rust(path: str, current: bytes, previous: bytes):
    if current != previous:
        existing = Counter(node.text for node in rust_nodes(path, previous) if rust_suppression(node))
    else:
        existing = None
    for node in rust_nodes(path, current):
        line = node.start_point.row + 1
        if node.type in {"line_comment", "block_comment"} and node.child_by_field_name("doc") is None:
            yield f"{path}:{line}: HFT-D004 express intent in code or move the rationale to documentation"
        if existing is not None and rust_suppression(node):
            if existing[node.text]:
                existing[node.text] -= 1
            else:
                yield f"{path}:{line}: HFT-D005 remove the Rust lint attribute and fix the underlying violation"


def config_values(values: dict, keys: tuple[str, ...]):
    for key in keys:
        value = values.get(key, [])
        if isinstance(value, dict):
            for pattern, rules in value.items():
                for rule in rules:
                    yield f"{key}[{pattern}] = {rule}"
        elif isinstance(value, list):
            for item in value:
                yield f"{key} = {item}"
        else:
            yield f"{key} = {value}"


def ruff_settings(path: str, source: bytes):
    config = tomllib.loads(source.decode())
    ruff = (
        config.get("tool", {}).get("ruff", {})
        if path.endswith("/pyproject.toml") or path == "pyproject.toml"
        else config
    )
    lint = ruff.get("lint", {})
    suppressed = {
        *config_values(ruff, ("exclude", "extend-exclude")),
        *config_values(lint, RUFF_SUPPRESSIONS),
    }
    if "extend" in ruff:
        suppressed.add(f"extend = {ruff['extend']}")
    selected = set(config_values(lint, RUFF_RESTRICTIONS))
    return suppressed, selected


def python_check_suppressions(source: bytes):
    tool = tomllib.loads(source.decode()).get("tool", {})
    mypy = tool.get("mypy", {})
    for scope in [mypy, *mypy.get("overrides", [])]:
        module = scope.get("module", "global")
        settings = set(config_values(scope, ("disable_error_code", "exclude")))
        for key in ("ignore_errors", "ignore_missing_imports", "exclude_gitignore"):
            if scope.get(key, False):
                settings.add(key)
        if scope.get("follow_imports") in {"skip", "silent"}:
            settings.add(f"follow_imports = {scope['follow_imports']}")
        for setting in settings:
            yield f"mypy ({module}): {setting}"
    coverage = tool.get("coverage", {})
    for section, names in (("run", ("omit",)), ("report", ("omit", "exclude_lines", "exclude_also"))):
        for setting in config_values(coverage.get(section, {}), names):
            yield f"coverage.{section}: {setting}"


def workflow_overrides(source: bytes):
    overrides = Counter()
    for job_id, job in (yaml.safe_load(source) or {}).get("jobs", {}).items():
        if job.get("continue-on-error", False) is not False:
            overrides[(job_id, "", yaml.safe_dump(job))] += 1
        for step in job.get("steps", []):
            if step.get("continue-on-error", False) is not False:
                label = step.get("name") or step.get("id") or step.get("uses") or step.get("run")
                overrides[(job_id, label, yaml.safe_dump(step))] += 1
    return overrides


def known_float_violations(source: bytes):
    records = (yaml.safe_load(source) or {}).get("exemptions", [])
    violations = Counter()
    for record in records:
        if record["kind"] == "known-violation":
            key = record["file"], record["code"].strip()
            violations[key] += record.get("count", 1)
    return violations


def check_configuration(path: str, current: bytes, previous: bytes):
    if path == FLOAT_POLICY:
        for file, _ in known_float_violations(current) - known_float_violations(previous):
            yield f"{path}: HFT-D005 fix the money precision violation in {file}"
    elif path.endswith(".toml"):
        current_suppressed, current_selected = ruff_settings(path, current)
        previous_suppressed, previous_selected = ruff_settings(path, previous)
        if path != "pyproject.toml" and current != previous:
            config = tomllib.loads(current.decode())
            if not path.endswith("/pyproject.toml") or "ruff" in config.get("tool", {}):
                yield f"{path}: HFT-D005 use the root Ruff configuration"
                return
        for setting in sorted(current_suppressed - previous_suppressed):
            yield f"{path}: HFT-D005 remove the new Ruff suppression ({setting})"
        for setting in sorted(previous_selected - current_selected):
            yield f"{path}: HFT-D005 restore the Ruff rule ({setting})"
        if path == "pyproject.toml":
            additions = set(python_check_suppressions(current)) - set(python_check_suppressions(previous))
            for setting in sorted(additions):
                yield f"{path}: HFT-D005 remove the new check suppression ({setting})"
    else:
        for job_id, step, _ in workflow_overrides(current) - workflow_overrides(previous):
            location = f"{job_id} / {step}" if step else job_id
            yield f"{path}: HFT-D005 remove the new continue-on-error setting ({location})"


def read_file(path: str) -> bytes:
    file = ROOT / path
    if file.is_symlink() or not file.is_file():
        raise ValueError(f"expected a regular file: {path}")
    return file.read_bytes()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base", default="origin/main", help="Git revision used to check added suppressions")
    args = parser.parse_args()

    try:
        base = git("rev-parse", "--verify", "--end-of-options", f"{args.base}^{{commit}}").strip().decode()
        baseline = git_paths("ls-tree", "-r", "-z", "--name-only", base)
        paths = git_paths("ls-files", "--cached", "--others", "--exclude-standard", "-z")
        changed = git_paths("diff", "--name-only", "--no-renames", "-z", base) | (paths - baseline)
        checked_python = 0
        checked_rust = 0
        errors = []
        for path in sorted(paths):
            if path == "config/settings.py":
                continue
            python = path.endswith((".py", ".pyi")) and not path.startswith(IMMUTABLE)
            rust = path.endswith(".rs")
            configuration = (
                path == FLOAT_POLICY
                or Path(path).name in {"pyproject.toml", "ruff.toml", ".ruff.toml"}
                or (path.startswith(".github/workflows/") and path.endswith((".yml", ".yaml")))
            )
            if not python and not rust and not configuration:
                continue
            if not (ROOT / path).exists() and not (ROOT / path).is_symlink():
                continue
            current = read_file(path)
            previous = current
            if path in changed:
                previous = git("show", f"{base}:{path}") if path in baseline else b""
            if python:
                checked_python += 1
                errors.extend(check_python(path, current, previous))
            elif rust:
                checked_rust += 1
                errors.extend(check_rust(path, current, previous))
            else:
                errors.extend(check_configuration(path, current, previous))
    except (
        subprocess.CalledProcessError,
        OSError,
        ValueError,
        SyntaxError,
        tokenize.TokenError,
        yaml.YAMLError,
    ) as exc:
        print(f"Code policy check could not complete: {exc}", file=sys.stderr)
        return 2

    if errors:
        print("\n".join(errors), file=sys.stderr)
        print(f"Code policy check failed with {len(errors)} violation(s).", file=sys.stderr)
        return 1
    print(f"Code policy check passed ({checked_python} Python files, {checked_rust} Rust files).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
