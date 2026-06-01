"""C72 — TMFD6 Queue-Position-Aware Maker.

R5 T1 CONDITIONAL PROCEED (2026-04-19). Overlay on C60 (PROMOTED TMFD6
R47-minimal, R1): quote only when near-side L1 queue is thin.

### Observability resolution (per Researcher T1 concern #3)

Real "self-queue-position" is simulation-internal (our own arrival-rank).
Using it would re-introduce the PowerProb 14x pessimism that R47 SKILL
empirically DISABLED. Instead, C72 uses a **CK-observable proxy**: gate
on top-of-book QUEUE-DEPTH (bid_qty for buy-side, ask_qty for sell-side).
A thin near-side queue proxies "if we were there, we'd be near top".

Note: Lipton-PSS (2013, arXiv:1312.0514) shows thin near-side queue
correlates with adverse price movement AWAY from that side. So the gate
is ambiguous — could be either high-priority quoting (good) or
adverse-selection (bad). T5 is the arbiter.

### Mechanism

C60 baseline preserved:
  - spread_threshold_pts = 5
  - max_pos = 2 canonical
  - inventory_skew_tenths = 2 (linear, non-|pos|-gated)
  - qi_skew_threshold = 0.10, qi_skew_widen_ticks = 1, enable_qi_layer = True
  - D1/D2/D3 DISABLED

NEW layer (C72 overlay):
  - queue_depth_max_bid: only quote BUY when bid_qty <= threshold
  - queue_depth_max_ask: only quote SELL when ask_qty <= threshold
  - Independent per-side (avoid ganging both sides at once).
  - Default thresholds configurable; T5 sweeps {2, 5, 10, 20}.

Non-|pos|-gated — gate is on OBSERVABLE L1 queue depth, NOT |pos|.

### Cost citation

`shared-context.yaml#cost_model.TMF`
  rt_cost_pts: 1.5 (inst est.; confirmed=false)
  point_value_ntd: 10

Any PROMOTE MUST carry `requires_broker_confirmation_before_live: true`.

### Dominance risk (T1 flag #1)

T1 scenario analysis showed C72 PnL <= C60 baseline at +/-30% RT under
+30% per-trip edge. T5 must report:
  - per-trip edge conditional on queue-thin gate
  - retention rate (C72 fills / C60 fills)
  - net PnL at inst RT
and explicitly compare to C60 baseline to confirm or reject dominance.

Research-module float exception (Rule 11). Scaled-int math throughout.
"""

from __future__ import annotations

from dataclasses import dataclass

from structlog import get_logger

from research.backtest.maker_engine import (
    CancelQuote,
    Hold,
    PostQuote,
    TickData,
)
from research.registry.schemas import (
    AlphaManifest,
    AlphaStatus,
    AlphaTier,
)

logger = get_logger("alpha.c72_tmfd6_queue_position_aware")

_TMF_POINT_VALUE_NTD = 10
_TMF_INST_RT_COST_PTS = 1.5
_TMF_RETAIL_RT_COST_PTS = 4.0

# Same as C60: D1/D2/D3 hard-disabled; D4 QI retained (deployed TMFD6 config).
_DISABLED_SIGNAL_LAYERS_MOST = (
    "_update_pe",
    "_update_queue",
    "_update_mfg",
    "compute_pe_gate",
    "compute_queue_suppress",
    "compute_mfg_skew",
)


# ----------------------------------------------------------------------------
# Parameters
# ----------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class C72Params:
    """Tuning parameters for C72 (TMFD6 queue-position-aware maker).

    Inherits C60's R47-minimal params; adds queue-depth gate.

    spread_threshold_pts: int = 5
        TMFD6 baseline (same as C60).
    max_pos: int = 2
        Canonical (same as C60 PROMOTE).
    inventory_skew_tenths: int = 2
        Linear skew (same as C60).
    qi_skew_threshold: float = 0.10
        D4 QI threshold (same as C60).
    qi_skew_widen_ticks: int = 1
        D4 QI widen (same as C60).
    enable_qi_layer: bool = True
        D4 QI retained (same as C60).
    enable_pe_layer / enable_queue_layer / enable_mfg_layer: bool = False
        R47-minimal (same as C60).
    enable_queue_depth_gate: bool = True
        C72 new layer: gate quoting on L1 queue depth.
    queue_depth_max_bid: int = 5
        Max bid_qty to admit buy-side quote. Lower = stricter gate.
    queue_depth_max_ask: int = 5
        Max ask_qty to admit sell-side quote.
    """

    spread_threshold_pts: int = 5
    max_pos: int = 2
    inventory_skew_tenths: int = 2
    qi_skew_threshold: float = 0.10
    qi_skew_widen_ticks: int = 1
    enable_qi_layer: bool = True
    enable_pe_layer: bool = False
    enable_queue_layer: bool = False
    enable_mfg_layer: bool = False
    # C72-specific
    enable_queue_depth_gate: bool = True
    queue_depth_max_bid: int = 5
    queue_depth_max_ask: int = 5


# ----------------------------------------------------------------------------
# TmfD6QueuePositionAwareMaker — MakerEngine-compatible strategy
# ----------------------------------------------------------------------------


class TmfD6QueuePositionAwareMaker:
    """R47-minimal with queue-depth gate for TMFD6 day session.

    API mirrors C33 / C60:
      - ``on_tick(TickData)`` -> list[PostQuote | CancelQuote | Hold]
      - ``on_fill(side, price, mid)`` - updates local pos
      - ``on_gap()`` - clears transient quote state
    """

    __slots__ = (
        "_params",
        "_position",
        "_last_bid",
        "_last_ask",
        "_tick_count",
        "_spread_blocked",
        "_max_pos_blocked",
        "_qi_widen_events",
        "_queue_depth_blocked_bid",
        "_queue_depth_blocked_ask",
        "_quotes_posted",
        "_active_symbol",
    )

    def __init__(
        self,
        params: C72Params | None = None,
        active_symbol: str = "TMFD6",
    ) -> None:
        self._params = params or C72Params()
        self._position = 0
        self._last_bid: int | None = None
        self._last_ask: int | None = None
        self._tick_count = 0
        self._spread_blocked = 0
        self._max_pos_blocked = 0
        self._qi_widen_events = 0
        self._queue_depth_blocked_bid = 0
        self._queue_depth_blocked_ask = 0
        self._quotes_posted = 0
        self._active_symbol = active_symbol

    # ---- Observability ---------------------------------------------------

    @property
    def position(self) -> int:
        return self._position

    @property
    def params(self) -> C72Params:
        return self._params

    @property
    def tick_count(self) -> int:
        return self._tick_count

    @property
    def spread_blocked(self) -> int:
        return self._spread_blocked

    @property
    def max_pos_blocked(self) -> int:
        return self._max_pos_blocked

    @property
    def qi_widen_events(self) -> int:
        return self._qi_widen_events

    @property
    def queue_depth_blocked_bid(self) -> int:
        return self._queue_depth_blocked_bid

    @property
    def queue_depth_blocked_ask(self) -> int:
        return self._queue_depth_blocked_ask

    @property
    def quotes_posted(self) -> int:
        return self._quotes_posted

    @property
    def active_symbol(self) -> str:
        return self._active_symbol

    # ---- MakerStrategy protocol -----------------------------------------

    def on_tick(
        self, tick: TickData
    ) -> list[PostQuote | CancelQuote | Hold]:
        self._tick_count += 1
        if tick.is_trade:
            return [Hold()]
        return self._on_bidask(tick)

    def _on_bidask(
        self, tick: TickData
    ) -> list[PostQuote | CancelQuote | Hold]:
        params = self._params
        if tick.bid_price <= 0 or tick.ask_price <= 0:
            return [Hold()]
        if tick.ask_price <= tick.bid_price:
            return [Hold()]

        scale = tick.scale
        spread_raw = tick.ask_price - tick.bid_price
        spread_pts = spread_raw // scale
        if spread_pts < params.spread_threshold_pts:
            self._spread_blocked += 1
            return [Hold()]

        # L2 D4 QI skew (same as C60)
        widen_bid_ticks, widen_ask_ticks = self._compute_qi_skew(tick)
        if widen_bid_ticks or widen_ask_ticks:
            self._qi_widen_events += 1

        # C72 queue-depth gate (NEW). Independent per-side.
        buy_depth_ok, sell_depth_ok = self._queue_depth_gate_check(tick)

        actions: list[PostQuote | CancelQuote | Hold] = []
        pos = self._position
        max_pos = params.max_pos

        # Linear inventory skew (same as C60).
        skew = (pos * params.inventory_skew_tenths * scale) // 10
        tick_size = scale
        bid_quote = tick.bid_price - skew - widen_bid_ticks * tick_size
        ask_quote = tick.ask_price - skew + widen_ask_ticks * tick_size

        bid_moved = bid_quote != self._last_bid
        ask_moved = ask_quote != self._last_ask

        # Buy side: also require queue-depth gate.
        if pos < max_pos and bid_moved and buy_depth_ok:
            actions.append(PostQuote(side="buy", price=bid_quote, qty=1))
            self._last_bid = bid_quote
            self._quotes_posted += 1
        elif pos >= max_pos:
            self._max_pos_blocked += 1

        # Sell side: also require queue-depth gate.
        if pos > -max_pos and ask_moved and sell_depth_ok:
            actions.append(PostQuote(side="sell", price=ask_quote, qty=1))
            self._last_ask = ask_quote
            self._quotes_posted += 1

        return actions or [Hold()]

    def _compute_qi_skew(self, tick: TickData) -> tuple[int, int]:
        """D4 QI skew (inherited from C60 pattern)."""
        params = self._params
        if not params.enable_qi_layer:
            return 0, 0
        total_qty = tick.bid_qty + tick.ask_qty
        if total_qty <= 0:
            return 0, 0
        imbalance = (tick.bid_qty - tick.ask_qty) / total_qty
        threshold = params.qi_skew_threshold
        widen = params.qi_skew_widen_ticks
        if imbalance > threshold:
            return 0, widen
        if imbalance < -threshold:
            return widen, 0
        return 0, 0

    def _queue_depth_gate_check(self, tick: TickData) -> tuple[bool, bool]:
        """C72 NEW layer: gate each side on L1 near-side queue depth.

        Returns (buy_side_ok, sell_side_ok).

        Thin queue => gate OPEN (quote admitted).
        Thick queue => gate CLOSED (increment blocked counter).

        Disabled case: both gates always open.
        """
        params = self._params
        if not params.enable_queue_depth_gate:
            return True, True

        # Buy-side: we would post at the bid; the near-side queue is bid_qty.
        buy_ok = tick.bid_qty <= params.queue_depth_max_bid
        if not buy_ok:
            self._queue_depth_blocked_bid += 1
        # Sell-side: we would post at the ask; near-side queue is ask_qty.
        sell_ok = tick.ask_qty <= params.queue_depth_max_ask
        if not sell_ok:
            self._queue_depth_blocked_ask += 1
        return buy_ok, sell_ok

    def on_fill(self, side: str, price: int, mid_price: float) -> None:
        if side == "buy":
            self._position += 1
            self._last_bid = None
        elif side == "sell":
            self._position -= 1
            self._last_ask = None
        else:
            logger.warning("c72_unknown_fill_side", side=side)

    def on_gap(self) -> None:
        self._last_bid = None
        self._last_ask = None

    def reset(self) -> None:
        self._position = 0
        self._last_bid = None
        self._last_ask = None
        self._tick_count = 0
        self._spread_blocked = 0
        self._max_pos_blocked = 0
        self._qi_widen_events = 0
        self._queue_depth_blocked_bid = 0
        self._queue_depth_blocked_ask = 0
        self._quotes_posted = 0


# ----------------------------------------------------------------------------
# AlphaProtocol shim
# ----------------------------------------------------------------------------


class C72Alpha:
    """AlphaProtocol wrapper for registry smoke-path."""

    __slots__ = ("_maker", "_manifest", "_last_signal")

    def __init__(
        self,
        params: C72Params | None = None,
        active_symbol: str = "TMFD6",
    ) -> None:
        self._maker = TmfD6QueuePositionAwareMaker(
            params=params, active_symbol=active_symbol
        )
        self._last_signal = 0.0
        self._manifest = AlphaManifest(
            alpha_id="c72_tmfd6_queue_position_aware",
            hypothesis=(
                "Overlay on C60 (PROMOTED TMFD6 R47-minimal): quote only "
                "when near-side L1 queue depth is below threshold "
                "(CK-observable proxy for self-queue-position near top). "
                "Non-|pos|-gated — gate is on L1 depth, not |pos|. "
                "Dominance-risk flagged per Researcher T1: C72 must show "
                "per-trip edge improvement exceeding fill-retention loss "
                "to beat C60 baseline. T5 is the arbiter."
            ),
            formula=(
                "R47-minimal + D4 QI (C60 baseline) gated on L1 depth: "
                "quote buy when bid_qty <= queue_depth_max_bid; quote sell "
                "when ask_qty <= queue_depth_max_ask. Default thresholds 5 "
                "per side; T5 sweeps {2, 5, 10, 20}. D1/D2/D3 DISABLED."
            ),
            paper_refs=(
                "r47_maker_strategy",
                "r47_structural_properties",
                "c60_tmfd6_r47_minimal_inst_rt",
                "shared-context_2026-04-19_cost_model",
                "memory/backtest_method_reliability",
                "1312.0514v1_Lipton_Pesavento_Sotiropoulos_2013_quote_imbalance",
                "1903.07222v4_Law_Viens_2019_MM_weakly_consistent_LOB",
                "1806.05101v1_Lu_Abergel_2018_LOB_MM",
                "2510.27334v1_Jafree_2025_adverse_selection_meta_orders_RL",
            ),
            data_fields=(
                "bid_price",
                "ask_price",
                "bid_qty",
                "ask_qty",
                "trade_price",
                "trade_volume",
                "trade_direction",
                "mid_price",
                "spread_pts",
                "bid_depth_l1",
                "ask_depth_l1",
            ),
            complexity="O(1)",
            status=AlphaStatus.PROTOTYPE,
            tier=AlphaTier.TIER_1,
            rust_module=None,
            latency_profile="shioaji_sim_p95_v2026-03-04",
            roles_used=("architect", "code-reviewer"),
            skills_used=("hft-backtester",),
            feature_set_version=None,
            strategy_type="maker",
            instrument="TMFD6",
        )

    @property
    def manifest(self) -> AlphaManifest:
        return self._manifest

    @property
    def maker(self) -> TmfD6QueuePositionAwareMaker:
        return self._maker

    def update(self, *args: object, **kwargs: object) -> float:
        return self._last_signal

    def reset(self) -> None:
        self._maker.reset()
        self._last_signal = 0.0

    def get_signal(self) -> float:
        return self._last_signal


__all__ = [
    "C72Alpha",
    "C72Params",
    "TmfD6QueuePositionAwareMaker",
    "_DISABLED_SIGNAL_LAYERS_MOST",
    "_TMF_POINT_VALUE_NTD",
    "_TMF_INST_RT_COST_PTS",
    "_TMF_RETAIL_RT_COST_PTS",
]
