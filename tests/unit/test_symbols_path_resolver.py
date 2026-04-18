"""Tests for the unified ``resolve_symbols_config_path`` helper.

Guards against the 2026-04-15 class of incident where five independent callers
had subtly different fallback chains for the same YAML file.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from hft_platform.config import symbols_path as sp


@pytest.fixture(autouse=True)
def _scrub_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Guarantee a clean slate for ``SYMBOLS_CONFIG`` in every test."""
    monkeypatch.delenv("SYMBOLS_CONFIG", raising=False)


class TestPrecedence:
    def test_explicit_wins(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("SYMBOLS_CONFIG", "/env/value.yaml")
        out = sp.resolve_symbols_config_path(
            explicit=str(tmp_path / "pick-me.yaml"),
            paths_setting="/settings/value.yaml",
        )
        assert out.endswith("pick-me.yaml")
        assert "env" not in out
        assert "settings" not in out

    def test_env_beats_settings_when_no_explicit(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("SYMBOLS_CONFIG", "/env/win.yaml")
        out = sp.resolve_symbols_config_path(paths_setting="/settings/lose.yaml")
        assert out.endswith("/env/win.yaml")

    def test_settings_wins_when_no_env(self) -> None:
        out = sp.resolve_symbols_config_path(paths_setting="/settings/win.yaml")
        assert out.endswith("/settings/win.yaml")

    def test_empty_env_treated_as_unset(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("SYMBOLS_CONFIG", "   ")
        out = sp.resolve_symbols_config_path(paths_setting="/settings/win.yaml")
        assert out.endswith("/settings/win.yaml")


class TestFallback:
    def test_canonical_base_fallback(self) -> None:
        out = sp.resolve_symbols_config_path()
        # With no explicit/env/settings input and whatever state the repo has,
        # resolver must still return a string. In a fresh checkout it is
        # ``config/base/symbols.yaml`` — which is checked in.
        assert out.endswith("config/base/symbols.yaml") or out.endswith(
            "config/symbols.yaml"
        )

    def test_returns_absolute_path(self) -> None:
        out = sp.resolve_symbols_config_path(paths_setting="relative/path.yaml")
        assert os.path.isabs(out)


class TestPropagateEnv:
    def test_sets_env_when_unset(self, monkeypatch: pytest.MonkeyPatch) -> None:
        sp.propagate_env("/a/chosen/path.yaml")
        assert os.environ["SYMBOLS_CONFIG"] == "/a/chosen/path.yaml"

    def test_preserves_operator_override(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("SYMBOLS_CONFIG", "/operator/override.yaml")
        sp.propagate_env("/would-overwrite.yaml")
        assert os.environ["SYMBOLS_CONFIG"] == "/operator/override.yaml"


class TestInternalPicker:
    def test_pick_returns_tier(self) -> None:
        tier, path = sp._pick("/explicit.yaml", paths_setting=None)
        assert tier == "explicit"
        assert path == "/explicit.yaml"

    def test_pick_env_tier(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("SYMBOLS_CONFIG", "/env.yaml")
        tier, path = sp._pick(None, paths_setting=None)
        assert tier == "env"

    def test_pick_settings_tier(self) -> None:
        tier, _ = sp._pick(None, paths_setting="/settings.yaml")
        assert tier == "settings"

    def test_pick_base_default_last(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # Ensure the "project_cwd_file" tier cannot win by pointing the project
        # root's ``config/symbols.yaml`` check at a non-existent location.
        monkeypatch.setattr(sp, "_PROJECT_ROOT", Path("/definitely-not-a-real-path"))
        tier, path = sp._pick(None, paths_setting=None)
        assert tier == "base_default"
        assert path.endswith("config/base/symbols.yaml")
