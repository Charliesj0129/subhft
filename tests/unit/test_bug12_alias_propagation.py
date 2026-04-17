"""Regression test for Bug 12 (2026-04-17).

Symptom: after `docker compose restart`, ``symbol_metadata.alias_to_actual``
stayed empty. StrategyRunner._resolve_strategy_symbols saw an empty alias
map and skipped resolution, leaving R47 with ``strategy.symbols = {"TMFR1"}``
while the feed published ``TMFE6`` — R47 silent for 13 hours during the
2026-04-17 day session.

Root cause: the alias map was populated only as a side effect of
``_subscribe_symbol`` calls, and the propagation to SymbolMetadata ran once
inline after subscribe_basket. When the callback-not-ready guard pushed
work to the background retry thread, no one ever triggered propagation for
the late subscriptions.

Fix in this commit:
  (a) ``MarketDataService._resolve_aliases_eager`` pre-populates the map via
      ContractsRuntime.resolve_symbol_aliases() before subscribe.
  (b) ``_propagate_alias_map`` is idempotent and fires post-connect hooks only
      when the map grows.
  (c) The quote_runtime retry thread invokes ``client.on_alias_map_updated``
      after each successful late subscribe so propagation catches up.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock


def _make_md_service_shim(symbol_metadata, client, post_connect_hooks=None):
    """Build a minimal MarketDataService stub with only the bits under test."""
    from hft_platform.services.market_data import MarketDataService

    inst = MarketDataService.__new__(MarketDataService)
    inst.client = client
    inst.symbol_metadata = symbol_metadata
    inst._post_connect_hooks = list(post_connect_hooks or [])
    return inst


class _FakeSymbolMetadata:
    def __init__(self):
        self.alias_to_actual: dict[str, str] = {}

    def set_alias_map(self, alias_map):
        self.alias_to_actual.update(alias_map)


def test_propagate_is_idempotent_and_fires_hooks_only_on_growth():
    sm = _FakeSymbolMetadata()
    client = SimpleNamespace(alias_to_actual={"TMFR1": "TMFE6"})
    hook = MagicMock()
    inst = _make_md_service_shim(sm, client, post_connect_hooks=[hook])

    # First call propagates and fires hook (map grew from 0 → 1)
    inst._propagate_alias_map(trigger="pre_subscribe")
    assert sm.alias_to_actual == {"TMFR1": "TMFE6"}
    assert hook.call_count == 1

    # Second call with no new aliases: no hook re-fire
    inst._propagate_alias_map(trigger="post_subscribe")
    assert hook.call_count == 1

    # Late alias (retry thread style): hook fires again
    client.alias_to_actual["TXFR1"] = "TXFE6"
    inst._propagate_alias_map(trigger="retry")
    assert sm.alias_to_actual == {"TMFR1": "TMFE6", "TXFR1": "TXFE6"}
    assert hook.call_count == 2


def test_empty_alias_map_does_not_fire_hooks():
    sm = _FakeSymbolMetadata()
    client = SimpleNamespace(alias_to_actual={})
    hook = MagicMock()
    inst = _make_md_service_shim(sm, client, post_connect_hooks=[hook])
    inst._propagate_alias_map(trigger="pre_subscribe")
    assert sm.alias_to_actual == {}
    hook.assert_not_called()


def test_eager_resolve_calls_contracts_runtime():
    sm = _FakeSymbolMetadata()
    runtime = MagicMock()
    runtime.resolve_symbol_aliases.return_value = {"TMFR1": "TMFE6"}
    client = SimpleNamespace(
        alias_to_actual={"TMFR1": "TMFE6"},  # contracts_runtime would have updated this
        contracts_runtime=runtime,
    )
    inst = _make_md_service_shim(sm, client)
    inst._resolve_aliases_eager()
    runtime.resolve_symbol_aliases.assert_called_once()


def test_eager_resolve_swallows_runtime_errors():
    sm = _FakeSymbolMetadata()
    runtime = MagicMock()
    runtime.resolve_symbol_aliases.side_effect = RuntimeError("contract cache cold")
    client = SimpleNamespace(alias_to_actual={}, contracts_runtime=runtime)
    inst = _make_md_service_shim(sm, client)
    inst._resolve_aliases_eager()  # must not raise


def test_eager_resolve_without_runtime_is_noop():
    sm = _FakeSymbolMetadata()
    client = SimpleNamespace(alias_to_actual={})
    inst = _make_md_service_shim(sm, client)
    inst._resolve_aliases_eager()  # must not raise


def test_retry_thread_callback_populates_late_aliases():
    """Simulate Bug 12 scenario: inline subscribe defers, background retry
    succeeds later. The ``on_alias_map_updated`` callback must trigger
    propagation so strategy hook re-fires.
    """
    sm = _FakeSymbolMetadata()
    client = SimpleNamespace(alias_to_actual={})
    hook = MagicMock()
    inst = _make_md_service_shim(sm, client, post_connect_hooks=[hook])

    # Connect finished with no aliases (deferred)
    inst._propagate_alias_map(trigger="post_subscribe")
    assert hook.call_count == 0

    # Retry thread installs the callback
    client.on_alias_map_updated = lambda: inst._propagate_alias_map(trigger="retry")

    # Retry thread succeeds: alias now populated + callback invoked
    client.alias_to_actual["TMFR1"] = "TMFE6"
    client.on_alias_map_updated()

    assert sm.alias_to_actual == {"TMFR1": "TMFE6"}
    assert hook.call_count == 1
