"""Candidate-spec gate CLI (goal 完成狀態 §3 enforcement).

Round 11 shipped ``validate_spec()``.  Round 12 makes it callable as
a CI / pre-flight gate over either a single path or every candidate
spec under ``research/alphas/<id>/spec.yaml``.

Usage::

    python -m hft_platform.alpha.spec_check path/to/spec.yaml
    python -m hft_platform.alpha.spec_check --all
    python -m hft_platform.alpha.spec_check --root research/experiments

Exit codes:
    0 — every spec validated.
    1 — at least one spec is invalid OR --all found zero spec.yaml
        files (silent zero would mask a wiring break).
    2 — argparse usage error (handled by argparse).

The CLI returns a string per spec (label + errors) so the caller can
pipe it into a punch-list.  Library functions are pure for testing.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from hft_platform.alpha.strategy_spec import load_spec, validate_spec

DEFAULT_ROOT = Path("research/alphas")
SPEC_FILENAME = "spec.yaml"


def discover_specs(root: str | Path = DEFAULT_ROOT) -> list[Path]:
    """Return every ``spec.yaml`` directly under ``<root>/<candidate>/``.

    Order is sorted by directory name for deterministic CLI output.
    Non-existent root returns []; a candidate without a spec file is
    silently skipped (the spec gate is opt-in per candidate).
    """
    root_p = Path(root)
    if not root_p.exists():
        return []
    out: list[Path] = []
    for child in sorted(root_p.iterdir()):
        if not child.is_dir():
            continue
        spec = child / SPEC_FILENAME
        if spec.is_file():
            out.append(spec)
    return out


def check_one(path: str | Path) -> tuple[bool, list[str]]:
    """Validate one spec file.

    Returns (passed, errors).  Load failures (missing file, parse
    error, non-mapping) are reported as single-element error lists
    rather than raised, so a CI loop can collect every failure in one
    pass.
    """
    p = Path(path)
    try:
        spec = load_spec(p)
    except FileNotFoundError:
        return False, [f"spec not found: {p}"]
    except ValueError as exc:
        return False, [str(exc)]
    except Exception as exc:  # noqa: BLE001
        return False, [f"failed to load {p}: {exc!r}"]
    errors = validate_spec(spec)
    return len(errors) == 0, errors


def _format_report(path: Path, passed: bool, errors: list[str]) -> str:
    if passed:
        return f"[ok]   {path}"
    lines = [f"[FAIL] {path}"]
    for e in errors:
        lines.append(f"         - {e}")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="hft-alpha-spec-check",
        description=(
            "Validate candidate strategy_spec YAMLs against the canonical "
            "schema (goal §3).  Exits non-zero on any failure."
        ),
    )
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("path", nargs="?", help="Path to one spec.yaml file.")
    group.add_argument(
        "--all",
        action="store_true",
        help="Scan every candidate under --root for a spec.yaml file.",
    )
    parser.add_argument(
        "--root",
        default=str(DEFAULT_ROOT),
        help=f"Root to scan when --all is set (default: {DEFAULT_ROOT}).",
    )
    args = parser.parse_args(argv)

    if args.all:
        specs = discover_specs(args.root)
        if not specs:
            print(f"no spec.yaml files found under {args.root}")
            return 1
    else:
        specs = [Path(args.path)]

    rc = 0
    for spec_path in specs:
        passed, errors = check_one(spec_path)
        print(_format_report(spec_path, passed, errors))
        if not passed:
            rc = 1
    return rc


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
