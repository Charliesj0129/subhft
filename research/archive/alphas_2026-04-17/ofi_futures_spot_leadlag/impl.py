"""Futures-Spot OFI Lead-Lag Alpha — cross-asset order flow propagation.

Signal:  ofi_futures - beta * ofi_spot  (rolling beta, EMA smoothed)
Where:
    ofi_futures = L1 order flow imbalance of TAIEX futures (TXFD6)
    ofi_spot    = L1 order flow imbalance of underlying stock (2330)
    beta        = rolling_cov(ofi_futures, ofi_spot) / rolling_var(ofi_spot)
                  estimated over beta_window ticks via EMA of cross-moments
    signal      = EMA_out(residual / mad), clipped [-3, 3]
    signal      = 0.0 during warmup (beta_window/10 ticks for beta stabilization)

Microstructure basis:
    Hasbrouck (2003): index futures lead the underlying in price discovery.
    Cont, Cucuringu, Zhang (2021, arXiv:2112.13213): lagged cross-asset OFI
    significantly improves return forecasting at short horizons.

    On TWSE: TXFD6 (TAIEX futures) leads constituent stocks (esp. 2330/TSMC
    ~30% of TAIEX weight).

Timestamp sync (C1): TAIFEX and TWSE have separate clocks.  Verified on
    2026-03-23: overlapping session 00:30-02:51 UTC.  Accept < 100ms offset.

Data overlap (C4): 4 overlapping trading days (2026-03-19 to 2026-03-24),
    TXFD6: 2.2M rows, 2330: 236k rows.

Asymmetric tick rate: Futures tick ~10x faster than stocks.  Calling
    convention: update() called on EVERY event, using latest cached value
    for the other symbol.  This is Option A from the team lead's spec —
    chosen because it preserves futures information content without waiting
    for slower stock updates.

Allocator Law  : __slots__ on class; no heap allocations in update().
Precision Law  : output is float (signal score, not price).
Cache Law      : all state is scalar floats.
Latency profile: shioaji_sim_p95_v2026-03-04.
"""

from __future__ import annotations

import math

from research.registry.schemas import AlphaManifest, AlphaStatus, AlphaTier

# --- Default parameters ---
_DEFAULT_EMA_WINDOW: int = 4
_DEFAULT_BETA_WINDOW: int = 1000
_DEFAULT_OUTPUT_WINDOW: int = 8
_DEFAULT_MAD_WINDOW: int = 32
_SIGNAL_CLIP: float = 3.0
_EPSILON: float = 1e-12
_DEFAULT_STALE_TICKS_LIMIT: int = 200

_MANIFEST = AlphaManifest(
    alpha_id="ofi_futures_spot_leadlag",
    hypothesis=(
        "TAIEX futures (TXFD6) lead the underlying stock (2330/TSMC) in "
        "price discovery.  When futures OFI shifts aggressively, the stock "
        "price adjusts with a measurable delay.  The lagged cross-asset OFI "
        "divergence creates a short-lived directional signal for the stock."
    ),
    formula=(
        "beta = EMA_cov(ofi_f, ofi_s) / EMA_var(ofi_s); "
        "residual = ofi_f_ema - beta * ofi_s_ema; "
        "signal = EMA_out(residual / mad), clipped [-3, 3]"
    ),
    paper_refs=("Hasbrouck 2003", "arXiv:2112.13213"),
    # Fix 3: data_fields reflects what the alpha consumes (OFI computed by bridge)
    data_fields=("ofi_l1_raw",),
    complexity="O(1)",
    status=AlphaStatus.DRAFT,
    tier=AlphaTier.TIER_2,
    rust_module=None,
    latency_profile="shioaji_sim_p95_v2026-03-04",
    roles_used=("planner", "code-reviewer"),
    skills_used=("iterative-retrieval", "validation-gate"),
    feature_set_version="lob_shared_v1",
)


def _ema_alpha(window: int) -> float:
    """Compute EMA smoothing factor from window size."""
    return 1.0 - math.exp(-1.0 / window)


class _RollingBetaTracker:
    """O(1) rolling beta via EMA of cross-moments.

    beta = cov(x, y) / var(y)
    where cov and var are estimated via EMA of demeaned products.

    Fix 1: demeaned values computed BEFORE updating means to avoid
    underestimating variance.
    """

    __slots__ = (
        "_alpha", "_mean_x", "_mean_y",
        "_cov_xy", "_var_y", "_beta", "_initialized",
    )

    def __init__(self, window: int) -> None:
        self._alpha: float = _ema_alpha(window)
        self._mean_x: float = 0.0
        self._mean_y: float = 0.0
        self._cov_xy: float = 0.0
        self._var_y: float = 0.0
        self._beta: float = 1.0  # default beta = 1 (equal weighting)
        self._initialized: bool = False

    def update(self, x: float, y: float) -> float:
        """Update with new (x=futures_ofi, y=spot_ofi) and return current beta."""
        if not self._initialized:
            self._mean_x = x
            self._mean_y = y
            self._cov_xy = 0.0
            self._var_y = max(y * y, _EPSILON)
            self._initialized = True
            return self._beta

        # Fix 1: compute demeaned values BEFORE updating means
        dx = x - self._mean_x
        dy = y - self._mean_y

        # Update running means
        a = self._alpha
        self._mean_x += a * (x - self._mean_x)
        self._mean_y += a * (y - self._mean_y)

        # Update running covariance and variance
        self._cov_xy += a * (dx * dy - self._cov_xy)
        self._var_y += a * (dy * dy - self._var_y)

        # Compute beta (with denominator floor)
        denom = max(self._var_y, _EPSILON)
        self._beta = self._cov_xy / denom

        return self._beta

    @property
    def beta(self) -> float:
        return self._beta

    def reset(self) -> None:
        self._mean_x = 0.0
        self._mean_y = 0.0
        self._cov_xy = 0.0
        self._var_y = 0.0
        self._beta = 1.0
        self._initialized = False


class OfiFuturesSpotLeadlagAlpha:
    """O(1) cross-asset OFI lead-lag signal: futures OFI leads spot price.

    Accepts (ofi_futures, ofi_spot) as positional or keyword args.

    Parameters
    ----------
    ema_window : int
        Window for smoothing raw OFI inputs (default 4).
    beta_window : int
        Window for rolling beta estimation (default 1000).
    output_window : int
        Window for output signal smoothing (default 8).
    mad_window : int
        Window for MAD normalizer (default 32).
    stale_ticks_limit : int
        Max ticks without non-zero update before stale guard activates (default 200).
    normalize_by_depth : bool
        If True, normalize OFI by total L1 depth before processing.
        Produces dimensionless ratio [-1, +1] for both instruments,
        eliminating scale mismatch between futures (~2 lots) and
        stocks (~500+ lots).  Default False.
    """

    __slots__ = (
        "_ema_window", "_beta_window", "_output_window",
        "_alpha_ema", "_alpha_out", "_alpha_mad",
        "_beta_tracker",
        "_ema_futures", "_ema_spot",
        "_ema_out", "_ema_mad",
        "_signal", "_initialized", "_tick_count",
        "_ticks_since_futures", "_ticks_since_spot",
        "_stale_ticks_limit",
        "_warmup_ticks",
        "_normalize_by_depth",
    )

    def __init__(
        self,
        ema_window: int = _DEFAULT_EMA_WINDOW,
        beta_window: int = _DEFAULT_BETA_WINDOW,
        output_window: int = _DEFAULT_OUTPUT_WINDOW,
        mad_window: int = _DEFAULT_MAD_WINDOW,
        stale_ticks_limit: int = _DEFAULT_STALE_TICKS_LIMIT,
        normalize_by_depth: bool = False,
    ) -> None:
        self._ema_window: int = ema_window
        self._beta_window: int = beta_window
        self._output_window: int = output_window
        self._stale_ticks_limit: int = stale_ticks_limit

        self._alpha_ema: float = _ema_alpha(ema_window)
        self._alpha_out: float = _ema_alpha(output_window)
        self._alpha_mad: float = _ema_alpha(mad_window)

        self._beta_tracker: _RollingBetaTracker = _RollingBetaTracker(beta_window)

        # EMA states
        self._ema_futures: float = 0.0
        self._ema_spot: float = 0.0
        self._ema_out: float = 0.0
        self._ema_mad: float = 0.0

        self._signal: float = 0.0
        self._initialized: bool = False
        self._tick_count: int = 0

        # Stale data tracking
        self._ticks_since_futures: int = 0
        self._ticks_since_spot: int = 0

        self._normalize_by_depth: bool = normalize_by_depth

        # Warmup: need beta_window/10 ticks minimum for beta to stabilize
        self._warmup_ticks: int = max(beta_window // 10, 64)

    @property
    def manifest(self) -> AlphaManifest:
        return _MANIFEST

    def update(self, *args: float, **kwargs: object) -> float:
        # Accept (ofi_futures, ofi_spot) as positional or keyword
        # Optional: depth_futures, depth_spot for normalization
        if len(args) >= 2:
            ofi_futures = float(args[0])
            ofi_spot = float(args[1])
            depth_futures = float(args[2]) if len(args) >= 3 else 0.0
            depth_spot = float(args[3]) if len(args) >= 4 else 0.0
        elif "ofi_futures" in kwargs and "ofi_spot" in kwargs:
            ofi_futures = float(kwargs["ofi_futures"])  # type: ignore[arg-type]
            ofi_spot = float(kwargs["ofi_spot"])  # type: ignore[arg-type]
            depth_futures = float(kwargs.get("depth_futures", 0.0))  # type: ignore[arg-type]
            depth_spot = float(kwargs.get("depth_spot", 0.0))  # type: ignore[arg-type]
        else:
            raise ValueError(
                "ofi_futures_spot_leadlag requires (ofi_futures, ofi_spot) as "
                "positional args or via ofi_futures=/ofi_spot= keywords."
            )

        # Depth normalization: OFI / total_depth -> dimensionless [-1, +1]
        if self._normalize_by_depth:
            ofi_futures = ofi_futures / max(depth_futures, 1.0)
            ofi_spot = ofi_spot / max(depth_spot, 1.0)

        self._tick_count += 1

        # Track staleness (reset on non-zero update)
        if ofi_futures != 0.0:
            self._ticks_since_futures = 0
        else:
            self._ticks_since_futures += 1

        if ofi_spot != 0.0:
            self._ticks_since_spot = 0
        else:
            self._ticks_since_spot += 1

        # Stale guard: if either symbol hasn't had real data for too long
        stale = (
            self._ticks_since_futures > self._stale_ticks_limit
            or self._ticks_since_spot > self._stale_ticks_limit
        )

        if not self._initialized:
            # Seed EMAs; MAD seeded with input magnitude (Round 1 lesson)
            self._ema_futures = ofi_futures
            self._ema_spot = ofi_spot
            self._ema_mad = max(abs(ofi_futures), abs(ofi_spot), 1.0)
            self._ema_out = 0.0
            self._beta_tracker.update(ofi_futures, ofi_spot)
            self._initialized = True
            self._signal = 0.0
            return self._signal

        # Update OFI EMAs
        a = self._alpha_ema
        self._ema_futures += a * (ofi_futures - self._ema_futures)
        self._ema_spot += a * (ofi_spot - self._ema_spot)

        # Update rolling beta
        beta = self._beta_tracker.update(ofi_futures, ofi_spot)

        # Compute residual: futures flow not yet reflected in spot
        residual = self._ema_futures - beta * self._ema_spot

        # Update MAD normalizer
        abs_res = abs(residual)
        self._ema_mad += self._alpha_mad * (abs_res - self._ema_mad)

        # Standardize (floor MAD at 1% of input scale to prevent 0/0 -> 1.0)
        input_scale = max(abs(self._ema_futures), abs(self._ema_spot), 1.0) * 0.01
        denom = max(self._ema_mad, input_scale, _EPSILON)
        surprise = residual / denom

        # Smooth output
        self._ema_out += self._alpha_out * (surprise - self._ema_out)

        # Apply warmup, stale guard, and clipping
        if self._tick_count < self._warmup_ticks or stale:
            self._signal = 0.0
        else:
            self._signal = max(-_SIGNAL_CLIP, min(_SIGNAL_CLIP, self._ema_out))

        return self._signal

    def reset(self) -> None:
        self._ema_futures = 0.0
        self._ema_spot = 0.0
        self._ema_out = 0.0
        self._ema_mad = 0.0
        self._signal = 0.0
        self._initialized = False
        self._tick_count = 0
        self._ticks_since_futures = 0
        self._ticks_since_spot = 0
        self._beta_tracker.reset()

    def get_signal(self) -> float:
        return self._signal

    @property
    def warmup_ticks(self) -> int:
        return self._warmup_ticks

    @property
    def beta(self) -> float:
        return self._beta_tracker.beta


ALPHA_CLASS = OfiFuturesSpotLeadlagAlpha

__all__ = ["OfiFuturesSpotLeadlagAlpha", "ALPHA_CLASS"]
