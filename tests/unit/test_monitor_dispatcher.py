from __future__ import annotations

import math

from hft_platform.monitor._alpha_dispatcher import AlphaDispatcher
from hft_platform.monitor._types import AlphaState, SymbolState, WatchlistSymbol
from research.registry.schemas import AlphaManifest, AlphaStatus, AlphaTier


def _symbol_state(alpha_ids: tuple[str, ...]) -> SymbolState:
    return SymbolState(
        symbol=WatchlistSymbol(
            code="TMFC6",
            name="TMF",
            product_type="future",
            alpha_ids=alpha_ids,
        )
    )


class _CounterAlpha:
    manifest = AlphaManifest(
        alpha_id="counter",
        hypothesis="",
        formula="",
        paper_refs=(),
        data_fields=("bid_qty", "ask_qty"),
        complexity="O(1)",
        status=AlphaStatus.DRAFT,
        tier=AlphaTier.TIER_2,
    )

    def __init__(self) -> None:
        self.total = 0.0

    def update(self, bid_qty: float, ask_qty: float) -> float:
        self.total += bid_qty - ask_qty
        return self.total

    def reset(self) -> None:
        self.total = 0.0

    def get_signal(self) -> float:
        return self.total


class _FilteredAlpha:
    manifest = AlphaManifest(
        alpha_id="filtered",
        hypothesis="",
        formula="",
        paper_refs=(),
        data_fields=("bid_qty", "ask_qty"),
        complexity="O(1)",
        status=AlphaStatus.DRAFT,
        tier=AlphaTier.TIER_2,
    )

    def update(self, bid_qty: float, ask_qty: float) -> float:
        return bid_qty - ask_qty

    def reset(self) -> None:
        pass

    def get_signal(self) -> float:
        return 0.0


class _BrokenAlpha:
    manifest = AlphaManifest(
        alpha_id="broken",
        hypothesis="",
        formula="",
        paper_refs=(),
        data_fields=("bid_qty",),
        complexity="O(1)",
        status=AlphaStatus.DRAFT,
        tier=AlphaTier.TIER_2,
    )

    def update(self, bid_qty: float) -> float:
        raise ValueError("boom")

    def reset(self) -> None:
        pass

    def get_signal(self) -> float:
        return 0.0


def test_dispatcher_uses_per_symbol_alpha_instances() -> None:
    dispatcher = AlphaDispatcher()
    dispatcher._alpha_classes = {"counter": _CounterAlpha}
    dispatcher._weights = {}

    first = _symbol_state(("counter",))
    second = _symbol_state(("counter",))
    dispatcher.bind_symbol(first)
    dispatcher.bind_symbol(second)

    dispatcher.dispatch(first, {"bid_qty": 6.0, "ask_qty": 5.0})
    dispatcher.dispatch(second, {"bid_qty": 3.0, "ask_qty": 5.0})

    assert first.alpha_states["counter"].signal == 1.0
    assert second.alpha_states["counter"].signal == -2.0


def test_dispatcher_falls_back_to_manifest_fields_and_weights_composite() -> None:
    dispatcher = AlphaDispatcher()
    dispatcher._alpha_classes = {"filtered": _FilteredAlpha}
    dispatcher._weights = {"qi": 0.1, "fmd": 0.1}

    state = _symbol_state(("filtered",))
    dispatcher.bind_symbol(state)
    dispatcher.dispatch(
        state,
        {
            "bid_qty": 8.0,
            "ask_qty": 3.0,
            "mid_price": 100.0,
            "spread_scaled": 2,
            "microprice_x2": 4,
        },
    )

    assert state.alpha_states["filtered"].signal == 5.0

    state.alpha_states = {
        "qi": AlphaState(alpha_id="qi", signal=1.5, z_score=1.5),
        "fmd": AlphaState(alpha_id="fmd", signal=0.8, z_score=0.8),
    }
    dispatcher._update_composite(state)

    assert math.isclose(state.composite, 1.15, rel_tol=1e-9)


def test_dispatcher_isolates_errors_and_disables_after_threshold() -> None:
    dispatcher = AlphaDispatcher()
    dispatcher._alpha_classes = {
        "broken": _BrokenAlpha,
        "counter": _CounterAlpha,
    }
    dispatcher._weights = {}

    state = _symbol_state(("broken", "counter"))
    dispatcher.bind_symbol(state)

    for _ in range(10):
        dispatcher.dispatch(state, {"bid_qty": 10.0, "ask_qty": 8.0})

    broken = state.alpha_states["broken"]
    healthy = state.alpha_states["counter"]

    assert broken.disabled is True
    assert math.isnan(broken.signal)
    assert healthy.disabled is False
    assert healthy.signal == 20.0
