"""Shared CLI utilities (not user-facing)."""

import os


def _safe_write(path: str, content: str) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        f.write(content)


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
