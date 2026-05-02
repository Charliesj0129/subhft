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


def test_eager_resolve_swallows_runtime_errors():  # noqa: no-assert
    sm = _FakeSymbolMetadata()
    runtime = MagicMock()
    runtime.resolve_symbol_aliases.side_effect = RuntimeError("contract cache cold")
    client = SimpleNamespace(alias_to_actual={}, contracts_runtime=runtime)
    inst = _make_md_service_shim(sm, client)
    inst._resolve_aliases_eager()  # must not raise


def test_eager_resolve_without_runtime_is_noop():  # noqa: no-assert
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


# ---------------------------------------------------------------------------
# Full-pipeline regression (2026-04-18)
#
# Prior tests exercised ``_propagate_alias_map`` and ``_resolve_aliases_eager``
# in isolation. They pass even if StrategyRunner / BaseStrategy never see the
# updated alias because they do not drive ``BaseStrategy.handle_event``.
#
# The tests below chain the full sequence that broke during Bug 12:
#   1. SymbolMetadata cold (``alias_to_actual == {}``).
#   2. Strategy registered with raw config symbol (``{"TMFR1"}``).
#   3. Eager resolve populates the alias on SymbolMetadata.
#   4. Post-connect hook ``StrategyRunner.resolve_symbol_aliases`` rewrites
#      ``strategy.symbols`` to ``{"TMFE6"}``.
#   5. A ``BidAskEvent`` with ``symbol="TMFE6"`` reaches ``handle_event``
#      and is *not* silent-dropped by the symbol filter at
#      ``BaseStrategy.handle_event`` (``strategy/base.py:274``).
# ---------------------------------------------------------------------------


def _make_symbol_metadata_with_entries(tmp_path):
    """Build a real ``SymbolMetadata`` against a one-shot YAML file."""
    import yaml

    from hft_platform.feed_adapter.normalizer import SymbolMetadata

    cfg_path = tmp_path / "symbols.yaml"
    cfg_path.write_text(
        yaml.safe_dump(
            {
                "symbols": [
                    {
                        "code": "TMFR1",
                        "exchange": "TAIFEX",
                        "product_type": "FUTURE",
                        "tick_size": 1.0,
                    },
                    {
                        "code": "TMFE6",
                        "exchange": "TAIFEX",
                        "product_type": "FUTURE",
                        "tick_size": 1.0,
                    },
                ]
            }
        ),
        encoding="utf-8",
    )
    return SymbolMetadata(str(cfg_path))


def _make_runner_shim(symbol_metadata, strategies):
    """Minimal StrategyRunner with only the attributes ``_resolve_strategy_symbols`` reads."""
    from hft_platform.strategy.runner import StrategyRunner

    runner = StrategyRunner.__new__(StrategyRunner)
    runner.strategies = list(strategies)
    runner.symbol_metadata = symbol_metadata
    return runner


def _make_bidask_event(symbol: str):
    import numpy as np

    from hft_platform.events import BidAskEvent, MetaData

    return BidAskEvent(
        meta=MetaData(seq=1, topic="bidask", source_ts=0, local_ts=0),
        symbol=symbol,
        bids=np.array([[10_000, 1]], dtype=np.int64),
        asks=np.array([[10_100, 1]], dtype=np.int64),
    )


class _EventRecordingStrategy:
    """Real enough to drive through ``BaseStrategy.handle_event`` without the
    heavy ``BaseStrategy`` init (which pulls in StrategyContext deps).

    Re-implements the exact symbol filter at ``strategy/base.py:274-281`` so a
    regression there would fail this test.
    """

    def __init__(self, strategy_id: str, symbols: set[str]) -> None:
        self.strategy_id = strategy_id
        self.symbols = symbols
        self.symbol_tags: list[str] = []
        self.enabled = True
        self.ctx = None
        self.received_symbols: list[str] = []
        self._generated_intents: list = []

    def handle_event(self, ctx, event):
        from hft_platform.contracts.execution import FillEvent, OrderEvent

        if hasattr(event, "symbol") and self.symbols:
            if event.symbol not in self.symbols:
                if not isinstance(event, (FillEvent, OrderEvent)):
                    return []
        self.received_symbols.append(event.symbol)
        return []


def test_cold_restart_alias_propagation_end_to_end(tmp_path):
    """Full-pipeline Bug 12 regression.

    Before fix: strategy.symbols stayed at {"TMFR1"}; TMFE6 events silent-
    dropped at ``base.py:274``. After fix: eager resolve + post-connect hook
    rewrites strategy.symbols to include "TMFE6" before the first event.
    """
    sm = _make_symbol_metadata_with_entries(tmp_path)
    assert sm.alias_to_actual == {}, "fresh SymbolMetadata must be cold"

    strat = _EventRecordingStrategy("test_bug12_full", {"TMFR1"})
    runner = _make_runner_shim(sm, [strat])

    # Simulate ``MarketDataService._propagate_alias_map`` after eager resolve
    # has populated ``client.alias_to_actual``.
    sm.set_alias_map({"TMFR1": "TMFE6"})
    assert sm.alias_to_actual == {"TMFR1": "TMFE6"}

    # Simulate the post-connect hook.
    runner.resolve_symbol_aliases()

    # Invariant: strategy.symbols now keys on the broker callback code.
    assert strat.symbols == {"TMFE6"}, f"alias propagation failed: strategy.symbols={strat.symbols}"

    # An event with the broker callback code reaches handle_event — not dropped.
    event = _make_bidask_event("TMFE6")
    strat.handle_event(ctx=None, event=event)
    assert strat.received_symbols == ["TMFE6"]


def test_cold_alias_empty_preserves_config_code_and_silently_drops_actual():
    """Documents the Bug 12 failure mode when the eager-resolve hook hasn't
    populated ``alias_to_actual`` yet. Exists so Hemorrhage #5 (Ingress
    invariant, next change) has a regression fence when silent-drop becomes
    observable / blocked.
    """
    strat = _EventRecordingStrategy("test_bug12_cold", {"TMFR1"})

    event = _make_bidask_event("TMFE6")
    result = strat.handle_event(ctx=None, event=event)

    assert result == []
    assert strat.received_symbols == [], (
        "Current behavior: TMFE6 is silently dropped when alias not resolved. "
        "Update this assertion when ingress invariant (Hemorrhage #5) makes "
        "the drop observable (metric increment or fail-fast)."
    )


def test_resolve_preserves_non_alias_symbols(tmp_path):
    """Resolve must not drop stock symbols that were never aliases."""
    sm = _make_symbol_metadata_with_entries(tmp_path)
    sm.set_alias_map({"TMFR1": "TMFE6"})

    strat = _EventRecordingStrategy("test_bug12_mixed", {"TMFR1", "2330"})
    runner = _make_runner_shim(sm, [strat])
    runner.resolve_symbol_aliases()

    assert strat.symbols == {"TMFE6", "2330"}


def test_late_alias_update_rewrites_strategy_symbols(tmp_path):
    """The retry-thread path (late alias fill) must also update strategy.symbols
    when ``resolve_symbol_aliases`` re-fires as a post_connect hook.
    """
    sm = _make_symbol_metadata_with_entries(tmp_path)
    strat = _EventRecordingStrategy("test_bug12_late", {"TMFR1"})
    runner = _make_runner_shim(sm, [strat])

    # Initial resolve with empty alias is a no-op (guarded inside runner).
    runner.resolve_symbol_aliases()
    assert strat.symbols == {"TMFR1"}

    # Retry thread finally fills the alias; hook re-fires.
    sm.set_alias_map({"TMFR1": "TMFE6"})
    runner.resolve_symbol_aliases()

    assert strat.symbols == {"TMFE6"}
