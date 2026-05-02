"""C32b — TOB-survival refresh regime gate, a refresh-cadence modulator on R47.

R47 (deployed TMFD6 maker) cancels-and-reposts its quote on every L1 update.
On days where the top-of-book is stable (high survival time, low information
content per LOB event), this behavior destroys queue priority without
information benefit. C32b preserves queue priority across non-informative
events during a measured high-survival regime.

Mechanism
---------
- **Regime classifier**: rolling 5-minute median of per-minute median TOB
  survival in ms (`tob_roll5_med_ms`). Gate is active when
  `tob_roll5_med_ms > 200 ms`.
- **Modulator hook**: `should_delay_refresh(event) -> bool`. Returns True only
  when ALL of:
  1. regime is active,
  2. the incoming LOB update does NOT move mid by > 0.5 tick,
  3. the current hold has not exceeded the 250 ms cap,
  4. the regime has not flipped inactive since the hold began.
  Returns False otherwise (release: the caller should proceed with its normal
  cancel-and-repost).

Design invariants
-----------------
- **Incremental cost = 0** (verified physically):
  - The modulator NEVER issues new `place_order` or `cancel_order` calls —
    it ONLY answers "should you delay the refresh?" to the caller.
  - A True return causes FEWER cancel messages; a False return yields
    baseline behavior.
  - No mutation of R47 order price / qty / side / max_pos.
- **Threshold 200 ms is FROZEN** (IS-selected, DA-validated; no retuning).
- **Max hold 250 ms is FROZEN** (upper bound; release fires on mid-move,
  regime flip, or timeout whichever first).
- **R47 signal layers unchanged** (PE / queue / MFG stay at their R47
  structural-minimal defaults).

Research-module float exception (Rule 11 of 25-architecture-governance):
this module is offline / CLI-invoked research code. Prices are scaled
integers from CK (default scale 1e6); millisecond arithmetic is int math.
Timestamps are treated as monotonic ns supplied by the caller; the module
never calls `datetime.now()`.

Cost citation: `memory/feedback_taifex_fee_structure.md` (TMF RT=4pt,
user-confirmed 2026-03-26). C32b adds ZERO incremental RT.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass

from structlog import get_logger

from research.registry.schemas import (
    AlphaManifest,
    AlphaStatus,
    AlphaTier,
)

logger = get_logger("alpha.c32b_tob_survival_refresh_regime_gate")


# ----------------------------------------------------------------------------
# Parameters
# ----------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class C32bParams:
    """Tuning parameters for C32b (all FROZEN except window sizes).

    tob_median_threshold_ms
        Gate-active threshold: regime is "high TOB survival" when
        `tob_roll5_med_ms > tob_median_threshold_ms`. Default 200 is
        IS-selected per the R6 T1 sweep (valid range [150, 300] ms;
        picked as max avg_separation subject to trigger∈[5%, 50%]).
        **FROZEN** (DA-validated; do not retune).
    max_delay_hold_ms
        Upper bound on how long the modulator will hold a single quote
        between refreshes. Default 250 is the role-template-prescribed
        cap; if exceeded, `should_delay_refresh` returns False to force
        a refresh regardless of regime state.
        **FROZEN**.
    mid_move_half_tick_scale
        Scaled-integer threshold representing 0.5 tick in price-scaled
        units. Mid move above this forces a refresh. Default equals
        0.5 × 10_000 = 5_000 (platform x10_000 scale). When running on
        CK data (scale x1_000_000), the caller supplies the scale via
        `scale` arg to `should_delay_refresh`, and the half-tick threshold
        is adjusted: `half_tick = 0.5 * scale / (scale // PLATFORM_SCALE)`.
        Simpler: supply `scale` at call time and we compute the half-tick
        internally from `tick_size_pts` = 1 pt.
    tick_size_pts
        Tick size in points (TMFD6 and TXFD6 both = 1 pt). Used together
        with the incoming `scale` to derive the half-tick threshold.
    tob_window_minutes
        Rolling window (minutes) for the regime classifier. Default 5.
    min_events_per_minute
        Minimum number of TOB-change events required per minute for a
        minute to contribute to the rolling median. Default 20 matches
        the Researcher T1 sim.
    """

    tob_median_threshold_ms: int = 200
    max_delay_hold_ms: int = 250
    tick_size_pts: int = 1
    tob_window_minutes: int = 5
    min_events_per_minute: int = 20


# ----------------------------------------------------------------------------
# Event shape — minimal structural LOB event the modulator consumes.
# ----------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class LobRefreshEvent:
    """Minimal LOB-update event consumed by the modulator.

    exch_ts_ns: exchange timestamp in nanoseconds (monotonic).
    bid_price / ask_price: scaled-integer top-of-book.
    scale: price scale factor (e.g. 1_000_000 for CK, 10_000 for platform).
    tob_changed: True iff this event changed the best-bid or best-ask
        price level (used to accumulate TOB survival samples).
    """

    exch_ts_ns: int
    bid_price: int
    ask_price: int
    scale: int
    tob_changed: bool


# ----------------------------------------------------------------------------
# _RollingTobSurvivalTracker — 5-minute rolling median of per-minute median
# TOB survival (in ms). Samples are (minute_idx, median_ms); minutes with
# fewer than `min_events_per_minute` TOB changes are skipped (insufficient
# resolution for a robust median).
# ----------------------------------------------------------------------------


class _RollingTobSurvivalTracker:
    """Per-minute median TOB survival → rolling 5-min median regime flag."""

    __slots__ = (
        "_tob_window_minutes",
        "_min_events_per_minute",
        "_current_minute",
        "_current_survivals_ms",
        "_prev_tob_change_ts_ns",
        "_per_minute_medians",
        "_last_roll_median_ms",
    )

    def __init__(
        self,
        tob_window_minutes: int = 5,
        min_events_per_minute: int = 20,
    ) -> None:
        self._tob_window_minutes = tob_window_minutes
        self._min_events_per_minute = min_events_per_minute
        self._current_minute: int = -1
        self._current_survivals_ms: list[int] = []
        self._prev_tob_change_ts_ns: int | None = None
        # Rolling window of per-minute medians (ms). Deque auto-discards old.
        self._per_minute_medians: deque[float] = deque(maxlen=tob_window_minutes)
        self._last_roll_median_ms: float = 0.0

    def feed(self, event: LobRefreshEvent) -> float:
        """Update the tracker with a LOB event; return current 5-min roll median (ms).

        Only TOB-changing events contribute a survival sample.
        """
        if not event.tob_changed:
            return self._last_roll_median_ms

        minute_idx = event.exch_ts_ns // 60_000_000_000

        if self._prev_tob_change_ts_ns is not None:
            survival_ns = event.exch_ts_ns - self._prev_tob_change_ts_ns
            # Negative delta is a monotonicity violation; skip.
            if survival_ns >= 0:
                survival_ms = survival_ns // 1_000_000
                # Still in the same minute: append to running list.
                if minute_idx == self._current_minute:
                    self._current_survivals_ms.append(int(survival_ms))
                else:
                    # Minute boundary crossed: finalize previous minute first.
                    self._finalize_minute()
                    self._current_minute = minute_idx
                    self._current_survivals_ms = [int(survival_ms)]
        else:
            # First ever TOB change — open the current minute bucket.
            self._current_minute = minute_idx
            self._current_survivals_ms = []

        self._prev_tob_change_ts_ns = event.exch_ts_ns
        # Compute a fresh rolling median reflecting whatever minutes are
        # already finalized; the in-progress minute doesn't contribute
        # until it is closed.
        self._last_roll_median_ms = self._compute_roll_median()
        return self._last_roll_median_ms

    def _finalize_minute(self) -> None:
        """Close the current minute: compute its median and append to window."""
        samples = self._current_survivals_ms
        if len(samples) < self._min_events_per_minute:
            return
        sorted_samples = sorted(samples)
        n = len(sorted_samples)
        if n % 2 == 1:
            median = float(sorted_samples[n // 2])
        else:
            median = (sorted_samples[n // 2 - 1] + sorted_samples[n // 2]) / 2.0
        self._per_minute_medians.append(median)

    def _compute_roll_median(self) -> float:
        if not self._per_minute_medians:
            return 0.0
        sorted_w = sorted(self._per_minute_medians)
        n = len(sorted_w)
        if n % 2 == 1:
            return float(sorted_w[n // 2])
        return (sorted_w[n // 2 - 1] + sorted_w[n // 2]) / 2.0

    @property
    def roll_median_ms(self) -> float:
        return self._last_roll_median_ms


# ----------------------------------------------------------------------------
# C32bModulator — the refresh-delay hook
# ----------------------------------------------------------------------------


class C32bModulator:
    """R47 refresh-cadence modulator. Exposes `should_delay_refresh(event)`.

    State kept across calls:
      - `_tracker`: rolling 5-min TOB-survival median.
      - `_hold_start_ns`: timestamp at which the current hold began; None if
        no hold is in progress.
      - `_hold_anchor_mid_scaled`: mid price (scaled int) at the start of
        the current hold; a mid move of > 0.5 tick vs this anchor releases
        the hold.
      - `_hold_anchor_regime_active`: True iff the regime was active when
        the hold began; if it flips inactive during the hold, release.
    """

    __slots__ = (
        "_params",
        "_tracker",
        "_hold_start_ns",
        "_hold_anchor_mid_scaled",
        "_hold_anchor_regime_active",
        "_delay_count",
        "_release_count",
    )

    def __init__(self, params: C32bParams | None = None) -> None:
        self._params = params or C32bParams()
        self._tracker = _RollingTobSurvivalTracker(
            tob_window_minutes=self._params.tob_window_minutes,
            min_events_per_minute=self._params.min_events_per_minute,
        )
        self._hold_start_ns: int | None = None
        self._hold_anchor_mid_scaled: int | None = None
        self._hold_anchor_regime_active: bool = False
        self._delay_count = 0
        self._release_count = 0

    @property
    def params(self) -> C32bParams:
        return self._params

    @property
    def tracker(self) -> _RollingTobSurvivalTracker:
        return self._tracker

    @property
    def delay_count(self) -> int:
        return self._delay_count

    @property
    def release_count(self) -> int:
        return self._release_count

    @property
    def hold_in_progress(self) -> bool:
        return self._hold_start_ns is not None

    # ---- Main hook --------------------------------------------------------

    def should_delay_refresh(self, event: LobRefreshEvent) -> bool:
        """Return True iff the caller should HOLD the current quote.

        Semantics:
          - True: the caller should NOT cancel-and-repost for this event.
          - False: release — caller should proceed with baseline refresh.

        This hook is pure in the sense that it NEVER places or cancels
        orders directly; it answers a binary question. All order-side
        actions remain in the caller's (R47) hands.

        Releases (False) when any of:
          - regime not active (tob_roll5_med <= threshold),
          - mid moved by > 0.5 tick vs the hold-anchor mid,
          - hold duration exceeds `max_delay_hold_ms`,
          - regime flipped inactive since the hold began.

        Holds (True) when ALL of:
          - regime active,
          - mid did NOT move by > 0.5 tick,
          - hold duration within cap,
          - regime was active at the start of the current hold (no flip).
        """
        # 1) Update the regime tracker (only TOB-changing events contribute).
        roll_ms = self._tracker.feed(event)
        regime_active = roll_ms > self._params.tob_median_threshold_ms

        # 2) Validate book; degenerate books release (pass through).
        if event.bid_price <= 0 or event.ask_price <= 0:
            return self._release()
        if event.ask_price <= event.bid_price:
            return self._release()

        mid_scaled = (event.bid_price + event.ask_price) // 2
        half_tick_scaled = (event.scale * self._params.tick_size_pts) // 2

        # 3) Regime inactive — cannot delay. Release any in-progress hold.
        if not regime_active:
            return self._release()

        # 4) Regime active and no hold in progress: open a new hold and
        #    signal delay for this event.
        if self._hold_start_ns is None:
            self._open_hold(event.exch_ts_ns, mid_scaled, regime_active)
            self._delay_count += 1
            return True

        # 5) A hold is in progress — evaluate release conditions.
        # 5a) Regime flipped inactive after hold started → release.
        if not self._hold_anchor_regime_active:
            # Defensive; should match regime_active branch above.
            return self._release()

        # 5b) Mid moved by > 0.5 tick vs anchor → release.
        if self._hold_anchor_mid_scaled is not None:
            mid_delta = abs(mid_scaled - self._hold_anchor_mid_scaled)
            if mid_delta > half_tick_scaled:
                return self._release()

        # 5c) Hold duration exceeded cap → release.
        elapsed_ms = (event.exch_ts_ns - self._hold_start_ns) // 1_000_000
        if elapsed_ms > self._params.max_delay_hold_ms:
            return self._release()

        # 6) All guards pass: continue the hold.
        self._delay_count += 1
        return True

    # ---- Helpers ----------------------------------------------------------

    def _open_hold(
        self,
        now_ns: int,
        mid_scaled: int,
        regime_active: bool,
    ) -> None:
        self._hold_start_ns = now_ns
        self._hold_anchor_mid_scaled = mid_scaled
        self._hold_anchor_regime_active = regime_active

    def _release(self) -> bool:
        if self._hold_start_ns is not None:
            self._release_count += 1
        self._hold_start_ns = None
        self._hold_anchor_mid_scaled = None
        self._hold_anchor_regime_active = False
        return False

    def on_refresh_executed(self) -> None:
        """Caller invokes this after actually refreshing (cancel+repost).

        This drops any in-progress hold without counting as a "release"
        event (the caller chose to refresh for its own reasons — e.g. at
        EOD flatten).
        """
        self._hold_start_ns = None
        self._hold_anchor_mid_scaled = None
        self._hold_anchor_regime_active = False

    def on_gap(self) -> None:
        """Reset all transient state after bus overflow."""
        self._hold_start_ns = None
        self._hold_anchor_mid_scaled = None
        self._hold_anchor_regime_active = False
        # Tracker survives gap; it's stateless vs ordering once minutes close.


# ----------------------------------------------------------------------------
# AlphaProtocol shim
# ----------------------------------------------------------------------------


class C32bAlpha:
    """AlphaProtocol wrapper around C32bModulator for registry smoke-path."""

    __slots__ = ("_modulator", "_manifest", "_last_signal")

    def __init__(self, params: C32bParams | None = None) -> None:
        self._modulator = C32bModulator(params=params)
        self._last_signal = 0.0
        self._manifest = AlphaManifest(
            alpha_id="c32b_tob_survival_refresh_regime_gate_rescue",
            hypothesis=(
                "On TMFD6 minutes where top-of-book survival is high (rolling "
                "5-min median of per-minute median TOB-survival > 200 ms), "
                "delaying R47's cancel-and-repost on non-mid-moving LOB events "
                "preserves queue priority and lifts expected fill probability "
                "per quote-life. The modulator issues no new orders: "
                "incremental RT cost = 0. Threshold and hold cap (200 ms / "
                "250 ms) are IS-selected per R6 T1 and DA-validated."
            ),
            formula=(
                "should_delay_refresh(event) = regime_active(tob_roll5_med > "
                "200 ms) AND NOT mid_moved(|Δmid| > 0.5 tick) AND hold_ms "
                "<= 250 ms AND anchor_regime_active."
            ),
            paper_refs=(
                "r47_maker_strategy",
                "r47_structural_properties",
                "feedback_taifex_fee_structure",
                "2017_Moallemi_Yuan_queue_value_LOB",
                "2014_Maglaras_multi_class_LOB",
                "1995_Biais_Hillion_Spatt_LOB",
                "2014_Stoikov_Waeber_optimal_liquidation",
            ),
            data_fields=(
                "bid_price",
                "ask_price",
                "tob_changed",
                "exch_ts",
            ),
            complexity="O(1)",
            status=AlphaStatus.PROTOTYPE,
            tier=AlphaTier.TIER_1,
            rust_module=None,
            latency_profile="shioaji_sim_p95_v2026-03-04",
            roles_used=(),
            skills_used=("hft-backtester",),
            feature_set_version=None,
            strategy_type="maker",
            instrument="TMFD6",
        )

    @property
    def manifest(self) -> AlphaManifest:
        return self._manifest

    @property
    def modulator(self) -> C32bModulator:
        return self._modulator

    def update(self, *args: object, **kwargs: object) -> float:
        return self._last_signal

    def reset(self) -> None:
        self._modulator = C32bModulator(params=self._modulator.params)
        self._last_signal = 0.0

    def get_signal(self) -> float:
        return self._last_signal


__all__ = [
    "C32bAlpha",
    "C32bModulator",
    "C32bParams",
    "LobRefreshEvent",
]
