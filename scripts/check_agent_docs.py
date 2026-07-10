"""Agent-docs consistency gate.

Governing agent documents (CLAUDE.md, AGENTS.md, .agent rules/skills indexes)
have repeatedly drifted from the tree they describe, and every instance was
caught by hand. This gate makes the three drift classes machine-checkable:

  A. path-refs   — every repo path referenced in backticks inside a governing
                   doc exists on disk.
  B. skills-index — `.agent/skills/<name>/` directories and the rows of
                   `.agent/skills/00-index.md` match bidirectionally.
  C. memory-table — the routing table in `.agent/memory/README.md` and the
                   actual `.agent/memory/*.md` files match bidirectionally.

Scope note: `.agent/memory/*.md` bodies other than README.md are historical
records — their path claims are dated snapshots, deliberately NOT checked.
`.agent/rules/ecc/` and other DEPRECATED generations (see .agent/00-MANIFEST.md)
are excluded.

Known pre-existing drift is tolerated via .agent/agent-docs-known-drift.txt
(one `<check-id> <subject>` per line). The file is a ratchet: entries that no
longer match anything are reported as stale so the list only shrinks.
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

KNOWN_DRIFT_FILE = Path(".agent/agent-docs-known-drift.txt")

# A backtick token is treated as a repo path claim only when it is plainly
# path-shaped: safe charset, and anchored at a known repo root (or an exact
# well-known top-level file). Everything else (code, globs, placeholders,
# relative mentions) is ignored rather than guessed at.
_PATH_CHARSET = re.compile(r"^[A-Za-z0-9_.\-/]+$")
_PATH_ROOTS = (
    ".agent/",
    "config/",
    "docs/",
    "research/",
    "rust_core/",
    "scripts/",
    "src/",
    "tests/",
)
_TOP_LEVEL_FILES = {
    "AGENTS.md",
    "CLAUDE.md",
    "Makefile",
    "pyproject.toml",
    ".importlinter",
    ".gitignore",
}
_BACKTICK_TOKEN = re.compile(r"`([^`\n]+)`")
_INDEX_ROW_NAME = re.compile(r"^\|\s*`([A-Za-z0-9_-]+)`\s*\|")
_MEMORY_ROW_NAME = re.compile(r"^\|\s*`([A-Za-z0-9_.\-]+\.md)`\s*\|")


def governing_docs(root: Path) -> list[Path]:
    docs: list[Path] = []
    for name in ("CLAUDE.md", "AGENTS.md"):
        if (root / name).is_file():
            docs.append(root / name)
    manifest = root / ".agent/00-MANIFEST.md"
    if manifest.is_file():
        docs.append(manifest)
    docs.extend(sorted((root / ".agent/rules").glob("*.md")))
    skills = root / ".agent/skills"
    for candidate in ("00-index.md", "README.md"):
        if (skills / candidate).is_file():
            docs.append(skills / candidate)
    docs.extend(sorted(skills.glob("*/SKILL.md")))
    readme = root / ".agent/memory/README.md"
    if readme.is_file():
        docs.append(readme)
    return docs


def extract_path_tokens(text: str) -> list[str]:
    tokens: list[str] = []
    for raw in _BACKTICK_TOKEN.findall(text):
        token = raw.strip().rstrip(".,;:")
        # `file.py:32` style line refs: keep the path part only.
        if ":" in token:
            token = token.split(":", 1)[0]
        if not token or not _PATH_CHARSET.match(token):
            continue
        if token in _TOP_LEVEL_FILES:
            tokens.append(token)
        elif token.startswith(_PATH_ROOTS) and "/" in token:
            tokens.append(token)
    return tokens


def check_path_refs(root: Path, drift: set[str], errors: list[str], seen: set[str]) -> None:
    for doc in governing_docs(root):
        rel_doc = doc.relative_to(root)
        for token in extract_path_tokens(doc.read_text(encoding="utf-8")):
            target = root / token
            if target.exists():
                continue
            key = f"path {token}"
            if key in drift:
                seen.add(key)
                continue
            errors.append(f"path-refs: {rel_doc}: `{token}` does not exist")


def check_skills_index(root: Path, drift: set[str], errors: list[str], seen: set[str]) -> None:
    skills = root / ".agent/skills"
    index = skills / "00-index.md"
    if not index.is_file():
        errors.append("skills-index: .agent/skills/00-index.md is missing")
        return
    indexed: set[str] = set()
    for line in index.read_text(encoding="utf-8").splitlines():
        match = _INDEX_ROW_NAME.match(line.strip())
        if match:
            indexed.add(match.group(1))
    on_disk = {p.parent.name for p in skills.glob("*/SKILL.md")}
    for name in sorted(on_disk - indexed):
        key = f"skill-unindexed {name}"
        if key in drift:
            seen.add(key)
            continue
        errors.append(f"skills-index: directory `{name}` has no row in 00-index.md")
    for name in sorted(indexed - on_disk):
        key = f"skill-phantom {name}"
        if key in drift:
            seen.add(key)
            continue
        errors.append(f"skills-index: row `{name}` has no .agent/skills/{name}/SKILL.md")


def check_memory_table(root: Path, drift: set[str], errors: list[str], seen: set[str]) -> None:
    memory = root / ".agent/memory"
    readme = memory / "README.md"
    if not readme.is_file():
        errors.append("memory-table: .agent/memory/README.md is missing")
        return
    listed: set[str] = set()
    for line in readme.read_text(encoding="utf-8").splitlines():
        match = _MEMORY_ROW_NAME.match(line.strip())
        if match:
            listed.add(match.group(1))
    on_disk = {p.name for p in memory.glob("*.md")} - {"README.md"}
    for name in sorted(on_disk - listed):
        key = f"memory-unlisted {name}"
        if key in drift:
            seen.add(key)
            continue
        errors.append(f"memory-table: file `{name}` is not in the README routing table")
    for name in sorted(listed - on_disk):
        key = f"memory-phantom {name}"
        if key in drift:
            seen.add(key)
            continue
        errors.append(f"memory-table: README routing table lists `{name}` which does not exist")


def load_known_drift(root: Path) -> set[str]:
    path = root / KNOWN_DRIFT_FILE
    if not path.is_file():
        return set()
    entries: set[str] = set()
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#"):
            entries.add(line)
    return entries


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--root",
        type=Path,
        default=Path.cwd(),
        help="Repo root to check (default: current directory)",
    )
    args = parser.parse_args(argv)
    root: Path = args.root.resolve()

    drift = load_known_drift(root)
    seen: set[str] = set()
    errors: list[str] = []
    check_path_refs(root, drift, errors, seen)
    check_skills_index(root, drift, errors, seen)
    check_memory_table(root, drift, errors, seen)

    for line in errors:
        print(f"ERROR {line}")
    stale = sorted(drift - seen)
    for entry in stale:
        print(f"WARN stale known-drift entry (fixed or gone — remove it): {entry}")

    tolerated = len(seen)
    print(
        f"agent-docs-check: {len(errors)} error(s), {tolerated} tolerated known-drift, "
        f"{len(stale)} stale baseline entr(ies)"
    )
    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(main())
