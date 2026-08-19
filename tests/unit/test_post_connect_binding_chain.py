"""Regression tests for the dead post-connect binding chain (2026-08-08).

Symptom: ``alpha_signal_events_total{outcome="intent"} == 0`` for R47 across
16.7M received events, and no order in ClickHouse since 2026-06-08 — while
every health signal, dashboard and alert stayed green.

Root cause chain, each link verified against production:

1. ``config/live/strategies.yaml`` pins ``symbols: ["TMFE6"]`` — a May-2026
   expiry — and relies on ``contract_families`` to roll it forward.
2. That roll-forward is the Shioaji family populator, registered on
   ``MarketDataService._post_connect_hooks``.
3. ``_propagate_alias_map`` ran the whole hook chain only when the alias map
   *changed size*.
4. The live symbol universe declares no R1/R2/C0/C1 aliases, so the map is
   permanently empty and ``new_size == prev_size == 0`` on every connect.
5. → no hook ever ran → ``strategy.symbols`` stayed ``{"TMFE6"}`` while the
   feed published ``TMFI6`` → ``StrategyBase.handle_event`` dropped 100% of
   events at its symbol filter.

Two independent breaks sat on the same chain: the populator also received
``None`` for ``api`` (the pool object had no ``.api`` property) and returned
an empty calendar without logging, and the stale-instrument gate — the check
that exists to refuse exactly this — was registered as a hook, so the runner's
``except Exception`` would have downgraded its refusal to a warning.

The gate then had to be fixed twice. Moving it off ``_post_connect_hooks``
onto its own list made its exception propagate, but it still ran from the
post-subscribe propagation pass — i.e. *after* ``subscribe_basket``, when the
broker is already streaming the contract being refused. Placement, not just
exception handling, is what makes a gate a gate; see
``test_gate_refusal_prevents_the_subscription_from_happening``.
"""

from __future__ import annotations

import asyncio
from datetime import date
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from hft_platform.contracts.family_resolver import ContractFamilyResolver
from hft_platform.contracts.ref import ContractFamily, FamilyCode, Product
from hft_platform.feed_adapter.shioaji.family_populator import populate_resolver_from_shioaji
from hft_platform.services import market_data as market_data_module
from hft_platform.services.market_data import FeedState, MarketDataService


class _FakeSymbolMetadata:
    def __init__(self) -> None:
        self.alias_to_actual: dict[str, str] = {}

    def set_alias_map(self, alias_map: dict[str, str]) -> None:
        self.alias_to_actual.update(alias_map)


def _md_shim(client, *, hooks=None, gates=None) -> MarketDataService:
    """Minimal MarketDataService carrying only the propagation surface."""
    inst = MarketDataService.__new__(MarketDataService)
    inst.client = client
    inst.symbol_metadata = _FakeSymbolMetadata()
    inst._post_connect_hooks = list(hooks or [])
    inst._post_connect_gates = list(gates or [])
    inst.metrics_registry = None
    return inst


# --------------------------------------------------------------------------- #
# 1. The defect itself                                                         #
# --------------------------------------------------------------------------- #


def test_post_connect_hooks_run_even_when_alias_map_is_empty():
    """An alias-free symbol universe must still get one hook pass per connect.

    This is the exact production state: 292 concrete-month entries, zero
    aliases. Before the fix the hook chain was gated on an alias-map size
    change, so it never ran at all.
    """
    hook = MagicMock()
    md = _md_shim(SimpleNamespace(alias_to_actual={}), hooks=[hook])

    md._propagate_alias_map(trigger="post_subscribe", force_hooks=True)

    hook.assert_called_once()


def test_unforced_propagation_still_skips_hooks_when_nothing_changed():
    """The size-change trigger is kept as an *additional* trigger, not replaced.

    Late aliases learned by the background retry thread must still fire hooks
    without waiting for a reconnect (Bug 12), and a no-change pass must stay
    cheap.
    """
    hook = MagicMock()
    client = SimpleNamespace(alias_to_actual={})
    md = _md_shim(client, hooks=[hook])

    md._propagate_alias_map(trigger="pre_subscribe")
    hook.assert_not_called()

    client.alias_to_actual["TMFR1"] = "TMFE6"
    md._propagate_alias_map(trigger="retry")
    assert hook.call_count == 1


def test_one_failing_hook_does_not_stop_the_rest_of_the_chain():
    """Hooks stay advisory: a broken one must not silence the populator."""
    order: list[str] = []

    def _boom() -> None:
        order.append("boom")
        raise RuntimeError("hook exploded")

    def _later() -> None:
        order.append("later")

    md = _md_shim(SimpleNamespace(alias_to_actual={}), hooks=[_boom, _later])
    md._propagate_alias_map(trigger="post_subscribe", force_hooks=True)

    assert order == ["boom", "later"]


# --------------------------------------------------------------------------- #
# 2. The chain end to end: expired binding → front month                        #
# --------------------------------------------------------------------------- #


def test_family_populator_rebinds_strategy_symbols_to_current_front_month():
    """Drive the real chain: connect fires the hook, the populator rebinds.

    The strategy starts pinned to the expired ``TMFE6`` the config named and
    must end up on the live front month without any alias ever appearing.
    """
    from hft_platform.strategy.runner import StrategyRunner

    resolver = ContractFamilyResolver()
    strategy = SimpleNamespace(
        strategy_id="R47_MAKER_TMF",
        symbols={"TMFE6"},
        contract_families=(ContractFamily(Product.FUTURE, "TMF", FamilyCode.R1),),
    )
    runner = StrategyRunner.__new__(StrategyRunner)
    runner.strategies = [strategy]
    runner._family_resolver = None
    runner.set_family_resolver(resolver)

    # Mapping container matching Shioaji's ``Contracts.Futures`` interface.
    roots = {
        "TMF": [
            SimpleNamespace(code="TMFI6", delivery_date="2026/09/16", delivery_month=None),
            SimpleNamespace(code="TMFJ6", delivery_date="2026/10/21", delivery_month=None),
        ]
    }

    class _Container:
        def keys(self):
            return roots.keys()

        def __getitem__(self, root):
            return roots[root]

    api = SimpleNamespace(Contracts=SimpleNamespace(Futures=_Container()))

    def _populate() -> None:
        populate_resolver_from_shioaji(resolver, api, today=date(2026, 8, 8))

    md = _md_shim(SimpleNamespace(alias_to_actual={}), hooks=[_populate])
    md._propagate_alias_map(trigger="post_subscribe", force_hooks=True)

    assert "TMFI6" in strategy.symbols, "front month must be bound after connect"
    # The expired expiry survives because the config file hardcodes it and
    # ``_apply_family_bindings`` only adds. Harmless for liveness (nothing is
    # subscribed to it) but it keeps ``preflight_symbol_mismatch`` firing —
    # removing the hardcode from config/live/strategies.yaml is a separate,
    # frozen-registry change.
    assert "TMFE6" in strategy.symbols


# --------------------------------------------------------------------------- #
# 3. Gates fail closed instead of degrading to a warning                        #
# --------------------------------------------------------------------------- #


def test_stale_instrument_gate_runs_on_a_plain_connect():
    """The gate must fire on every connect, not only on an alias delta."""
    gate = MagicMock()
    md = _md_shim(SimpleNamespace(alias_to_actual={}), gates=[gate])

    md._run_connect_gates()

    gate.assert_called_once()


def test_gate_refusal_propagates_instead_of_being_swallowed(module_log_sink):
    """A gate raising is the whole point of a gate.

    On ``_post_connect_hooks`` the runner's ``except Exception`` caught
    ``StaleInstrumentError`` and logged a warning, so the platform carried on
    subscribed to an expired contract.

    Reads the event through ``module_log_sink`` rather than
    ``structlog.testing.capture_logs``: the latter swaps the *global* processor
    chain, which an earlier test in a full-suite run has already made
    ineffective, so this assertion failed for months on a code path that was
    working — see the fixture's docstring. A false red on the guard for the
    two-month outage is worse than no guard, because it trains the reader to
    skip it.
    """

    def _refuse() -> None:
        raise RuntimeError("stale instrument TMFE6")

    events = module_log_sink(market_data_module)
    md = _md_shim(SimpleNamespace(alias_to_actual={}), gates=[_refuse])

    with pytest.raises(RuntimeError, match="stale instrument TMFE6"):
        md._run_connect_gates()

    blocked = [e for e in events if e.get("event") == "connect_gate_blocked"]
    assert blocked, "a blocked gate must emit its own event, not a generic connect failure"
    assert blocked[0]["error_type"] == "RuntimeError"


def test_gates_never_run_from_alias_propagation():
    """Gates are not part of the propagation pass at all.

    They used to be, which is how they ended up running *after*
    ``subscribe_basket`` — see
    ``test_gate_refusal_prevents_the_subscription_from_happening``.
    """
    gate = MagicMock()
    client = SimpleNamespace(alias_to_actual={"TMFR1": "TMFE6"})
    md = _md_shim(client, gates=[gate])

    md._propagate_alias_map(trigger="pre_subscribe")
    md._propagate_alias_map(trigger="post_subscribe", force_hooks=True)

    gate.assert_not_called()


def test_gate_refusal_prevents_the_subscription_from_happening():
    """A refused connect must never reach ``subscribe_basket``.

    This is the test that distinguishes a gate from a log line. The first
    version of this fix ran gates *after* ``subscribe_basket``, so by the time
    the gate refused an expired contract the broker was already streaming it:
    the raise was caught by ``_connect_sequence``'s ``except Exception``,
    ``_set_state(DISCONNECTED)`` wrote a field nothing outside this module
    reads, and quotes kept arriving on the raw queue exactly as before. The
    refusal changed nothing it was written to prevent.
    """
    client = MagicMock()
    client.login.return_value = True
    client.fetch_snapshots.return_value = []

    def _refuse() -> None:
        raise RuntimeError("stale instrument TMFE6")

    md = _md_shim(client, gates=[_refuse])
    md.state = FeedState.INIT
    md.metrics_registry = None
    md._resolve_aliases_eager = lambda: None
    md._propagate_alias_map = lambda **_kwargs: 0

    asyncio.run(md._connect_sequence())

    client.subscribe_basket.assert_not_called()
    assert md.state is FeedState.DISCONNECTED


def test_gate_refusal_increments_its_own_counter():
    """A deliberate refusal and a dead broker both present as a silent feed.

    The counter is the only thing that tells an operator which one happened.
    """
    counter = MagicMock()

    def _refuse() -> None:
        raise RuntimeError("stale instrument TMFE6")

    md = _md_shim(SimpleNamespace(alias_to_actual={}), gates=[_refuse])
    md.metrics_registry = SimpleNamespace(feed_connect_gate_blocked_total=counter)

    with pytest.raises(RuntimeError):
        md._run_connect_gates()

    counter.labels.assert_called_once_with(gate="_refuse")
    counter.labels.return_value.inc.assert_called_once()


# --------------------------------------------------------------------------- #
# 4. Coverage ratio stops claiming health it cannot have                        #
# --------------------------------------------------------------------------- #


def test_coverage_ratio_is_not_one_when_no_aliases_are_configured():
    """0/0 is undefined, not "fully covered".

    Reporting 1.0 made the healthiest-looking gauge on the dashboard the
    direct evidence of the empty alias map that disabled the hook chain.
    """
    from hft_platform.observability.metrics import MetricsRegistry

    MetricsRegistry._instance = None
    registry = MetricsRegistry.get()

    md = _md_shim(SimpleNamespace(alias_to_actual={}))
    md._propagate_alias_map(trigger="post_subscribe", force_hooks=True)

    ratio = registry.alias_resolution_coverage_ratio._value.get()
    assert ratio != 1.0
    assert ratio != ratio, "undefined coverage must read as NaN"
    assert registry.alias_map_size._value.get() == 0.0


def test_alias_map_size_reports_the_configured_alias_count():
    from hft_platform.observability.metrics import MetricsRegistry

    MetricsRegistry._instance = None
    registry = MetricsRegistry.get()

    md = _md_shim(SimpleNamespace(alias_to_actual={"TMFR1": "TMFE6", "TXFR1": "TXFE6"}))
    md._propagate_alias_map(trigger="post_subscribe", force_hooks=True)

    assert registry.alias_map_size._value.get() == 2.0
    assert registry.alias_resolution_coverage_ratio._value.get() == 1.0


# --------------------------------------------------------------------------- #
# 5. Trading liveness is measurable                                             #
# --------------------------------------------------------------------------- #


def test_bound_live_symbols_gauge_is_zero_when_strategy_binds_expired_contract():
    """The number that would have caught this in an hour instead of two months."""
    from hft_platform.observability.metrics import MetricsRegistry
    from hft_platform.services.bootstrap import publish_strategy_binding_liveness

    MetricsRegistry._instance = None
    registry = MetricsRegistry.get()

    strategy = SimpleNamespace(strategy_id="R47_MAKER_TMF", symbols={"TMFE6"})
    counts = publish_strategy_binding_liveness([strategy], {"TMFI6", "TXFI6"})

    assert counts == {"R47_MAKER_TMF": 0}
    gauge = registry.strategy_bound_live_symbols.labels(strategy_id="R47_MAKER_TMF")
    assert gauge._value.get() == 0.0


def test_bound_live_symbols_gauge_counts_only_symbols_the_feed_carries():
    from hft_platform.observability.metrics import MetricsRegistry
    from hft_platform.services.bootstrap import publish_strategy_binding_liveness

    MetricsRegistry._instance = None
    MetricsRegistry.get()

    strategy = SimpleNamespace(strategy_id="R47_MAKER_TMF", symbols={"TMFI6", "TMFE6"})
    counts = publish_strategy_binding_liveness([strategy], {"TMFI6", "TXFI6"})

    assert counts == {"R47_MAKER_TMF": 1}


# --------------------------------------------------------------------------- #
# 5. Field findings from the 2026-08-08 deploy                                 #
# --------------------------------------------------------------------------- #


def test_liveness_measurement_is_registered_after_the_family_populators():
    """The gauge must judge the settled state, not the state it starts in.

    Found in the field: the first deploy of this fix rebound R47 from the
    expired ``TMFE6`` onto ``TMFH6`` exactly as designed, and
    ``strategy_bound_live_symbols`` still read 0. Hooks run in registration
    order, and ``_preflight_symbol_consistency`` — the only hook that publishes
    the gauge — was registered *before* the populator that does the rebind, so
    it measured the stale config on every connect. Left alone this would have
    fired a false ``StrategyBoundToNoLiveSymbols`` critical within five minutes
    of the next session open.

    A source-order assertion is normally a poor test. Here the defect *is* the
    order, it is invisible in any single unit's behaviour, and it had a dated
    production consequence — so the ordering is the thing worth pinning.
    """
    import inspect

    from hft_platform.services import bootstrap

    source = inspect.getsource(bootstrap)
    populator = source.index("_post_connect_hooks.append(_populate_families_from_shioaji)")
    liveness = source.index("_post_connect_hooks.append(_preflight_symbol_consistency)")

    assert populator < liveness, (
        "the liveness gauge is published before the family populator rebinds "
        "strategy.symbols, so it always reports the pre-rebind state"
    )


def test_pool_exposes_contract_lookup_from_a_logged_in_client():
    """Second attribute the pool did not forward, found the same way as ``api``.

    Under `HFT_QUOTE_CONNECTIONS=4` the stale-instrument gate resolved
    `md_client._get_contract`, got `None`, and returned without checking
    anything — so the gate ran, reported nothing, and verified nothing.
    """
    from hft_platform.feed_adapter.shioaji.quote_connection_pool import QuoteConnectionPool

    sentinel = object()
    # Real shape, verified against the running engine: ShioajiClientFacade
    # exposes `api` and `logged_in` but NOT `_get_contract`, and defines no
    # `__getattr__`, so the lookup only exists one level down on `_client`.
    # The first version of this test used a facade fake that carried
    # `_get_contract` directly — a fake more capable than the real object — so
    # it passed while production resolved every contract to None.
    dead = SimpleNamespace(logged_in=False, _client=SimpleNamespace(_get_contract=lambda *a, **k: "WRONG"))
    live = SimpleNamespace(logged_in=True, _client=SimpleNamespace(_get_contract=lambda *a, **k: sentinel))

    pool = QuoteConnectionPool.__new__(QuoteConnectionPool)
    pool._clients = [dead, live]

    assert not hasattr(live, "_get_contract"), "fake must match the real facade surface"
    assert pool._get_contract("TAIFEX", "TMFH6") is sentinel


def test_pool_contract_lookup_prefers_the_facade_when_it_has_one():
    """If the facade ever grows the method, use it rather than reaching past."""
    from hft_platform.feed_adapter.shioaji.quote_connection_pool import QuoteConnectionPool

    sentinel = object()
    facade = SimpleNamespace(
        logged_in=True,
        _get_contract=lambda *a, **k: sentinel,
        _client=SimpleNamespace(_get_contract=lambda *a, **k: "INNER"),
    )
    pool = QuoteConnectionPool.__new__(QuoteConnectionPool)
    pool._clients = [facade]

    assert pool._get_contract("TAIFEX", "TMFH6") is sentinel


def test_pool_contract_lookup_returns_none_when_no_facade_is_logged_in():
    """`None` means "cannot judge", which callers must not read as "fine"."""
    from hft_platform.feed_adapter.shioaji.quote_connection_pool import QuoteConnectionPool

    pool = QuoteConnectionPool.__new__(QuoteConnectionPool)
    pool._clients = [SimpleNamespace(logged_in=False, _client=SimpleNamespace(_get_contract=lambda *a, **k: "WRONG"))]

    assert pool._get_contract("TAIFEX", "TMFH6") is None
