"""Regression checks for the platform CLI/research package boundary."""

from __future__ import annotations

import ast
import subprocess
import sys
from pathlib import Path


def test_platform_parser_has_no_research_import() -> None:
    parser_path = Path(__file__).resolve().parents[2] / "src" / "hft_platform" / "cli" / "_parser.py"
    tree = ast.parse(parser_path.read_text(encoding="utf-8"))
    research_imports: list[str] = []

    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            research_imports.extend(
                alias.name for alias in node.names if alias.name == "research" or alias.name.startswith("research.")
            )
        elif isinstance(node, ast.ImportFrom):
            module = node.module or ""
            if module == "research" or module.startswith("research."):
                research_imports.append(module)

    assert research_imports == []


def test_building_platform_parser_does_not_load_research_modules() -> None:
    code = """
import sys
from hft_platform.cli import build_parser

build_parser()
loaded = sorted(
    module for module in sys.modules
    if module == "research" or module.startswith("research.")
)
print(",".join(loaded))
"""
    result = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == ""
