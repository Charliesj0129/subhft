"""Tests for L10 (loop_v1): live registry contains only enabled-true entries.

After L10 cutover, ``config/live/strategies.yaml`` is the canonical live
registry consumed by ``StrategyRegistry`` / ``StrategyRunner`` defaults.
This file MUST contain only ``enabled: true`` entries — disabled / killed /
revoked / shadow strategies live under
``research/strategy_archive/strategies_2026_05.yaml`` and are explicitly
not loaded at runtime.

This guard prevents accidental archive→live re-introduction without going
through promotion gates D/E/F + L11 freeze approval.
"""

from __future__ import annotations

from pathlib import Path

import yaml


def _load_live_registry() -> dict:
    path = Path("config/live/strategies.yaml")
    assert path.exists(), f"live registry missing: {path}"
    return yaml.safe_load(path.read_text(encoding="utf-8"))


class TestLiveRegistryStructure:
    def test_live_registry_file_exists(self) -> None:
        assert Path("config/live/strategies.yaml").is_file()

    def test_live_registry_has_strategies_top_level_key(self) -> None:
        cfg = _load_live_registry()
        assert "strategies" in cfg
        assert isinstance(cfg["strategies"], list)

    def test_live_registry_is_non_empty(self) -> None:
        cfg = _load_live_registry()
        assert len(cfg["strategies"]) >= 1, "live registry must contain at least R47"

    def test_every_live_entry_is_enabled_true(self) -> None:
        cfg = _load_live_registry()
        for entry in cfg["strategies"]:
            sid = entry.get("id", "<unknown>")
            assert entry.get("enabled") is True, (
                f"live registry entry {sid!r} must have enabled: true; "
                f"disabled entries belong under research/strategy_archive/"
            )

    def test_live_registry_contains_r47_maker_tmf(self) -> None:
        cfg = _load_live_registry()
        ids = [e["id"] for e in cfg["strategies"]]
        assert "R47_MAKER_TMF" in ids, (
            "R47_MAKER_TMF is the only loop_v1 production strategy; "
            "it must be present in config/live/strategies.yaml."
        )

    def test_live_registry_does_not_contain_archived_strategies(self) -> None:
        # Cross-check: ids explicitly archived in L10 must NOT have snuck
        # back into live. This catches a future PR that copies an archived
        # entry into config/live/ without flipping enabled to true.
        cfg = _load_live_registry()
        live_ids = {e["id"] for e in cfg["strategies"]}
        archived_ids = {
            "OPPORTUNISTIC_MM_TMF",
            "OPPORTUNISTIC_MM_TXF",
            "CBS_TMF",
            "MOMENTUM_BOUNCE_TMF",
            "TX_TMF_LEADLAG",
            "electronic_eye",
            "C14_TXF_FRONTMONTH_MAKER",
            "C17_TMF_FRONTMONTH_MAKER",
            "C27_VOL_AMPLIFIED_C14",
            "C33_TXFD6_SOLO_MAKER",
            "C60_TMFD6_SOLO_MAKER",
            "C63_TXFD6_TIGHT_SPREAD_MAKER",
        }
        leaked = live_ids & archived_ids
        assert not leaked, (
            f"archived strategies leaked into live registry: {sorted(leaked)}; "
            f"see docs/runbooks/forced_promotion.md and L11 freeze policy."
        )


class TestArchiveSnapshot:
    def test_archive_file_exists(self) -> None:
        assert Path("research/strategy_archive/strategies_2026_05.yaml").is_file()

    def test_every_archive_entry_is_enabled_false(self) -> None:
        path = Path("research/strategy_archive/strategies_2026_05.yaml")
        cfg = yaml.safe_load(path.read_text(encoding="utf-8"))
        for entry in cfg.get("strategies", []):
            sid = entry.get("id", "<unknown>")
            assert entry.get("enabled") is False, (
                f"archive entry {sid!r} must have enabled: false; "
                f"if it became live-eligible, move it to config/live/"
            )


class TestRegistryDefaultLoadsLive:
    def test_StrategyRegistry_default_path_loads_live_registry(self) -> None:
        # Smoke: StrategyRegistry() with no args must successfully open the
        # post-cutover default.
        from hft_platform.strategy.registry import StrategyRegistry

        reg = StrategyRegistry()
        assert reg.config_path == "config/live/strategies.yaml"
        # And the configs list should not be empty.
        assert len(reg.configs) >= 1
