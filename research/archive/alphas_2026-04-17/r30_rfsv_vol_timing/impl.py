"""R30 RFSV Vol-Timing Alpha — rough volatility forecasting + vol-of-vol direction.

Mechanism:
    Log-realized-volatility follows fractional Brownian motion with Hurst
    exponent H ~ 0.1 (Gatheral, Jaisson, Rosenbaum 2014).  The RFSV model
    yields a parsimonious vol forecast that outperforms HAR/GARCH.

    Challenger review (v2 fixes):
    - A-1: Expanded variogram to 12 lags + weighted least squares for robust H.
    - A-2: Forecast now uses _FORECAST_HORIZON via fBm conditional expectation
            with proper covariance kernel: C(s,t) = 0.5*(|s|^{2H}+|t|^{2H}-|s-t|^{2H}).
    - A-3: Added vol-of-vol directional component: when vol is accelerating
            (forecast >> recent), the next price move is expected to be larger.
            Combined with a simple return-sign filter, this produces directional
            signals, not just position sizing.  This is "entry timing" — enter
            when vol is contracting (better fills, tighter spreads) and exit/
            reverse when vol is expanding.
    - A-4: Warmup reduced from 48 to 16 buckets.

Variogram-based H estimation (Gatheral 2014, Section 2):
    m(q, delta) = E[|log(sigma_{t+delta}) - log(sigma_t)|^q]
    For fBm: m(q, delta) ~ C_q * delta^{qH}
    With q=2: m(2, delta) ~ C * delta^{2H}
    => H = slope of log(m(2,delta)) vs log(delta) / 2
    Uses 12 lags (1..12) instead of 6 sparse lags for better regression.

RFSV forecast (Gatheral 2014, Section 3):
    For fBm with Hurst H, the conditional expectation at horizon h given
    past observations at lags 1..K uses the covariance kernel:
      C(i,j) = 0.5 * (i^{2H} + j^{2H} - |i-j|^{2H})
      w = C(h, 1..K) @ inv(C(1..K, 1..K))
      forecast = w @ log_rv_past

Paper refs:
  Gatheral, Jaisson, Rosenbaum (2014) arXiv:1410.3394 — Volatility is rough.
  Mouti (2023) arXiv:2312.01426 — RFSV range-based confirmation.
  Bibinger, Yu, Zhang (2025) arXiv:2504.15985 — Multivariate fBm forecasting.

Allocator Law  : __slots__ on class; pre-allocated numpy arrays for RV history.
Precision Law  : output is float signal score, not price — no Decimal needed.
Cache Law      : RV history in contiguous float64 ring buffer.
Latency profile: shioaji_sim_p95_v2026-03-04 (set at inception per CLAUDE.md).
"""

from __future__ import annotations

import math

import numpy as np

from research.registry.schemas import AlphaManifest, AlphaStatus, AlphaTier

# --- Configuration constants (tunable via config.yaml) ---

# Realized volatility computation
_RV_WINDOW_TICKS: int = 300       # ticks per RV bucket (~5 min at ~1 tick/sec)
_RV_HISTORY_SIZE: int = 128       # number of RV buckets to retain for H estimation
_MIN_RV_BUCKETS: int = 16         # minimum buckets before H estimation begins (A-4: reduced)

# Hurst exponent estimation (variogram method)
# A-1 fix: use 12 consecutive lags (1..12) instead of 6 sparse lags
_VARIOGRAM_MAX_LAG: int = 12      # compute variogram at lags 1, 2, ..., 12
_H_UPDATE_INTERVAL: int = 4       # re-estimate H every N new RV buckets
_H_CLIP_LO: float = 0.01         # minimum H (sanity bound)
_H_CLIP_HI: float = 0.49         # maximum H (must be < 0.5 for rough vol)
_H_EMA_ALPHA: float = 0.3        # EMA smoothing for H estimates (stabilize)

# RFSV forecast (A-2 fix: horizon is now used in covariance kernel)
_FORECAST_HORIZON: int = 4        # forecast N RV buckets ahead (~20 min)
_FORECAST_LOOKBACK: int = 32      # use last N buckets for forecast (reduced for stable inversion)
_COV_REGULARIZE: float = 1e-8     # Tikhonov regularization for covariance inversion

# Vol-timing signal
_SIGNAL_SMOOTHING: float = 0.15   # EMA alpha for signal smoothing
_SIGNAL_CLIP: float = 1.0         # clip output to [-1, +1]
_WARMUP_BUCKETS: int = 16         # minimum buckets before emitting signal (A-4: reduced)

# A-3 fix: vol-of-vol directional component
_VOLOFVOL_WEIGHT: float = 0.6     # weight of vol-timing component
_DIRECTION_WEIGHT: float = 0.4    # weight of return-sign directional component
_RETURN_EMA_ALPHA: float = 0.05   # EMA alpha for recent return sign tracker

# Floor for log-RV to avoid log(0)
_LOG_RV_FLOOR: float = -20.0
_RV_FLOOR: float = 1e-16

_MANIFEST = AlphaManifest(
    alpha_id="r30_rfsv_vol_timing",
    hypothesis=(
        "Log-realized-vol follows fBm with H~0.1 (rough vol). "
        "RFSV forecast outperforms HAR/GARCH; combined with return-sign "
        "direction filter, enables entry-timing alpha on TMFD6."
    ),
    formula=(
        "forecast = w @ log_rv_past, w = C(h,1..K) @ inv(C(1..K,1..K)); "
        "signal = voltime_component * 0.6 + direction_component * 0.4"
    ),
    paper_refs=("arXiv:1410.3394", "arXiv:2312.01426", "arXiv:2504.15985"),
    data_fields=("mid_price", "volume"),
    complexity="O(1)",
    status=AlphaStatus.DRAFT,
    tier=AlphaTier.TIER_2,
    rust_module=None,
    latency_profile="shioaji_sim_p95_v2026-03-04",
    roles_used=("planner", "code-reviewer"),
    skills_used=("iterative-retrieval", "validation-gate"),
    feature_set_version="lob_shared_v3",
)


def _fbm_covariance(i: float, j: float, h: float) -> float:
    """Fractional Brownian motion covariance: C(i,j) = 0.5*(|i|^{2H}+|j|^{2H}-|i-j|^{2H})."""
    two_h = 2.0 * h
    return 0.5 * (abs(i) ** two_h + abs(j) ** two_h - abs(i - j) ** two_h)


class R30RfsvVolTimingAlpha:
    """RFSV-based realized volatility forecasting with directional entry timing.

    Feed tick prices via update(price=...).  The alpha:
    1. Accumulates squared returns into RV buckets.
    2. Estimates Hurst exponent H from variogram of log-RV (12 dense lags).
    3. Forecasts future log-RV using exact fBm conditional expectation.
    4. Combines vol-timing with return-sign direction for entry timing.
    5. Emits signal in [-1, +1].
    """

    __slots__ = (
        "_prev_price",
        "_tick_count_in_bucket",
        "_sum_sq_returns",
        "_rv_ring",
        "_log_rv_ring",
        "_rv_write_idx",
        "_rv_count",
        "_hurst_h",
        "_buckets_since_h_update",
        "_forecast_log_rv",
        "_recent_log_rv_ema",
        "_forecast_weights",
        "_return_sign_ema",
        "_signal",
        "_signal_ema",
        "_total_ticks",
    )

    def __init__(self) -> None:
        self._prev_price: int = 0
        self._tick_count_in_bucket: int = 0
        self._sum_sq_returns: float = 0.0

        self._rv_ring: np.ndarray = np.zeros(_RV_HISTORY_SIZE, dtype=np.float64)
        self._log_rv_ring: np.ndarray = np.full(
            _RV_HISTORY_SIZE, _LOG_RV_FLOOR, dtype=np.float64
        )
        self._rv_write_idx: int = 0
        self._rv_count: int = 0

        self._hurst_h: float = 0.1
        self._buckets_since_h_update: int = 0

        self._forecast_log_rv: float = 0.0
        self._recent_log_rv_ema: float = _LOG_RV_FLOOR
        # A-2: pre-computed forecast weights (recomputed when H changes)
        self._forecast_weights: np.ndarray | None = None

        # A-3: directional component — EMA of recent log-returns
        self._return_sign_ema: float = 0.0

        self._signal: float = 0.0
        self._signal_ema: float = 0.0
        self._total_ticks: int = 0

    @property
    def manifest(self) -> AlphaManifest:
        return _MANIFEST

    @property
    def hurst_h(self) -> float:
        """Current estimated Hurst exponent (for diagnostics)."""
        return self._hurst_h

    @property
    def rv_count(self) -> int:
        """Number of completed RV buckets."""
        return self._rv_count

    @property
    def forecast_log_rv(self) -> float:
        """Current forecast of log-RV (for diagnostics)."""
        return self._forecast_log_rv

    def update(self, *, price: int = 0, **kwargs: object) -> float:  # noqa: ARG002
        """Process a new tick price (scaled int x10000).

        Returns the current signal in [-1, +1].
        """
        self._total_ticks += 1

        if price <= 0:
            return self._signal

        if self._prev_price > 0:
            log_ret = math.log(price / self._prev_price)
            self._sum_sq_returns += log_ret * log_ret
            self._tick_count_in_bucket += 1
            # A-3: track return direction
            self._return_sign_ema += _RETURN_EMA_ALPHA * (
                log_ret - self._return_sign_ema
            )

        self._prev_price = price

        if self._tick_count_in_bucket >= _RV_WINDOW_TICKS:
            self._close_rv_bucket()

        return self._signal

    def _close_rv_bucket(self) -> None:
        """Finalize current RV bucket and update forecast."""
        rv = max(self._sum_sq_returns, _RV_FLOOR)
        log_rv = math.log(rv)

        idx = self._rv_write_idx % _RV_HISTORY_SIZE
        self._rv_ring[idx] = rv
        self._log_rv_ring[idx] = log_rv
        self._rv_write_idx += 1
        self._rv_count += 1

        self._sum_sq_returns = 0.0
        self._tick_count_in_bucket = 0

        self._recent_log_rv_ema += _SIGNAL_SMOOTHING * (
            log_rv - self._recent_log_rv_ema
        )

        self._buckets_since_h_update += 1
        if (
            self._rv_count >= _MIN_RV_BUCKETS
            and self._buckets_since_h_update >= _H_UPDATE_INTERVAL
        ):
            self._estimate_hurst()
            self._recompute_forecast_weights()
            self._buckets_since_h_update = 0

        if self._rv_count >= _MIN_RV_BUCKETS:
            self._compute_forecast()
            self._update_signal()

    def _estimate_hurst(self) -> None:
        """Estimate H via variogram of log-RV (A-1 fix: 12 dense lags).

        Uses weighted least squares with weights proportional to the number
        of overlapping pairs at each lag, improving statistical efficiency
        on short samples.
        """
        n = min(self._rv_count, _RV_HISTORY_SIZE)
        start = self._rv_write_idx % _RV_HISTORY_SIZE
        indices = np.arange(start - n, start) % _RV_HISTORY_SIZE
        log_rv = self._log_rv_ring[indices]

        max_lag = min(_VARIOGRAM_MAX_LAG, n // 2)
        if max_lag < 3:
            return

        log_lags: list[float] = []
        log_vars: list[float] = []
        wls_weights: list[float] = []

        for lag in range(1, max_lag + 1):
            diffs = log_rv[lag:] - log_rv[:-lag]
            n_pairs = len(diffs)
            if n_pairs < 2:
                break
            m2 = float(np.mean(diffs * diffs))
            if m2 > 0:
                log_lags.append(math.log(lag))
                log_vars.append(math.log(m2))
                # Weight by sqrt(n_pairs) — more pairs = more reliable
                wls_weights.append(math.sqrt(n_pairs))

        if len(log_lags) < 3:
            return

        # Weighted least squares: log(m2) = 2H * log(lag) + const
        x = np.array(log_lags, dtype=np.float64)
        y = np.array(log_vars, dtype=np.float64)
        w = np.array(wls_weights, dtype=np.float64)
        w /= float(np.sum(w))

        x_mean = float(np.sum(w * x))
        y_mean = float(np.sum(w * y))
        ss_xx = float(np.sum(w * (x - x_mean) ** 2))
        if ss_xx < 1e-30:
            return
        ss_xy = float(np.sum(w * (x - x_mean) * (y - y_mean)))
        slope = ss_xy / ss_xx

        h_new = slope / 2.0
        h_new = max(_H_CLIP_LO, min(_H_CLIP_HI, h_new))
        # EMA smooth the H estimate for stability
        self._hurst_h += _H_EMA_ALPHA * (h_new - self._hurst_h)

    def _recompute_forecast_weights(self) -> None:
        """Recompute fBm conditional expectation weights (A-2 fix).

        Uses exact fBm covariance kernel:
          C(i,j) = 0.5 * (|i|^{2H} + |j|^{2H} - |i-j|^{2H})

        Forecast at horizon h given past at lags 1..K:
          w = C(h, 1..K) @ inv(C(1..K, 1..K))
        """
        n = min(self._rv_count, _RV_HISTORY_SIZE)
        k = min(n, _FORECAST_LOOKBACK)
        if k < 2:
            self._forecast_weights = None
            return

        h = self._hurst_h
        horizon = float(_FORECAST_HORIZON)

        # Build covariance matrix C(i, j) for lags i, j in 1..K
        lags = np.arange(1, k + 1, dtype=np.float64)
        cov_matrix = np.zeros((k, k), dtype=np.float64)
        for i in range(k):
            for j in range(i, k):
                c = _fbm_covariance(lags[i], lags[j], h)
                cov_matrix[i, j] = c
                cov_matrix[j, i] = c

        # Regularize for numerical stability
        cov_matrix += _COV_REGULARIZE * np.eye(k, dtype=np.float64)

        # Cross-covariance vector: C(horizon, lag_i) for i = 1..K
        # Note: horizon is "future", lags are "past". In the fBm framework,
        # we compute C(horizon + lag_i, lag_j) but for stationary increments
        # the relevant kernel is C(horizon, lag_i) = 0.5*(h^{2H}+lag_i^{2H}-(h-lag_i)^{2H})
        # where h-lag_i can be negative (use abs).
        cross_cov = np.array(
            [_fbm_covariance(horizon, lags[i], h) for i in range(k)],
            dtype=np.float64,
        )

        try:
            weights = np.linalg.solve(cov_matrix, cross_cov)
        except np.linalg.LinAlgError:
            self._forecast_weights = None
            return

        self._forecast_weights = weights

    def _compute_forecast(self) -> None:
        """Forecast future log-RV using pre-computed fBm weights (A-2 fix)."""
        if self._forecast_weights is None:
            # Fallback: simple EMA-based forecast
            self._forecast_log_rv = self._recent_log_rv_ema
            return

        k = len(self._forecast_weights)
        n = min(self._rv_count, _RV_HISTORY_SIZE)
        if k > n:
            self._forecast_log_rv = self._recent_log_rv_ema
            return

        # Extract most recent k log-RV values (most recent = lag 1)
        start = self._rv_write_idx % _RV_HISTORY_SIZE
        indices = np.arange(start - k, start) % _RV_HISTORY_SIZE
        log_rv_recent = self._log_rv_ring[indices]
        log_rv_recent = log_rv_recent[::-1]  # index 0 = most recent = lag 1

        self._forecast_log_rv = float(np.dot(self._forecast_weights, log_rv_recent))

    def _update_signal(self) -> None:
        """Combine vol-timing and directional components (A-3 fix).

        Vol-timing component:
          -tanh(forecast_log_rv - recent_log_rv_ema)
          Positive when vol contracting (good for entry), negative when expanding.

        Directional component:
          tanh(return_sign_ema * vol_contracting_indicator)
          When vol is contracting AND recent returns are positive -> enter long.
          When vol is contracting AND recent returns are negative -> enter short.
          When vol is expanding -> flatten (directional component -> 0).

        Combined signal enables entry-timing: the alpha knows WHEN to enter
        (low vol) and WHICH DIRECTION (recent return momentum).
        """
        if self._rv_count < _WARMUP_BUCKETS:
            self._signal = 0.0
            return

        # Vol-timing component
        diff = self._forecast_log_rv - self._recent_log_rv_ema
        vol_component = -math.tanh(diff)  # +1 when vol contracting, -1 expanding

        # Directional component: return sign scaled by vol-timing favorability
        # Only express direction when vol conditions are favorable (vol_component > 0)
        vol_favorability = max(0.0, vol_component)  # 0 to 1
        direction_raw = math.tanh(self._return_sign_ema * 1000.0)  # saturate sign
        direction_component = direction_raw * vol_favorability

        raw_signal = (
            _VOLOFVOL_WEIGHT * vol_component
            + _DIRECTION_WEIGHT * direction_component
        )

        self._signal_ema += _SIGNAL_SMOOTHING * (raw_signal - self._signal_ema)
        self._signal = max(-_SIGNAL_CLIP, min(_SIGNAL_CLIP, self._signal_ema))

    def reset(self) -> None:
        """Reset all state."""
        self._prev_price = 0
        self._tick_count_in_bucket = 0
        self._sum_sq_returns = 0.0
        self._rv_ring[:] = 0.0
        self._log_rv_ring[:] = _LOG_RV_FLOOR
        self._rv_write_idx = 0
        self._rv_count = 0
        self._hurst_h = 0.1
        self._buckets_since_h_update = 0
        self._forecast_log_rv = 0.0
        self._recent_log_rv_ema = _LOG_RV_FLOOR
        self._forecast_weights = None
        self._return_sign_ema = 0.0
        self._signal = 0.0
        self._signal_ema = 0.0
        self._total_ticks = 0

    def get_signal(self) -> float:
        return self._signal


ALPHA_CLASS = R30RfsvVolTimingAlpha
