"""Cross-Asset OFI Network Alpha — Unit 13.

Extends the cross_ofi_leader concept (Paper 2112.13213, Cont, Cucuringu, Zhang
2021) to a full N-leader network model.  Instead of a single fixed leader
symbol, this alpha tracks OFI flows across N configurable leader symbols and
computes a weighted cross-impact signal using dynamically estimated
correlation-based leader weights.

Signal:
    Network_OFI_t = (1 - total_leader_weight) * self_OFI_t
                    + sum_i( w_i * leader_OFI_i_t )

    where w_i is proportional to the rolling Pearson correlation between
    leader_i's OFI and the target symbol's lagged return signal, normalised
    so that sum(w_i) <= cross_weight_cap (default 0.6).

    self_OFI = EMA_32( (Δbid_qty - Δask_qty) / (|Δbid_qty| + |Δask_qty| + 1) )
    leader_OFI_i = same formula applied on leader_i's L1 data (supplied externally).

Rationale:
    Multi-leader cross-asset OFI captures broader sector/market information
    flow.  Adaptive correlation weighting lets the network automatically
    up-weight leaders with stronger predictive relationships and down-weight
    leaders that have temporarily decoupled.  Compared to the fixed 50/50
    self/leader split in cross_ofi_leader, this improves IC in multi-sector
    portfolios.

Implementation notes:
    - update() accepts `leader_ofis` keyword: dict[str, float] mapping
      leader symbol id -> pre-computed OFI value, as produced by StrategyRunner.
    - Weights are updated lazily every `weight_update_interval` ticks via a
      rolling window of length `corr_window`.
    - When leader_ofis is absent or empty, falls back to pure self-OFI.
    - All leader weights are clipped to [0, 1] and normalised; total leader
      weight is further capped at `cross_weight_cap`.

Allocator Law  : numpy arrays pre-allocated in __init__; scalar EMA state.
Precision Law  : signal ∈ [-1, 1], float is fine (research module, Rule 11).
Async Law      : O(N) per weight_update_interval ticks; O(1) otherwise.
Latency profile: shioaji_sim_p95_v2026-03-04.
"""
from __future__ import annotations

import math

import numpy as np

from research.registry.schemas import AlphaManifest, AlphaStatus, AlphaTier

# ---------------------------------------------------------------------------
# Module-level constants
# ---------------------------------------------------------------------------
_EMA_WINDOW: int = 32
_EMA_ALPHA: float = 1.0 - math.exp(-1.0 / _EMA_WINDOW)

_DEFAULT_N_LEADERS: int = 5
_DEFAULT_CORR_WINDOW: int = 128   # rolling window for correlation estimation
_DEFAULT_WEIGHT_UPDATE_INTERVAL: int = 16  # ticks between weight recalculations
_DEFAULT_CROSS_WEIGHT_CAP: float = 0.6    # max total weight allocated to leaders

_MANIFEST = AlphaManifest(
    alpha_id="cross_ofi_network",
    hypothesis=(
        "A full N-leader OFI network with correlation-based adaptive weights "
        "captures broader sector and market-wide information flow than a "
        "single fixed-leader model.  Dynamically upweighting leaders whose "
        "OFI is more correlated with the target's future return improves IC "
        "in multi-sector portfolios."
    ),
    formula=(
        "Network_OFI_t = (1 - sum(w_i)) * self_OFI_t + sum_i(w_i * leader_OFI_i_t), "
        "where w_i ~ corr(leader_OFI_i, self_OFI_lagged), normalised to sum <= cross_weight_cap"
    ),
    paper_refs=("2112.13213",),
    data_fields=("bid_qty", "ask_qty"),
    complexity="O(N)",
    status=AlphaStatus.DRAFT,
    tier=AlphaTier.TIER_2,
    rust_module=None,
    latency_profile="shioaji_sim_p95_v2026-03-04",
    roles_used=("planner", "code-reviewer"),
    skills_used=("iterative-retrieval", "validation-gate"),
    feature_set_version="lob_shared_v1",
)


class CrossOfiNetworkAlpha:
    """Cross-asset OFI network alpha with N leaders and adaptive correlation weights.

    Parameters
    ----------
    n_leaders:
        Maximum number of leader symbols to track (default 5).
    corr_window:
        Rolling window length used to compute Pearson correlation for weight
        estimation (default 128 ticks).
    weight_update_interval:
        Number of ticks between weight recalculations (default 16).
    cross_weight_cap:
        Maximum total weight allocated to leader OFIs; the remainder goes to
        self-OFI (default 0.6, meaning at least 0.4 is always self-weight).

    update() signature:
        update(bid_qty, ask_qty, *, leader_ofis=None) -> float

        leader_ofis: dict[str, float] | None
            Maps leader symbol id to its pre-computed OFI value (already
            normalised to [-1, 1]).  Symbols not present in a given tick are
            treated as missing (last-value hold).
    """

    __slots__ = (
        # Self OFI state
        "_prev_bid_qty",
        "_prev_ask_qty",
        "_self_ema",
        "_initialized",
        "_tick_count",
        # Leader tracking (pre-allocated numpy buffers)
        "_n_leaders",
        "_leader_ids",          # list[str], registered in insertion order
        "_leader_ema",          # shape (max_leaders,) — smoothed leader OFI
        "_leader_weights",      # shape (max_leaders,) — correlation-based weights
        "_corr_window",
        "_weight_update_interval",
        "_cross_weight_cap",
        # Rolling buffers for correlation estimation
        "_self_ofi_buf",        # shape (corr_window,) ring buffer
        "_leader_ofi_bufs",     # shape (max_leaders, corr_window) ring buffer
        "_buf_idx",             # current write position in ring buffer
        "_buf_full",            # True once ring buffer has been filled once
        # Output
        "_signal",
    )

    def __init__(
        self,
        n_leaders: int = _DEFAULT_N_LEADERS,
        corr_window: int = _DEFAULT_CORR_WINDOW,
        weight_update_interval: int = _DEFAULT_WEIGHT_UPDATE_INTERVAL,
        cross_weight_cap: float = _DEFAULT_CROSS_WEIGHT_CAP,
    ) -> None:
        if n_leaders < 1:
            raise ValueError(f"n_leaders must be >= 1, got {n_leaders}")
        if corr_window < 4:
            raise ValueError(f"corr_window must be >= 4, got {corr_window}")
        if not (0.0 <= cross_weight_cap <= 1.0):
            raise ValueError(f"cross_weight_cap must be in [0, 1], got {cross_weight_cap}")

        self._prev_bid_qty: float = 0.0
        self._prev_ask_qty: float = 0.0
        self._self_ema: float = 0.0
        self._initialized: bool = False
        self._tick_count: int = 0
        self._signal: float = 0.0

        self._n_leaders: int = n_leaders
        self._leader_ids: list[str] = []
        self._leader_ema: np.ndarray = np.zeros(n_leaders, dtype=np.float64)
        self._leader_weights: np.ndarray = np.zeros(n_leaders, dtype=np.float64)

        self._corr_window: int = corr_window
        self._weight_update_interval: int = weight_update_interval
        self._cross_weight_cap: float = cross_weight_cap

        # Pre-allocated ring buffers
        self._self_ofi_buf: np.ndarray = np.zeros(corr_window, dtype=np.float64)
        self._leader_ofi_bufs: np.ndarray = np.zeros((n_leaders, corr_window), dtype=np.float64)
        self._buf_idx: int = 0
        self._buf_full: bool = False

    @property
    def manifest(self) -> AlphaManifest:
        return _MANIFEST

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def update(
        self,
        bid_qty: float = 0.0,
        ask_qty: float = 0.0,
        *,
        leader_ofis: dict[str, float] | None = None,
    ) -> float:
        """Update alpha state and return current signal.

        Parameters
        ----------
        bid_qty, ask_qty:
            Best-bid and best-ask quantities for the target symbol (L1).
        leader_ofis:
            Optional mapping of leader symbol id -> pre-computed OFI value.
            Each value should be in approximately [-1, 1].
        """
        bid_qty = float(bid_qty)
        ask_qty = float(ask_qty)
        self._tick_count += 1

        # Initialisation tick: store previous state, pre-register any leaders, return zero.
        if not self._initialized:
            self._prev_bid_qty = bid_qty
            self._prev_ask_qty = ask_qty
            self._initialized = True
            # Pre-register leaders on init tick so they are ready from tick 2 onward.
            if leader_ofis:
                for sym in leader_ofis:
                    self._get_or_register_leader(sym)
                self._rebalance_equal_weights()
            return 0.0

        # --- Self OFI ---
        d_bid = bid_qty - self._prev_bid_qty
        d_ask = ask_qty - self._prev_ask_qty
        activity = abs(d_bid) + abs(d_ask) + 1.0
        raw_self = (d_bid - d_ask) / activity
        self._self_ema += _EMA_ALPHA * (raw_self - self._self_ema)

        self._prev_bid_qty = bid_qty
        self._prev_ask_qty = ask_qty

        # --- Leader OFIs ---
        n_active_before = len(self._leader_ids)
        if leader_ofis:
            self._process_leader_ofis(leader_ofis)
        # If new leaders were registered, assign equal weights immediately.
        if len(self._leader_ids) > n_active_before:
            self._rebalance_equal_weights()

        # --- Update ring buffers ---
        idx = self._buf_idx
        self._self_ofi_buf[idx] = self._self_ema
        self._buf_idx = (idx + 1) % self._corr_window
        if self._buf_idx == 0:
            self._buf_full = True

        # --- Recompute weights periodically ---
        n_active = len(self._leader_ids)
        if (
            n_active > 0
            and (self._tick_count % self._weight_update_interval) == 0
            and (self._buf_full or self._buf_idx >= 8)
        ):
            self._update_leader_weights()

        # --- Combine self + leaders ---
        self._signal = self._compute_signal()
        return self._signal

    def get_signal(self) -> float:
        """Return the most recently computed signal value."""
        return self._signal

    def reset(self) -> None:
        """Reset all internal state to initial conditions."""
        self._prev_bid_qty = 0.0
        self._prev_ask_qty = 0.0
        self._self_ema = 0.0
        self._initialized = False
        self._tick_count = 0
        self._signal = 0.0
        self._leader_ids.clear()
        self._leader_ema[:] = 0.0
        self._leader_weights[:] = 0.0
        self._self_ofi_buf[:] = 0.0
        self._leader_ofi_bufs[:] = 0.0
        self._buf_idx = 0
        self._buf_full = False

    def get_leader_weights(self) -> dict[str, float]:
        """Return a copy of current leader weights, keyed by symbol id."""
        return {
            sym: float(self._leader_weights[i])
            for i, sym in enumerate(self._leader_ids)
        }

    def get_leader_count(self) -> int:
        """Return the number of registered leader symbols."""
        return len(self._leader_ids)

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    def _process_leader_ofis(self, leader_ofis: dict[str, float]) -> None:
        """Update leader EMA states and ring buffers from incoming OFI values."""
        for sym, raw_ofi in leader_ofis.items():
            idx = self._get_or_register_leader(sym)
            if idx is None:
                continue  # n_leaders capacity reached
            raw = float(raw_ofi)
            if not math.isfinite(raw):
                raw = 0.0
            self._leader_ema[idx] += _EMA_ALPHA * (raw - self._leader_ema[idx])
            self._leader_ofi_bufs[idx, self._buf_idx] = self._leader_ema[idx]

    def _get_or_register_leader(self, sym: str) -> int | None:
        """Return the array index for a leader symbol, registering if new."""
        for i, existing in enumerate(self._leader_ids):
            if existing == sym:
                return i
        if len(self._leader_ids) >= self._n_leaders:
            return None  # capacity full, ignore new symbol
        self._leader_ids.append(sym)
        return len(self._leader_ids) - 1

    def _rebalance_equal_weights(self) -> None:
        """Assign equal weights across all registered leaders up to cross_weight_cap.

        Called immediately on first registration so that leader contributions
        are non-zero from the very first active tick.  Correlation-based
        updates from _update_leader_weights() will refine these over time.
        """
        n_active = len(self._leader_ids)
        if n_active == 0:
            return
        equal_w = self._cross_weight_cap / n_active
        for i in range(n_active):
            self._leader_weights[i] = equal_w

    def _update_leader_weights(self) -> None:
        """Recompute leader weights using rolling Pearson correlation.

        For each leader i, weight_i = max(0, corr(self_ofi_buf, leader_ofi_buf_i)).
        Weights are normalised so their sum does not exceed cross_weight_cap.
        """
        n_active = len(self._leader_ids)
        if n_active == 0:
            return

        # Extract the valid portion of the ring buffer
        if self._buf_full:
            self_buf = self._self_ofi_buf
        else:
            self_buf = self._self_ofi_buf[: self._buf_idx]

        if self_buf.size < 4:
            return

        raw_weights = np.zeros(n_active, dtype=np.float64)
        self_std = float(np.std(self_buf))
        if self_std < 1e-12:
            # Self signal is constant — equal weights
            raw_weights[:n_active] = 1.0 / n_active
        else:
            for i in range(n_active):
                if self._buf_full:
                    leader_buf = self._leader_ofi_bufs[i]
                else:
                    leader_buf = self._leader_ofi_bufs[i, : self._buf_idx]

                corr = _pearson_corr(self_buf, leader_buf)
                raw_weights[i] = max(0.0, corr)  # only positive correlation is useful

        # Normalise to cross_weight_cap
        total = float(np.sum(raw_weights[:n_active]))
        if total > 1e-12:
            scale = self._cross_weight_cap / total
            for i in range(n_active):
                self._leader_weights[i] = raw_weights[i] * scale
        else:
            # No positive correlations — distribute equally up to cap
            equal_w = self._cross_weight_cap / n_active
            for i in range(n_active):
                self._leader_weights[i] = equal_w

    def _compute_signal(self) -> float:
        """Combine self OFI and leader OFIs using current weights."""
        n_active = len(self._leader_ids)
        if n_active == 0:
            return self._self_ema

        leader_contribution = 0.0
        for i in range(n_active):
            leader_contribution += float(self._leader_weights[i]) * float(self._leader_ema[i])

        total_leader_weight = float(np.sum(self._leader_weights[:n_active]))
        self_weight = max(0.0, 1.0 - total_leader_weight)
        return self_weight * self._self_ema + leader_contribution


# ---------------------------------------------------------------------------
# Module-level helpers (not in class to avoid __slots__ friction)
# ---------------------------------------------------------------------------

def _pearson_corr(x: np.ndarray, y: np.ndarray) -> float:
    """Compute Pearson correlation between two 1-D arrays.

    Returns 0.0 for degenerate cases (constant series, mismatched lengths,
    empty arrays, or non-finite values).
    """
    n = min(x.size, y.size)
    if n < 4:
        return 0.0
    xv = np.asarray(x[:n], dtype=np.float64)
    yv = np.asarray(y[:n], dtype=np.float64)
    if not (np.all(np.isfinite(xv)) and np.all(np.isfinite(yv))):
        mask = np.isfinite(xv) & np.isfinite(yv)
        xv = xv[mask]
        yv = yv[mask]
        if xv.size < 4:
            return 0.0

    xm = xv - float(np.mean(xv))
    ym = yv - float(np.mean(yv))
    denom = float(np.sqrt(np.dot(xm, xm) * np.dot(ym, ym)))
    if denom < 1e-12:
        return 0.0
    return float(np.clip(np.dot(xm, ym) / denom, -1.0, 1.0))


ALPHA_CLASS = CrossOfiNetworkAlpha

__all__ = [
    "CrossOfiNetworkAlpha",
    "ALPHA_CLASS",
    "_MANIFEST",
    "_pearson_corr",
    "_EMA_ALPHA",
    "_DEFAULT_N_LEADERS",
    "_DEFAULT_CORR_WINDOW",
    "_DEFAULT_WEIGHT_UPDATE_INTERVAL",
    "_DEFAULT_CROSS_WEIGHT_CAP",
]
