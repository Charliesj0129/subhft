"""C27 — Vol-percentile amplification on C14 TXF front-month maker.

Mechanism (per R14-T1 §0/§1):
  Baseline C14 maker with ``max_pos=3``. When within-day 1-min realized-
  vol percentile > P90 (hysteresis release at P70), switch to
  ``max_pos=4`` for the duration of the high-vol minute. Release on
  percentile falling below P70.

Implementation strategy:
  Compose (not subclass) ``TxfFrontMonthMaker`` from C14. Forward the
  MakerStrategy protocol. On every bidask tick, update the vol gate
  and mutate the wrapped maker's ``_params.max_pos`` BEFORE its own
  tick processing. Revert on gate release.

No modifications to C14, R47, or R6 backtest code. Uses `frozen=True,
slots=True` C14Params — mutation is via `object.__setattr__` through a
helper `_set_max_pos()` that replaces the whole params object (because
C14Params is frozen).

Interfaces:
  - ``C27VolAmplifiedMaker`` conforms to ``research.backtest.maker_engine.MakerStrategy``.
  - ``C27Alpha`` conforms to ``research.registry.schemas.AlphaProtocol``.
"""

from __future__ import annotations

from dataclasses import dataclass

from structlog import get_logger

from research.alphas.c14_txf_frontmonth_native_maker.impl import (
    C14Params,
    TxfFrontMonthMaker,
)
from research.alphas.c27_vol_amplification_on_c14.vol_gate import VolPercentileGate
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

logger = get_logger("alpha.c27_vol_amplification_on_c14")


@dataclass(frozen=True, slots=True)
class C27Params:
    """Tuning parameters for C27.

    Inherits C14's baseline config + adds vol-amplification knobs.
    """

    # C14 baseline
    spread_threshold_pts: int = 3
    max_pos_baseline: int = 3
    max_pos_amplified: int = 4
    inventory_skew_tenths: int = 2
    # Vol gate
    vol_percentile_threshold: float = 0.90
    vol_percentile_release: float = 0.70
    vol_window_seconds: int = 60  # informational — the gate uses minute buckets
    warmup_minutes: int = 10


class C27VolAmplifiedMaker:
    """Vol-amplification modulator wrapping C14's TxfFrontMonthMaker.

    Maintains its own ``VolPercentileGate`` + delegates every other
    decision to an owned ``TxfFrontMonthMaker``. Mutates the wrapped
    maker's max_pos by rebuilding the params on gate state-change.
    """

    __slots__ = (
        "_c27_params",
        "_maker",
        "_vol_gate",
        "_state_amplified",
        "_amplified_ticks",
        "_baseline_ticks",
        "_state_switches",
    )

    def __init__(
        self,
        c27_params: C27Params | None = None,
        active_symbol: str | None = None,
    ) -> None:
        self._c27_params = c27_params or C27Params()
        p = self._c27_params
        # Start in baseline state.
        baseline = C14Params(
            spread_threshold_pts=p.spread_threshold_pts,
            max_pos=p.max_pos_baseline,
            inventory_skew_tenths=p.inventory_skew_tenths,
        )
        self._maker = TxfFrontMonthMaker(
            params=baseline, active_symbol=active_symbol
        )
        self._vol_gate = VolPercentileGate(
            threshold_high=p.vol_percentile_threshold,
            threshold_low=p.vol_percentile_release,
            warmup_minutes=p.warmup_minutes,
        )
        self._state_amplified = False
        self._amplified_ticks = 0
        self._baseline_ticks = 0
        self._state_switches = 0

    # ---- Accessors --------------------------------------------------------

    @property
    def active_symbol(self) -> str | None:
        return self._maker.active_symbol

    @property
    def position(self) -> int:
        return self._maker.position

    @property
    def state_amplified(self) -> bool:
        return self._state_amplified

    @property
    def current_max_pos(self) -> int:
        return self._maker._params.max_pos  # type: ignore[attr-defined]

    @property
    def vol_gate(self) -> VolPercentileGate:
        return self._vol_gate

    @property
    def amplified_ticks(self) -> int:
        return self._amplified_ticks

    @property
    def baseline_ticks(self) -> int:
        return self._baseline_ticks

    @property
    def state_switches(self) -> int:
        return self._state_switches

    # ---- Rollover delegation ---------------------------------------------

    def set_active_symbol(self, new_symbol: str) -> None:
        # Rollover also resets vol-gate: new contract's tick stream is a
        # fresh session from the strategy's perspective.
        self._maker.set_active_symbol(new_symbol)
        self._vol_gate.reset()
        self._apply_state(False)

    def flatten_position(self) -> int:
        return self._maker.flatten_position()

    @property
    def rollover_events(self) -> int:
        return self._maker.rollover_events

    # ---- MakerStrategy protocol -----------------------------------------

    def on_tick(
        self, tick: TickData
    ) -> list[PostQuote | CancelQuote | Hold]:
        if tick.is_trade:
            # Trades don't drive quoting; pass through.
            return self._maker.on_tick(tick)

        # Update vol gate on bidask events using mid = (bid+ask)/2.
        if tick.bid_price > 0 and tick.ask_price > 0:
            mid = (tick.bid_price + tick.ask_price) / 2.0
            new_state = self._vol_gate.update(tick.exch_ts, mid)
            if new_state != self._state_amplified:
                self._apply_state(new_state)

        if self._state_amplified:
            self._amplified_ticks += 1
        else:
            self._baseline_ticks += 1

        return self._maker.on_tick(tick)

    def on_fill(self, side: str, price: int, mid_price: float) -> None:
        self._maker.on_fill(side, price, mid_price)

    def on_gap(self) -> None:
        """Clear transient state on bus overflow. Vol histogram wiped too."""
        self._maker.on_gap()
        self._vol_gate.reset()
        self._apply_state(False)

    # ---- Internal helpers -----------------------------------------------

    def _apply_state(self, new_amplified: bool) -> None:
        """Switch the wrapped maker's max_pos based on amplified flag."""
        if new_amplified == self._state_amplified and self.current_max_pos == self._target_max_pos(new_amplified):
            return
        target = self._target_max_pos(new_amplified)
        new_params = C14Params(
            spread_threshold_pts=self._c27_params.spread_threshold_pts,
            max_pos=target,
            inventory_skew_tenths=self._c27_params.inventory_skew_tenths,
        )
        # C14Params is frozen — replace the whole params object on the wrapped maker.
        self._maker._params = new_params  # type: ignore[attr-defined]
        if new_amplified != self._state_amplified:
            logger.info(
                "c27_state_switch",
                amplified=new_amplified,
                max_pos=target,
                completed_minutes=self._vol_gate.completed_minutes,
            )
            self._state_switches += 1
        self._state_amplified = new_amplified

    def _target_max_pos(self, amplified: bool) -> int:
        return (
            self._c27_params.max_pos_amplified
            if amplified
            else self._c27_params.max_pos_baseline
        )


class C27Alpha:
    """AlphaProtocol wrapper around C27VolAmplifiedMaker (registry smoke-path)."""

    __slots__ = ("_maker", "_manifest", "_last_signal")

    def __init__(
        self,
        params: C27Params | None = None,
        active_symbol: str | None = None,
    ) -> None:
        self._maker = C27VolAmplifiedMaker(
            c27_params=params, active_symbol=active_symbol
        )
        self._last_signal = 0.0
        self._manifest = AlphaManifest(
            alpha_id="c27_vol_amplification_on_c14",
            hypothesis=(
                "C14 maker's most-profitable round-trips concentrate in "
                "high within-day vol minutes (P95 bucket: +28.20 pt/trip "
                "OOS vs +5.98 pt P20 bucket, ratio 4.7×). Amplify "
                "exposure during P90+ minutes by raising max_pos from 3 "
                "to 4, hysteresis release at P70. Inverted mechanism "
                "vs R7 C13 (which killed the same gate used as a "
                "DISABLE trigger). Modulator on C14, not standalone."
            ),
            formula=(
                "on each bidask: update 1-min realized-vol percentile; "
                "if pct > 0.90 set maker.max_pos=4, if pct < 0.70 set "
                "maker.max_pos=3. All other C14 logic unchanged."
            ),
            paper_refs=(
                "r47_maker_strategy",
                "r47_structural_properties",
                "c14_txf_frontmonth_native_maker",
                "c13_vol_gate_disable_R7_kill",
            ),
            data_fields=(
                "bid_px",
                "ask_px",
                "bid_qty",
                "ask_qty",
                "mid_price",
                "trade_price",
                "trade_volume",
            ),
            complexity="O(1) per tick (amortised; vol histogram is bounded)",
            status=AlphaStatus.PROTOTYPE,
            tier=AlphaTier.TIER_1,
            rust_module=None,
            latency_profile="sim_p95_v2026-02-26",
            roles_used=("architect", "code-reviewer"),
            skills_used=("hft-backtester",),
            feature_set_version=None,
            strategy_type="maker",
            instrument="TXF_frontmonth",
        )

    @property
    def manifest(self) -> AlphaManifest:
        return self._manifest

    @property
    def maker(self) -> C27VolAmplifiedMaker:
        return self._maker

    def update(self, *args: object, **kwargs: object) -> float:
        return self._last_signal

    def reset(self) -> None:
        self._maker = C27VolAmplifiedMaker(
            c27_params=self._maker._c27_params,  # type: ignore[attr-defined]
            active_symbol=None,
        )
        self._last_signal = 0.0

    def get_signal(self) -> float:
        return self._last_signal


__all__ = [
    "C27Alpha",
    "C27Params",
    "C27VolAmplifiedMaker",
]
