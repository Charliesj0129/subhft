"""Integration tests for `python -m hft_platform.main` startup paths (loop_v1 L1).

Exercises the `main()` coroutine with `HFTSystem` and the metrics server
patched so we can validate:
  - sim startup without loop_id succeeds (legacy behavior)
  - shadow / live startup without loop_id refuses fast
  - shadow startup WITH loop_id succeeds (binding wires through)
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import yaml


@pytest.fixture
def loop_workspace(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    (tmp_path / "config" / "base").mkdir(parents=True)
    (tmp_path / "config" / "live").mkdir(parents=True)
    (tmp_path / "config" / "loops").mkdir(parents=True)
    (tmp_path / "config" / "env").mkdir(parents=True)

    (tmp_path / "config" / "base" / "main.yaml").write_text(
        yaml.safe_dump(
            {
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
                    }
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
    monkeypatch.delenv("HFT_RUNTIME_ROLE", raising=False)
    monkeypatch.delenv("HFT_ORDER_MODE", raising=False)
    monkeypatch.delenv("HFT_SYMBOLS", raising=False)
    return tmp_path


def test_refuse_helper_blocks_shadow_without_loop(monkeypatch):
    from hft_platform.main import _refuse_non_sim_without_loop

    monkeypatch.setenv("HFT_RUNTIME_ROLE", "engine")
    monkeypatch.setenv("HFT_ORDER_MODE", "shadow")
    with pytest.raises(RuntimeError, match="loop_id required"):
        _refuse_non_sim_without_loop({})


def test_refuse_helper_blocks_live_without_loop(monkeypatch):
    from hft_platform.main import _refuse_non_sim_without_loop

    monkeypatch.setenv("HFT_RUNTIME_ROLE", "engine")
    monkeypatch.setenv("HFT_ORDER_MODE", "live")
    with pytest.raises(RuntimeError, match="loop_id required"):
        _refuse_non_sim_without_loop({})


def test_refuse_helper_passes_with_loop_id_in_shadow(monkeypatch):
    from hft_platform.main import _refuse_non_sim_without_loop

    monkeypatch.setenv("HFT_RUNTIME_ROLE", "engine")
    monkeypatch.setenv("HFT_ORDER_MODE", "shadow")
    # Returns None on success; assert that explicitly.
    assert _refuse_non_sim_without_loop({"loop_id": "r47_tmf_v1"}) is None


def test_refuse_helper_passes_in_sim_without_loop(monkeypatch):
    from hft_platform.main import _refuse_non_sim_without_loop

    monkeypatch.setenv("HFT_RUNTIME_ROLE", "engine")
    monkeypatch.setenv("HFT_ORDER_MODE", "sim")
    assert _refuse_non_sim_without_loop({}) is None


def test_refuse_helper_skips_non_engine_roles(monkeypatch):
    from hft_platform.main import _refuse_non_sim_without_loop

    monkeypatch.setenv("HFT_RUNTIME_ROLE", "research")
    monkeypatch.setenv("HFT_ORDER_MODE", "live")
    # research role is exempt; returns None.
    assert _refuse_non_sim_without_loop({}) is None


def test_main_sim_startup_calls_loader(loop_workspace, monkeypatch):
    """Sim mode must call load_settings() and pass the result to HFTSystem()."""
    monkeypatch.setenv("HFT_ORDER_MODE", "sim")

    captured = {}

    async def _fake_run():
        return None

    fake_system = MagicMock()
    fake_system.run = AsyncMock(side_effect=_fake_run)

    def _capture_ctor(settings):
        captured["settings"] = settings
        return fake_system

    with (
        patch("hft_platform.main.start_resilient_metrics_server"),
        patch("hft_platform.main.HFTSystem", side_effect=_capture_ctor),
    ):

        async def _runner():
            from hft_platform.main import main

            await main()

        asyncio.run(_runner())

    assert "settings" in captured
    assert isinstance(captured["settings"], dict)
    assert captured["settings"]["mode"] == "sim"


def test_main_shadow_without_loop_refuses(loop_workspace, monkeypatch):
    """Shadow mode without loop_id must abort before HFTSystem is constructed."""
    monkeypatch.setenv("HFT_ORDER_MODE", "shadow")

    with patch("hft_platform.main.start_resilient_metrics_server"), patch("hft_platform.main.HFTSystem") as mock_sys:
        with pytest.raises(RuntimeError, match="loop_id required"):

            async def _runner():
                from hft_platform.main import main

                await main()

            asyncio.run(_runner())

    mock_sys.assert_not_called()


def test_main_shadow_with_loop_succeeds(loop_workspace, monkeypatch):
    """Shadow mode with HFT_LOOP env var binds the loop and proceeds."""
    monkeypatch.setenv("HFT_ORDER_MODE", "shadow")
    monkeypatch.setenv("HFT_LOOP", "r47_tmf_v1")

    captured = {}

    async def _fake_run():
        return None

    fake_system = MagicMock()
    fake_system.run = AsyncMock(side_effect=_fake_run)

    def _capture_ctor(settings):
        captured["settings"] = settings
        return fake_system

    with (
        patch("hft_platform.main.start_resilient_metrics_server"),
        patch("hft_platform.main.HFTSystem", side_effect=_capture_ctor),
    ):

        async def _runner():
            from hft_platform.main import main

            await main()

        asyncio.run(_runner())

    assert captured["settings"]["loop_id"] == "r47_tmf_v1"
    assert captured["settings"]["strategy"]["id"] == "R47_MAKER_TMF"
