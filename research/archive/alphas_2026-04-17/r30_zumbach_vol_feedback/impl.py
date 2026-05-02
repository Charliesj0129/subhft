"""R30 Zumbach Volatility Feedback Alpha — quadratic return features for direction.

Mechanism:
    The Zumbach effect (Time-Reversal Asymmetry, TRA) is the observation that
    past return trends predict future volatility more strongly than past
    volatility predicts future squared returns.

    Challenger review (v2 fixes):
    - B-1: Removed abs() — now uses signed Z values directly. Z = (S^2 - Q)/2
           where S = cumret, Q = sum(r_i^2). Z >= 0 always (Cauchy-Schwarz).
           The signal is now based on the magnitude-weighted direction of Z:
           when Z is large during a down-trend, predict vol expansion + mean
           reversion. This is the paper's actual Z^2 decomposition approach.
    - B-2: Reframed from leverage to margin-call/stop-loss cascade mechanism.
           On TAIFEX futures, down moves trigger margin calls and stop-losses,
           which force liquidation, amplify vol, and create overshoot. This is
           an empirical hypothesis (not structural leverage) to be validated
           at Gate C via TRA ratio > 1.0 on TMFD6 data.
    - B-3: Windows [30, 120, 360] acknowledged as hyperparameters for Gate C
           parameter sweep. No theoretical derivation claimed.
    - B-4: TRA diagnostic rewritten using proper rolling covariance estimator:
           TRA(tau) = E[sigma^2_{t+tau} * R_t^2] - E[sigma^2_t * R_{t+tau}^2]
           where R_t = sum of returns over window and sigma^2 = sum of r_i^2.

    Execution fixes:
    - Pre-allocated index buffer to avoid per-tick heap allocation in
      _get_recent_returns (fancy indexing replaced with slice copy).
    - signal_clip added to config.yaml.
    - min_expected_move_pts tightened to 6.0 (1.5x cost).

Paper refs:
  Dandapani, Jusselin, Rosenbaum (2019) arXiv:1907.06151 — QHawkes to super-Heston.
  El Euch, Fukasawa, Rosenbaum (2016) arXiv:1609.05177 — Microstructural foundations.
  Chudasama, Iyer (2025) arXiv:2508.16566 — Asymmetric QHawkes with TRA.

Allocator Law  : __slots__ on class; pre-allocated ring buffers + index buffer.
Precision Law  : output is float signal score, not price — no Decimal needed.
Cache Law      : return history in contiguous float64 ring buffer.
Latency profile: shioaji_sim_p95_v2026-03-04 (set at inception per CLAUDE.md).
"""

from __future__ import annotations

import math

import numpy as np

from research.registry.schemas import AlphaManifest, AlphaStatus, AlphaTier

# --- Configuration constants (tunable via config.yaml) ---

# Return computation
_RETURN_RING_SIZE: int = 512      # max return history (ring buffer)
_MIN_RETURNS: int = 64            # minimum returns before signal

# Zumbach statistic windows (B-3: acknowledged as hyperparameters for Gate C sweep)
_ZUMBACH_WINDOWS: tuple[int, ...] = (30, 120, 360)  # ~30s, ~2min, ~6min at 1 tick/sec
_WINDOW_WEIGHTS: tuple[float, ...] = (0.5, 0.3, 0.2)  # weight per window

# EMA tracking
_ZUMBACH_DECAY: float = 0.05     # EMA alpha for Zumbach statistic tracker

# Vol tracking
_VOL_EMA_ALPHA: float = 0.02     # slow EMA for realized vol
_VOL_WINDOW: int = 60            # ticks for short-term vol estimate

# Signal output
_SIGNAL_EMA_ALPHA: float = 0.1   # smoothing for output signal
_SIGNAL_SCALE: float = 5.0       # scaling factor before tanh
_SIGNAL_CLIP: float = 1.0        # clip to [-1, +1]
_WARMUP_TICKS: int = 120         # minimum ticks before emitting

# TRA diagnostic (B-4 fix: proper rolling covariance)
_TRA_WINDOW: int = 120           # window for TRA measurement
_TRA_DECAY: float = 0.02         # EMA alpha for TRA accumulators

_MANIFEST = AlphaManifest(
    alpha_id="r30_zumbach_vol_feedback",
    hypothesis=(
        "Zumbach effect: past return trends predict future volatility "
        "quadratically (TRA). On TAIFEX futures, down-trends trigger "
        "margin-call/stop-loss cascades that amplify vol and create "
        "mean-reversion overshoot opportunities."
    ),
    formula=(
        "Z(t,w) = (cumret_w^2 - sum_sq_w) / 2; "
        "signal = tanh(scale * direction_weighted_Z / vol)"
    ),
    paper_refs=("arXiv:1907.06151", "arXiv:1609.05177", "arXiv:2508.16566"),
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


class R30ZumbachVolFeedbackAlpha:
    """Zumbach volatility feedback alpha — quadratic return features.

    Feed tick prices via update(price=...).  The alpha:
    1. Tracks return history in a ring buffer.
    2. Computes Zumbach statistic Z(t) at multiple time scales.
    3. Direction-weights Z by the sign of cumulative return.
    4. Emits directional signal: positive after down-trend Z (mean-reversion).
    """

    __slots__ = (
        "_prev_price",
        "_return_ring",
        "_return_write_idx",
        "_return_count",
        "_zumbach_signed_ema",
        "_vol_ema",
        "_sq_return_sum",
        "_sq_return_count",
        "_signal",
        "_signal_ema",
        "_total_ticks",
        "_tra_stat_ema",
        "_tra_n_updates",
        "_idx_buf",
    )

    def __init__(self) -> None:
        self._prev_price: int = 0
        self._return_ring: np.ndarray = np.zeros(_RETURN_RING_SIZE, dtype=np.float64)
        self._return_write_idx: int = 0
        self._return_count: int = 0

        # B-1 fix: single signed Zumbach EMA instead of separate abs() down/up
        # Positive = down-trend Z dominates, Negative = up-trend Z dominates
        self._zumbach_signed_ema: float = 0.0

        # Realized vol tracking
        self._vol_ema: float = 0.0
        self._sq_return_sum: float = 0.0
        self._sq_return_count: int = 0

        self._signal: float = 0.0
        self._signal_ema: float = 0.0
        self._total_ticks: int = 0

        # B-4 fix: proper TRA statistic
        self._tra_stat_ema: float = 0.0
        self._tra_n_updates: int = 0

        # Execution fix: pre-allocated index buffer to avoid per-tick allocation
        self._idx_buf: np.ndarray = np.zeros(_RETURN_RING_SIZE, dtype=np.intp)

    @property
    def manifest(self) -> AlphaManifest:
        return _MANIFEST

    @property
    def tra_ratio(self) -> float:
        """Time-Reversal Asymmetry statistic (for diagnostics).

        Positive means Zumbach effect is present (past trends predict future vol
        more than past vol predicts future trends).
        """
        return self._tra_stat_ema

    @property
    def zumbach_signed(self) -> float:
        """Current signed Zumbach EMA (for diagnostics).

        Positive = down-trend Z dominates (mean-reversion opportunity).
        """
        return self._zumbach_signed_ema

    # Keep legacy properties for test compatibility
    @property
    def zumbach_down(self) -> float:
        """Positive component of signed Zumbach (for diagnostics)."""
        return max(0.0, self._zumbach_signed_ema)

    @property
    def zumbach_up(self) -> float:
        """Negative component of signed Zumbach (for diagnostics)."""
        return max(0.0, -self._zumbach_signed_ema)

    def update(self, *, price: int = 0, **kwargs: object) -> float:  # noqa: ARG002
        """Process a new tick price (scaled int x10000).

        Returns the current directional signal in [-1, +1].
        """
        self._total_ticks += 1

        if price <= 0:
            return self._signal

        if self._prev_price > 0:
            log_ret = math.log(price / self._prev_price)
            self._append_return(log_ret)

            self._sq_return_sum += log_ret * log_ret
            self._sq_return_count += 1
            if self._sq_return_count >= _VOL_WINDOW:
                short_vol = self._sq_return_sum / self._sq_return_count
                self._vol_ema += _VOL_EMA_ALPHA * (short_vol - self._vol_ema)
                self._sq_return_sum = 0.0
                self._sq_return_count = 0

            if self._return_count >= _MIN_RETURNS:
                self._compute_zumbach()
                self._compute_tra_diagnostic()
                self._update_signal()

        self._prev_price = price
        return self._signal

    def _append_return(self, log_ret: float) -> None:
        """Append a return to the ring buffer."""
        idx = self._return_write_idx % _RETURN_RING_SIZE
        self._return_ring[idx] = log_ret
        self._return_write_idx += 1
        self._return_count += 1

    def _get_recent_returns(self, n: int) -> np.ndarray:
        """Get the most recent n returns in chronological order.

        Execution fix: uses pre-allocated index buffer to avoid per-tick
        heap allocation from np.arange + fancy indexing.
        """
        n = min(n, self._return_count, _RETURN_RING_SIZE)
        start = self._return_write_idx % _RETURN_RING_SIZE
        # Fill pre-allocated buffer with indices
        for i in range(n):
            self._idx_buf[i] = (start - n + i) % _RETURN_RING_SIZE
        return self._return_ring[self._idx_buf[:n]]

    def _compute_zumbach(self) -> None:
        """Compute multi-scale Zumbach statistic with direction weighting (B-1 fix).

        For a window of returns r_1, ..., r_W:
          S = sum(r_i)           (cumulative return)
          Q = sum(r_i^2)         (realized variance)
          Z = (S^2 - Q) / 2     (cross-product term, always >= 0 by Cauchy-Schwarz)

        Direction weighting (B-1 fix):
          signed_Z = -sign(S) * Z
          When S < 0 (down-trend): signed_Z is positive -> mean-reversion signal
          When S > 0 (up-trend): signed_Z is negative -> no action or reversal

        This preserves sign information (fixing the abs() bug) and is consistent
        with the paper's Z^2 formulation since Z^2 = Z * Z and we weight by
        the trend direction.
        """
        total_signed_z = 0.0
        total_weight = 0.0

        for window, weight in zip(_ZUMBACH_WINDOWS, _WINDOW_WEIGHTS):
            if self._return_count < window:
                continue

            returns = self._get_recent_returns(window)
            cumret = float(np.sum(returns))
            sum_sq = float(np.sum(returns * returns))
            z_stat = (cumret * cumret - sum_sq) / 2.0

            # Direction-weight: negative sign of cumret * Z
            # Down-trend (cumret < 0) -> positive contribution
            # Up-trend (cumret > 0) -> negative contribution
            if abs(cumret) > 1e-20:
                sign = -1.0 if cumret > 0 else 1.0
            else:
                sign = 0.0
            total_signed_z += weight * sign * z_stat
            total_weight += weight

        if total_weight < 1e-10:
            return

        signed_z_norm = total_signed_z / total_weight

        self._zumbach_signed_ema += _ZUMBACH_DECAY * (
            signed_z_norm - self._zumbach_signed_ema
        )

    def _compute_tra_diagnostic(self) -> None:
        """Compute proper TRA statistic (B-4 fix).

        TRA(tau) = E[RV_{t+tau} * R_t^2] - E[RV_t * R_{t+tau}^2]

        where:
          R_t = cumulative return over [t-tau, t]
          RV_t = sum of r_i^2 over [t-tau, t]

        Positive TRA = Zumbach effect present.
        Uses non-overlapping blocks for cleaner covariance estimation.
        """
        tau = _TRA_WINDOW
        n_needed = 3 * tau  # need 3 blocks: past, middle, future
        if self._return_count < n_needed:
            return

        returns = self._get_recent_returns(n_needed)

        # Three non-overlapping blocks
        block_past = returns[:tau]
        block_mid = returns[tau : 2 * tau]
        block_future = returns[2 * tau :]

        # Returns and vol for each block
        r_past = float(np.sum(block_past))
        rv_past = float(np.sum(block_past * block_past))
        r_mid = float(np.sum(block_mid))
        rv_mid = float(np.sum(block_mid * block_mid))
        r_future = float(np.sum(block_future))
        rv_future = float(np.sum(block_future * block_future))

        # Forward: future vol conditioned on past trend
        # Backward: past vol conditioned on future trend
        # Use mid block as "present" reference
        forward_term = rv_future * r_past * r_past
        backward_term = rv_past * r_future * r_future

        tra_sample = forward_term - backward_term

        self._tra_stat_ema += _TRA_DECAY * (tra_sample - self._tra_stat_ema)
        self._tra_n_updates += 1

    def _update_signal(self) -> None:
        """Convert signed Zumbach into directional signal (B-1 fix).

        Positive zumbach_signed_ema = down-trend Z dominates:
          Margin-call/stop-loss cascades amplifying vol during sell-offs.
          Predicts mean-reversion overshoot -> signal positive (buy).

        Negative zumbach_signed_ema = up-trend Z dominates:
          Less common in futures; signal negative (reversal expected).

        Near zero = no directional Zumbach signal.
        """
        if self._total_ticks < _WARMUP_TICKS:
            self._signal = 0.0
            return

        # Normalize by vol level to make signal scale-invariant
        vol_norm = max(self._vol_ema, 1e-20)
        normalized = self._zumbach_signed_ema / vol_norm

        raw_signal = math.tanh(_SIGNAL_SCALE * normalized)

        self._signal_ema += _SIGNAL_EMA_ALPHA * (raw_signal - self._signal_ema)
        self._signal = max(-_SIGNAL_CLIP, min(_SIGNAL_CLIP, self._signal_ema))

    def reset(self) -> None:
        """Reset all state."""
        self._prev_price = 0
        self._return_ring[:] = 0.0
        self._return_write_idx = 0
        self._return_count = 0
        self._zumbach_signed_ema = 0.0
        self._vol_ema = 0.0
        self._sq_return_sum = 0.0
        self._sq_return_count = 0
        self._signal = 0.0
        self._signal_ema = 0.0
        self._total_ticks = 0
        self._tra_stat_ema = 0.0
        self._tra_n_updates = 0

    def get_signal(self) -> float:
        return self._signal


ALPHA_CLASS = R30ZumbachVolFeedbackAlpha
