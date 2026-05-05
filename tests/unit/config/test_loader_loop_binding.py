"""Tests for loader loop_id binding (loop_v1 L1)."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from hft_platform.config.loader import (
    LoopBindingError,
    _assert_strategy_enabled,
    _bind_loop,
    load_settings,
    resolve_active_strategy,
)


@pytest.fixture
def loop_workspace(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Materialize a minimal config tree and chdir into it.

    Layout:
      config/base/main.yaml         — base entrypoint with loop_id
      config/live/strategies.yaml   — registry with R47_MAKER_TMF enabled
      config/loops/r47_tmf_v1.yaml  — loop definition
    """
    (tmp_path / "config" / "base").mkdir(parents=True)
    (tmp_path / "config" / "live").mkdir(parents=True)
    (tmp_path / "config" / "loops").mkdir(parents=True)
    (tmp_path / "config" / "env").mkdir(parents=True)

    (tmp_path / "config" / "base" / "main.yaml").write_text(
        yaml.safe_dump(
            {
                "loop_id": "r47_tmf_v1",
                "mode": "sim",
                "broker": "shioaji",
                "symbols": ["TMFR1"],
                "prometheus_port": 9090,
            }
        )
    )
    (tmp_path / "config" / "live" / "strategies.yaml").write_text(
        yaml.safe_dump(
            {
                "strategies": [
                    {
                        "id": "R47_MAKER_TMF",
                        "module": "hft_platform.strategies.r47_maker",
                        "class": "R47MakerStrategy",
                        "enabled": True,
                    },
                    {
                        "id": "DISABLED_STRAT",
                        "module": "hft_platform.strategies.simple_mm",
                        "class": "SimpleMarketMaker",
                        "enabled": False,
                    },
                ]
            }
        )
    )
    (tmp_path / "config" / "loops" / "r47_tmf_v1.yaml").write_text(
        yaml.safe_dump(
            {
                "loop_id": "r47_tmf_v1",
                "strategy": {
                    "id": "R47_MAKER_TMF",
                    "module": "hft_platform.strategies.r47_maker",
                    "class": "R47MakerStrategy",
                },
                "broker": "shioaji",
            }
        )
    )

    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("HFT_LOOP", raising=False)
    monkeypatch.delenv("HFT_CONFIG_STRICT", raising=False)
    monkeypatch.delenv("HFT_MODE", raising=False)
    monkeypatch.delenv("HFT_ENV", raising=False)
    monkeypatch.delenv("HFT_SYMBOLS", raising=False)
    return tmp_path


# -- _assert_strategy_enabled -------------------------------------------------

def test_assert_strategy_enabled_passes_when_enabled(loop_workspace: Path):
    _assert_strategy_enabled("R47_MAKER_TMF")  # no raise


def test_assert_strategy_enabled_rejects_disabled(loop_workspace: Path):
    with pytest.raises(LoopBindingError, match="disabled"):
        _assert_strategy_enabled("DISABLED_STRAT")


def test_assert_strategy_enabled_rejects_missing(loop_workspace: Path):
    with pytest.raises(LoopBindingError, match="not present"):
        _assert_strategy_enabled("NEVER_HEARD_OF_THIS_ONE")


def test_assert_strategy_enabled_rejects_no_registry(tmp_path: Path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    with pytest.raises(LoopBindingError, match="strategies registry not found"):
        _assert_strategy_enabled("R47_MAKER_TMF")


# -- _bind_loop ---------------------------------------------------------------

def test_bind_loop_overrides_strategy(loop_workspace: Path):
    settings = {
        "loop_id": "r47_tmf_v1",
        "strategy": {"id": "WRONG_ID", "module": "x", "class": "y"},
        "broker": "fubon",
    }
    out = _bind_loop(settings)
    assert out["strategy"]["id"] == "R47_MAKER_TMF"
    assert out["broker"] == "shioaji"


def test_bind_loop_no_op_without_loop_id(loop_workspace: Path):
    settings = {"strategy": {"id": "X"}, "broker": "fubon"}
    out = _bind_loop(settings)
    assert out["strategy"]["id"] == "X"
    assert out["broker"] == "fubon"


def test_bind_loop_missing_loop_file(loop_workspace: Path):
    settings = {"loop_id": "nonexistent_loop"}
    with pytest.raises(LoopBindingError, match="loop file not found"):
        _bind_loop(settings)


def test_bind_loop_loop_id_mismatch(loop_workspace: Path):
    bad = loop_workspace / "config" / "loops" / "bad.yaml"
    bad.write_text(yaml.safe_dump({"loop_id": "different_id", "strategy": {"id": "X"}}))
    settings = {"loop_id": "bad"}
    with pytest.raises(LoopBindingError, match="loop file"):
        _bind_loop(settings)


def test_bind_loop_disabled_strategy_blocks_binding(loop_workspace: Path):
    bad = loop_workspace / "config" / "loops" / "bad.yaml"
    bad.write_text(
        yaml.safe_dump(
            {
                "loop_id": "bad",
                "strategy": {
                    "id": "DISABLED_STRAT",
                    "module": "hft_platform.strategies.simple_mm",
                    "class": "SimpleMarketMaker",
                },
            }
        )
    )
    settings = {"loop_id": "bad"}
    with pytest.raises(LoopBindingError, match="disabled"):
        _bind_loop(settings)


# -- resolve_active_strategy --------------------------------------------------

def test_resolve_active_strategy_returns_dict(loop_workspace: Path):
    settings = {"strategy": {"id": "X", "module": "m", "class": "C"}}
    assert resolve_active_strategy(settings) == {"id": "X", "module": "m", "class": "C"}


def test_resolve_active_strategy_handles_missing():
    assert resolve_active_strategy({}) == {}
    assert resolve_active_strategy({"strategy": None}) == {}


# -- load_settings end-to-end -------------------------------------------------

def test_load_settings_resolves_loop_from_main_yaml(loop_workspace: Path):
    settings, _ = load_settings()
    assert settings["loop_id"] == "r47_tmf_v1"
    assert settings["strategy"]["id"] == "R47_MAKER_TMF"
    assert settings["broker"] == "shioaji"


def test_load_settings_hft_loop_env_var(loop_workspace: Path, monkeypatch):
    # Strip loop_id from main.yaml so env var alone drives the binding.
    (loop_workspace / "config" / "base" / "main.yaml").write_text(
        yaml.safe_dump(
            {
                "mode": "sim",
                "broker": "shioaji",
                "symbols": ["TMFR1"],
                "prometheus_port": 9090,
            }
        )
    )
    monkeypatch.setenv("HFT_LOOP", "r47_tmf_v1")
    settings, _ = load_settings()
    assert settings["loop_id"] == "r47_tmf_v1"
    assert settings["strategy"]["id"] == "R47_MAKER_TMF"


def test_load_settings_strict_rejects_unknown_top_level(loop_workspace: Path):
    """When loop_id is bound, strict mode is on; unknown top-level keys must SystemExit."""
    main_path = loop_workspace / "config" / "base" / "main.yaml"
    bad = yaml.safe_load(main_path.read_text())
    bad["definitely_a_typo"] = "boom"
    main_path.write_text(yaml.safe_dump(bad))
    with pytest.raises(SystemExit):
        load_settings()


def test_load_settings_cli_loop_override(loop_workspace: Path):
    # Default loop is r47_tmf_v1. CLI override to a freshly-written loop.
    second = loop_workspace / "config" / "loops" / "alt_loop.yaml"
    second.write_text(
        yaml.safe_dump(
            {
                "loop_id": "alt_loop",
                "strategy": {
                    "id": "R47_MAKER_TMF",
                    "module": "hft_platform.strategies.r47_maker",
                    "class": "R47MakerStrategy",
                },
                "broker": "shioaji",
            }
        )
    )
    settings, _ = load_settings({"loop_id": "alt_loop"})
    assert settings["loop_id"] == "alt_loop"
    assert settings["strategy"]["id"] == "R47_MAKER_TMF"


def test_load_settings_no_loop_keeps_legacy_behavior(loop_workspace: Path):
    """A config without loop_id retains pre-L1 behavior (loose validation)."""
    (loop_workspace / "config" / "base" / "main.yaml").write_text(
        yaml.safe_dump(
            {
                "mode": "sim",
                "broker": "shioaji",
                "symbols": ["TMFR1"],
                "strategy": {
                    "id": "R47_MAKER_TMF",
                    "module": "hft_platform.strategies.r47_maker",
                    "class": "R47MakerStrategy",
                },
                "prometheus_port": 9090,
                "an_unknown_key_that_used_to_pass": "still does",
            }
        )
    )
    settings, _ = load_settings()
    assert "loop_id" not in settings or settings["loop_id"] is None
