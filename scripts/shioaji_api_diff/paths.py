"""Canonical repo-relative paths for the shioaji-api-diff tool."""

from __future__ import annotations

from pathlib import Path

# scripts/shioaji_api_diff/paths.py -> repo root is three parents up.
REPO_ROOT = Path(__file__).resolve().parents[2]
GOLDEN_DIR = REPO_ROOT / "tests" / "golden" / "shioaji_sdk"
RUNBOOK_PATH = REPO_ROOT / "docs" / "runbooks" / "shioaji-version-diff.md"

DEFAULT_VERSIONS = ["1.2.9", "1.3.3", "1.5.3"]
