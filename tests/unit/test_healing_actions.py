"""Tests for healing action registry."""
from __future__ import annotations

from unittest.mock import AsyncMock

import pytest


class TestActionRegistry:
    def test_register_and_get(self):
        from hft_platform.healing.actions import ActionRegistry
        registry = ActionRegistry()
        action_fn = AsyncMock()
        registry.register("test_action", action_fn)
        assert registry.get("test_action") is action_fn

    def test_get_unknown_returns_none(self):
        from hft_platform.healing.actions import ActionRegistry
        registry = ActionRegistry()
        assert registry.get("unknown_action") is None

    def test_list_actions(self):
        from hft_platform.healing.actions import ActionRegistry
        registry = ActionRegistry()
        registry.register("action_a", AsyncMock())
        registry.register("action_b", AsyncMock())
        names = registry.list_actions()
        assert "action_a" in names
        assert "action_b" in names

    @pytest.mark.asyncio
    async def test_wait_action(self):
        from hft_platform.healing.actions import ActionRegistry
        registry = ActionRegistry()
        registry.register_builtins()
        wait_fn = registry.get("wait")
        assert wait_fn is not None
        await wait_fn(duration_s=0.01)

    def test_register_builtins_includes_log_warn(self):
        from hft_platform.healing.actions import ActionRegistry
        registry = ActionRegistry()
        registry.register_builtins()
        assert registry.get("log_warn") is not None

    def test_list_actions_empty(self):
        from hft_platform.healing.actions import ActionRegistry
        registry = ActionRegistry()
        assert registry.list_actions() == []

    def test_register_overwrite(self):
        from hft_platform.healing.actions import ActionRegistry
        registry = ActionRegistry()
        fn1 = AsyncMock()
        fn2 = AsyncMock()
        registry.register("my_action", fn1)
        registry.register("my_action", fn2)
        assert registry.get("my_action") is fn2

    @pytest.mark.asyncio
    async def test_log_warn_action(self):
        from hft_platform.healing.actions import ActionRegistry
        registry = ActionRegistry()
        registry.register_builtins()
        log_warn_fn = registry.get("log_warn")
        assert log_warn_fn is not None
        # Should not raise
        await log_warn_fn(message="test warning")

    def test_list_actions_returns_all_registered(self):
        from hft_platform.healing.actions import ActionRegistry
        registry = ActionRegistry()
        for i in range(5):
            registry.register(f"action_{i}", AsyncMock())
        names = registry.list_actions()
        assert len(names) == 5
        for i in range(5):
            assert f"action_{i}" in names

    def test_register_builtins_count(self):
        from hft_platform.healing.actions import ActionRegistry
        registry = ActionRegistry()
        registry.register_builtins()
        names = registry.list_actions()
        assert "wait" in names
        assert "log_warn" in names
        assert len(names) >= 2
