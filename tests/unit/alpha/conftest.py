"""Shared fixtures for the alpha governance tests."""

from __future__ import annotations

from pathlib import Path

import pytest


@pytest.fixture
def governed_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    """An evidence root outside the repo and a clean, known tree, so a strict run may start."""
    from research import pipeline

    root = tmp_path / "evidence"
    monkeypatch.setenv("HFT_RESEARCH_EVIDENCE_ROOT", str(root))
    monkeypatch.setattr(pipeline, "git_state", lambda _root: ("c0ffee", False))
    return root
