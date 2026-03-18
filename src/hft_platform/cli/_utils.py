"""Shared CLI utilities — path helpers, formatters, parsers."""

from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Any


def _ensure_project_root_on_path() -> None:
    """Ensure repository root is importable for research/* modules."""
    root = Path(__file__).resolve().parents[3]
    if (root / "research").exists():
        root_str = str(root)
        if root_str not in sys.path:
            sys.path.insert(0, root_str)


def _safe_write(path: str, content: str) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        f.write(content)


def _resolve_default_mode() -> str:
    raw = str(os.getenv("HFT_MODE", "sim")).strip().lower()
    if raw == "real":
        return "live"
    if raw not in {"sim", "live", "replay"}:
        return "sim"
    return raw


def _print_issues(errors: list[str], warnings: list[str]) -> None:
    if warnings:
        print("Warnings:")
        for msg in warnings[:20]:
            print(f"- {msg}")
        if len(warnings) > 20:
            print(f"... {len(warnings) - 20} more warnings")
    if errors:
        print("Errors:")
        for msg in errors[:20]:
            print(f"- {msg}")
        if len(errors) > 20:
            print(f"... {len(errors) - 20} more errors")


def _parse_param_grid(raw: str | None) -> dict[str, list[Any]]:
    if not raw:
        return {}
    grid: dict[str, list[Any]] = {}
    pairs = [part.strip() for part in str(raw).split(";") if part.strip()]
    for pair in pairs:
        if "=" not in pair:
            raise ValueError(f"Invalid grid token: {pair}")
        key, val = pair.split("=", 1)
        items = [item.strip() for item in val.split(",") if item.strip()]
        casted: list[Any] = []
        for item in items:
            try:
                casted.append(int(item))
                continue
            except ValueError:
                pass
            try:
                casted.append(float(item))
                continue
            except ValueError:
                pass
            casted.append(item)
        grid[key.strip()] = casted
    return grid
