"""R25 Large Order Flow Detection — METAORDER-TRAIL alpha (Phase A).

Two-stage signal using EXISTING FeatureEngine features only (EC-5):
  Stage 1 (SWEEP): Detect consecutive same-direction price moves >= N ticks.
  Stage 2 (CONFIRM): Check ofi_l1_ema5s [22] is same-sign and above threshold.

Signal output:
  +1.0 = confirmed buy-side sweep (enter long)
  -1.0 = confirmed sell-side sweep (enter short)
   0.0 = no signal / cooldown / warmup

Design: MR-4 — single confirmation condition. Phase B adds depth/toxicity.

Paper refs:
  arXiv:2503.18199 — metaorder detection from public data
  arXiv:1412.4503  — square-root law (Donier & Bonart)
  arXiv:1609.00599 — order anticipation (Strehle)
  arXiv:1701.03960 — optimal trailing stop (Leung & Zhang)

Allocator Law : __slots__ on class; no heap in update().
Precision Law : prices as int (x10000); signal is float {-1, 0, +1}.
Cache Law     : scalar state only, no arrays in hot path.
"""

from __future__ import annotations

from research.registry.schemas import AlphaManifest, AlphaStatus, AlphaTier

# ---------------------------------------------------------------------------
# Feature Engine slot indices (lob_shared_v3)
# ---------------------------------------------------------------------------
_FE_MID_PRICE_X2: int = 2    # mid_price_x2
_FE_SPREAD_SCALED: int = 3   # spread_scaled
_FE_OFI_EMA5S: int = 22      # ofi_l1_ema5s
_FE_OFI_EMA30S: int = 23     # ofi_l1_ema30s

# ---------------------------------------------------------------------------
# Default parameters
# ---------------------------------------------------------------------------
_TICK_SIZE_X10000: int = 10_000     # 1 pt in scaled-int
_SWEEP_MIN_TICKS: int = 2           # minimum cumulative ticks for sweep
_SWEEP_MAX_EVENTS: int = 5          # max events to accumulate sweep over
_OFI_EMA5S_THRESHOLD: int = 50      # |ofi_l1_ema5s| must exceed this
_COOLDOWN_NS: int = 10_000_000_000  # 10s between signals
_WARMUP_EVENTS: int = 60            # wait for FE EMA warmup (~v3 needs 40+)

# ---------------------------------------------------------------------------
# Manifest
# ---------------------------------------------------------------------------
_MANIFEST = AlphaManifest(
    alpha_id="r25_large_order_flow",
    hypothesis=(
        "Large institutional orders manifest as multi-level price sweeps. "
        "Confirmed by sustained ofi_l1_ema5s same-sign, these events predict "
        "price continuation (square-root law). Targets rare discrete events."
    ),
    formula=(
        "sweep = cumulative same-dir price move >= 2 ticks over <= 5 events; "
        "confirm = ofi_l1_ema5s same-sign as sweep AND |ofi_l1_ema5s| > threshold; "
        "signal = sweep_direction when confirmed"
    ),
    paper_refs=(
        "arXiv:2503.18199",
        "arXiv:1412.4503",
        "arXiv:1609.00599",
        "arXiv:1701.03960",
    ),
    data_fields=(
        "mid_price_x2",
        "spread_scaled",
        "ofi_l1_ema5s",
        "ofi_l1_ema30s",
    ),
    complexity="O(1)",
    status=AlphaStatus.DRAFT,
    tier=AlphaTier.TIER_2,
    rust_module=None,
    latency_profile="sim_p95_v2026-02-26",
    roles_used=("planner", "code-reviewer"),
    skills_used=("iterative-retrieval", "validation-gate"),
    feature_set_version="lob_shared_v3",
)


class LargeOrderFlowAlpha:
    """Phase A: Sweep detection + single OFI-EMA confirmation.

    Consumes FeatureEngine output tuple (lob_shared_v3, 27 features).
    Uses only pre-computed features — no custom OFI calculations (EC-5).

    Parameters
    ----------
    sweep_min_ticks : int
        Minimum cumulative same-direction price move for sweep. Default: 2.
    sweep_max_events : int
        Maximum consecutive events to accumulate sweep. Default: 5.
    ofi_threshold : int
        Minimum |ofi_l1_ema5s| for confirmation. Default: 50.
    cooldown_ns : int
        Minimum time between signals. Default: 10_000_000_000 (10s).
    """

    __slots__ = (
        "_sweep_min_ticks",
        "_sweep_max_events",
        "_ofi_threshold",
        "_cooldown_ns",
        "_signal",
        "_last_signal_ts",
        "_prev_mid_x2",
        "_sweep_cum_delta",
        "_sweep_event_count",
        "_sweep_direction",
        "_tick_count",
    )

    def __init__(
        self,
        sweep_min_ticks: int = _SWEEP_MIN_TICKS,
        sweep_max_events: int = _SWEEP_MAX_EVENTS,
        ofi_threshold: int = _OFI_EMA5S_THRESHOLD,
        cooldown_ns: int = _COOLDOWN_NS,
    ) -> None:
        self._sweep_min_ticks = sweep_min_ticks
        self._sweep_max_events = sweep_max_events
        self._ofi_threshold = ofi_threshold
        self._cooldown_ns = cooldown_ns

        self._signal: float = 0.0
        self._last_signal_ts: int = 0
        self._prev_mid_x2: int = 0
        self._sweep_cum_delta: int = 0
        self._sweep_event_count: int = 0
        self._sweep_direction: int = 0  # +1, -1, or 0
        self._tick_count: int = 0

    @property
    def manifest(self) -> AlphaManifest:
        return _MANIFEST

    def update(self, *args: float, **kwargs: object) -> float:
        """Process a FeatureEngine update and return signal.

        Accepts keyword args ``features`` (tuple of 27 values from
        lob_shared_v3) and ``ts_ns`` (event timestamp).

        Returns
        -------
        float
            Signal: +1.0 (buy), -1.0 (sell), 0.0 (no signal).
        """
        features: tuple[int | float, ...] | None = None
        ts_ns: int = 0

        if "features" in kwargs:
            features = kwargs["features"]  # type: ignore[assignment]
        if "ts_ns" in kwargs:
            ts_ns = int(kwargs["ts_ns"])  # type: ignore[arg-type]

        if features is None or len(features) < _FE_OFI_EMA5S + 1:
            return 0.0

        self._tick_count += 1

        mid_x2 = int(features[_FE_MID_PRICE_X2])
        ofi_ema5s = int(features[_FE_OFI_EMA5S])

        # --- Stage 1: Sweep detection ---
        self._signal = 0.0

        if self._tick_count <= _WARMUP_EVENTS:
            self._prev_mid_x2 = mid_x2
            return 0.0

        delta_mid_x2 = mid_x2 - self._prev_mid_x2
        self._prev_mid_x2 = mid_x2

        if delta_mid_x2 == 0:
            # No price move — decay sweep accumulator
            if self._sweep_event_count > 0:
                self._sweep_event_count += 1
                if self._sweep_event_count > self._sweep_max_events:
                    self._sweep_cum_delta = 0
                    self._sweep_event_count = 0
                    self._sweep_direction = 0
            return 0.0

        # Price moved — check direction consistency
        move_dir = 1 if delta_mid_x2 > 0 else -1

        if self._sweep_direction == 0 or move_dir == self._sweep_direction:
            # Same direction or starting new sweep
            self._sweep_direction = move_dir
            self._sweep_cum_delta += delta_mid_x2
            self._sweep_event_count += 1
        else:
            # Direction changed — reset sweep
            self._sweep_cum_delta = delta_mid_x2
            self._sweep_event_count = 1
            self._sweep_direction = move_dir

        # Check if sweep exceeds too many events (stale)
        if self._sweep_event_count > self._sweep_max_events:
            self._sweep_cum_delta = delta_mid_x2
            self._sweep_event_count = 1
            self._sweep_direction = move_dir

        # mid_price_x2 = best_bid + best_ask.  A 1-tick move shifts mid_price_x2
        # by 2 * tick_size_x10000 when one side moves, or 1 * tick_size_x10000
        # when the book is crossed.  Use 1 * tick_size as the conservative
        # (minimum) step so we don't miss sweeps where bid and ask move together.
        sweep_ticks = abs(self._sweep_cum_delta) // _TICK_SIZE_X10000

        if sweep_ticks < self._sweep_min_ticks:
            return 0.0

        # --- Stage 2: Confirmation via ofi_l1_ema5s (single condition, MR-4) ---

        # Check cooldown
        if ts_ns > 0 and (ts_ns - self._last_signal_ts) < self._cooldown_ns:
            return 0.0

        # OFI must be same-sign as sweep direction and above threshold
        ofi_same_sign = (
            (self._sweep_direction > 0 and ofi_ema5s > 0)
            or (self._sweep_direction < 0 and ofi_ema5s < 0)
        )
        ofi_above_threshold = abs(ofi_ema5s) >= self._ofi_threshold

        if ofi_same_sign and ofi_above_threshold:
            self._signal = float(self._sweep_direction)
            if ts_ns > 0:
                self._last_signal_ts = ts_ns

            # Reset sweep accumulator after signal
            self._sweep_cum_delta = 0
            self._sweep_event_count = 0
            self._sweep_direction = 0
        else:
            self._signal = 0.0

        return self._signal

    @property
    def signal(self) -> float:
        """Current signal value."""
        return self._signal

    @property
    def sweep_direction(self) -> int:
        """Current sweep accumulator direction (+1/-1/0)."""
        return self._sweep_direction

    @property
    def sweep_ticks(self) -> int:
        """Current accumulated sweep size in ticks."""
        return abs(self._sweep_cum_delta) // _TICK_SIZE_X10000

    def get_signal(self) -> float:
        """AlphaProtocol: return current signal."""
        return self._signal

    def reset(self) -> None:
        """Reset all internal state."""
        self._signal = 0.0
        self._last_signal_ts = 0
        self._prev_mid_x2 = 0
        self._sweep_cum_delta = 0
        self._sweep_event_count = 0
        self._sweep_direction = 0
        self._tick_count = 0


ALPHA_CLASS = LargeOrderFlowAlpha

__all__ = ["LargeOrderFlowAlpha", "ALPHA_CLASS"]
