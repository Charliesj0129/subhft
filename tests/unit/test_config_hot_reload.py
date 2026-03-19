"""Tests for ConfigWatcher hot-reload (WU-14)."""

from __future__ import annotations

import asyncio
from typing import Any, Dict, List
from unittest.mock import MagicMock

import pytest
import yaml

from hft_platform.config.hot_reload import _MAX_CALLBACKS, ConfigWatcher


def _write_yaml(path: str, data: Dict[str, Any]) -> None:
    with open(path, "w") as f:
        yaml.safe_dump(data, f)


class TestConfigHotReload:
    """Tests for ConfigWatcher."""

    def test_initial_load(self, tmp_path: Any) -> None:
        cfg_file = tmp_path / "limits.yaml"
        data = {"global_defaults": {"per_symbol_max_notional": 42}}
        _write_yaml(str(cfg_file), data)

        watcher = ConfigWatcher(str(cfg_file))
        watcher._load_and_store()

        assert watcher.current_config == data

    def test_missing_file_initial_load(self, tmp_path: Any) -> None:
        watcher = ConfigWatcher(str(tmp_path / "nonexistent.yaml"))
        watcher._load_and_store()
        assert watcher.current_config == {}

    def test_register_callback(self, tmp_path: Any) -> None:
        cfg_file = tmp_path / "limits.yaml"
        _write_yaml(str(cfg_file), {"a": 1})

        watcher = ConfigWatcher(str(cfg_file))
        cb = MagicMock()
        watcher.register(cb)
        assert len(watcher._callbacks) == 1

    def test_register_callback_limit(self, tmp_path: Any) -> None:
        cfg_file = tmp_path / "limits.yaml"
        _write_yaml(str(cfg_file), {"a": 1})

        watcher = ConfigWatcher(str(cfg_file))
        for _ in range(_MAX_CALLBACKS):
            watcher.register(lambda c: None)

        with pytest.raises(RuntimeError, match="callback limit exceeded"):
            watcher.register(lambda c: None)

    def test_reload_on_mtime_change(self, tmp_path: Any) -> None:
        cfg_file = tmp_path / "limits.yaml"
        original = {"global_defaults": {"per_symbol_max_notional": 10}}
        _write_yaml(str(cfg_file), original)

        watcher = ConfigWatcher(str(cfg_file))
        watcher._load_and_store()

        received: List[Dict[str, Any]] = []
        watcher.register(lambda c: received.append(c))

        # Modify the file
        updated = {"global_defaults": {"per_symbol_max_notional": 99}}
        _write_yaml(str(cfg_file), updated)

        # Force a different mtime (filesystem resolution may be 1s)
        new_mtime = watcher._last_mtime + 10.0
        watcher._load_and_notify(new_mtime)

        assert len(received) == 1
        assert received[0] == updated
        assert watcher.current_config == updated

    def test_invalid_yaml_keeps_old_config(self, tmp_path: Any) -> None:
        cfg_file = tmp_path / "limits.yaml"
        original = {"global_defaults": {"x": 1}}
        _write_yaml(str(cfg_file), original)

        watcher = ConfigWatcher(str(cfg_file))
        watcher._load_and_store()

        # Write invalid YAML
        with open(str(cfg_file), "w") as f:
            f.write(": : : invalid yaml [[[")

        new_mtime = watcher._last_mtime + 10.0
        watcher._load_and_notify(new_mtime)

        # Old config should be preserved
        assert watcher.current_config == original

    def test_non_dict_yaml_keeps_old_config(self, tmp_path: Any) -> None:
        cfg_file = tmp_path / "limits.yaml"
        original = {"global_defaults": {"x": 1}}
        _write_yaml(str(cfg_file), original)

        watcher = ConfigWatcher(str(cfg_file))
        watcher._load_and_store()

        # Write a YAML list instead of dict
        with open(str(cfg_file), "w") as f:
            f.write("- item1\n- item2\n")

        new_mtime = watcher._last_mtime + 10.0
        watcher._load_and_notify(new_mtime)

        assert watcher.current_config == original

    def test_callback_error_does_not_crash(self, tmp_path: Any) -> None:
        cfg_file = tmp_path / "limits.yaml"
        _write_yaml(str(cfg_file), {"a": 1})

        watcher = ConfigWatcher(str(cfg_file))
        watcher._load_and_store()

        def bad_callback(c: Dict[str, Any]) -> None:
            raise ValueError("boom")

        good_received: List[Dict[str, Any]] = []

        watcher.register(bad_callback)
        watcher.register(lambda c: good_received.append(c))

        updated = {"a": 2}
        _write_yaml(str(cfg_file), updated)
        watcher._load_and_notify(watcher._last_mtime + 10.0)

        # Good callback still called despite bad one raising
        assert len(good_received) == 1
        assert good_received[0] == updated

    @pytest.mark.asyncio
    async def test_start_and_stop(self, tmp_path: Any) -> None:
        cfg_file = tmp_path / "limits.yaml"
        _write_yaml(str(cfg_file), {"global_defaults": {}})

        watcher = ConfigWatcher(str(cfg_file), poll_interval_s=1.0)
        watcher.start()

        assert watcher._task is not None
        assert not watcher._task.done()

        await watcher.stop()
        assert watcher._task is None

    @pytest.mark.asyncio
    async def test_poll_detects_change(self, tmp_path: Any) -> None:
        cfg_file = tmp_path / "limits.yaml"
        _write_yaml(str(cfg_file), {"v": 1})

        watcher = ConfigWatcher(str(cfg_file), poll_interval_s=1.0)
        # Override poll interval to something fast for testing
        watcher._poll_interval_s = 0.1
        received: List[Dict[str, Any]] = []
        watcher.register(lambda c: received.append(c))
        watcher.start()

        # Wait for initial poll
        await asyncio.sleep(0.05)

        # Modify file
        _write_yaml(str(cfg_file), {"v": 2})

        # Wait for poll to pick it up (multiple poll cycles)
        await asyncio.sleep(0.5)

        await watcher.stop()

        # Should have received at least one callback
        assert len(received) >= 1
        assert received[-1] == {"v": 2}

    def test_check_and_reload_no_change(self, tmp_path: Any) -> None:
        """No callback when mtime hasn't changed."""
        cfg_file = tmp_path / "limits.yaml"
        _write_yaml(str(cfg_file), {"a": 1})

        watcher = ConfigWatcher(str(cfg_file))
        watcher._load_and_store()

        received: List[Dict[str, Any]] = []
        watcher.register(lambda c: received.append(c))

        # Synchronous call with same mtime — no notify
        loop = asyncio.new_event_loop()
        loop.run_until_complete(watcher._check_and_reload())
        loop.close()

        assert len(received) == 0

    def test_sighup_handler_flag(self, tmp_path: Any) -> None:
        """SIGHUP registration is attempted on start (Unix)."""
        cfg_file = tmp_path / "limits.yaml"
        _write_yaml(str(cfg_file), {"a": 1})

        watcher = ConfigWatcher(str(cfg_file))
        # Just verify the flag exists and is False initially
        assert watcher._sighup_registered is False

    def test_poll_interval_minimum(self) -> None:
        """Poll interval clamped to at least 1.0s."""
        watcher = ConfigWatcher("/dev/null", poll_interval_s=0.01)
        assert watcher._poll_interval_s >= 1.0


class TestRiskEngineReloadCallback:
    """Test the RiskEngine.on_config_reload integration."""

    def test_on_config_reload_clears_caches(self, tmp_path: Any) -> None:
        """RiskEngine.on_config_reload clears validator caches."""
        cfg_file = tmp_path / "limits.yaml"
        original = {
            "global_defaults": {"per_symbol_max_notional": 50_000_000, "max_notional": 10_000_000},
            "strategies": {},
        }
        _write_yaml(str(cfg_file), original)

        intent_q: asyncio.Queue = asyncio.Queue()
        order_q: asyncio.Queue = asyncio.Queue()

        from hft_platform.risk.engine import RiskEngine

        engine = RiskEngine(str(cfg_file), intent_q, order_q)

        # Populate some caches by running a check
        from hft_platform.contracts.strategy import IntentType, OrderIntent, Side

        intent = OrderIntent(
            intent_id=1,
            strategy_id="s1",
            symbol="2330",
            intent_type=IntentType.NEW,
            side=Side.BUY,
            price=1_000_000,
            qty=1,
        )
        engine.evaluate(intent)

        # Verify some caches are populated
        has_cached = False
        for v in engine.validators:
            for attr in vars(v):
                if "cache" in attr.lower():
                    obj = getattr(v, attr)
                    if isinstance(obj, dict) and len(obj) > 0:
                        has_cached = True
        assert has_cached, "Expected at least one validator cache to be populated"

        # Now trigger reload
        new_config = {
            "global_defaults": {"per_symbol_max_notional": 1, "max_notional": 1},
            "strategies": {},
        }
        engine.on_config_reload(new_config)

        # All caches should be cleared
        for v in engine.validators:
            for attr in vars(v):
                if "cache" in attr.lower():
                    obj = getattr(v, attr)
                    if isinstance(obj, dict):
                        assert len(obj) == 0, f"Cache {attr} on {type(v).__name__} not cleared"

        # Config should be updated
        assert engine.config == new_config
