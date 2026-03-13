"""markov_lob_inertia — Intraday LOB Price Change Markov Dynamics.

Signal: Tracks a 3-state Markov chain of price movements (UP/DOWN/FLAT).
Transition probabilities reveal price inertia — tendency to continue in the
same direction. High inertia amplifies momentum; low inertia signals
mean-reversion.

References:
  Paper 039: Intraday LOB Price Change Markov Dynamics

Formula:
  state     = classify(mid_price - prev_mid) -> {DOWN=0, FLAT=1, UP=2}
  T[i,j]   += alpha_decay * (indicator(i->j) - T[i,j])   (EMA-smoothed)
  p_up      = T[state, UP]   / row_sum
  p_down    = T[state, DOWN] / row_sum
  inertia   = T[state, state] / row_sum
  raw       = (p_up - p_down) * inertia
  signal   += alpha_ema * (raw - signal)                  in [-1, 1]

Allocator Law : __slots__ on class; all state is scalar/flat-array.
Precision Law : output is float (signal score, not price).
Cache Law     : 9-element flat array for 3x3 transition matrix.
"""
from __future__ import annotations

import math

from research.registry.schemas import AlphaManifest, AlphaStatus, AlphaTier

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
_EMA_ALPHA: float = 1.0 - math.exp(-1.0 / 8.0)  # ~0.1175, signal smoothing
_DECAY: float = 1.0 - math.exp(-1.0 / 32.0)  # ~0.0308, transition matrix decay
_EPSILON: float = 1e-8

# States
_DOWN: int = 0
_FLAT: int = 1
_UP: int = 2

# ---------------------------------------------------------------------------
# Manifest
# ---------------------------------------------------------------------------
_MANIFEST = AlphaManifest(
    alpha_id="markov_lob_inertia",
    hypothesis=(
        "Price change transition probabilities reveal inertia patterns: "
        "high self-transition probability signals momentum continuation, "
        "low self-transition signals mean-reversion opportunity."
    ),
    formula="signal = EMA_8((P(UP|state) - P(DOWN|state)) * P(state|state))",
    paper_refs=("039",),
    data_fields=("bid_qty", "ask_qty"),
    complexity="O(1)",
    status=AlphaStatus.DRAFT,
    tier=AlphaTier.ENSEMBLE,
    rust_module=None,
    roles_used=("planner",),
    skills_used=("iterative-retrieval",),
    feature_set_version="lob_shared_v1",
)


# ---------------------------------------------------------------------------
# Alpha implementation
# ---------------------------------------------------------------------------
class MarkovLobInertiaAlpha:
    """O(1) Markov-chain inertia alpha with EMA-smoothed transition matrix.

    State layout:
      _transitions: flat list of 9 floats (3x3 row-major transition counts)
      _current_state: last observed state (0=DOWN, 1=FLAT, 2=UP)
      _prev_mid: previous mid-price proxy for state classification
      _signal_ema: EMA-smoothed output signal
      _signal: cached signal value
      _initialized: whether we have seen at least one tick
    """

    __slots__ = (
        "_transitions",
        "_current_state",
        "_prev_mid",
        "_signal_ema",
        "_signal",
        "_initialized",
    )

    def __init__(self) -> None:
        # Pre-allocate 3x3 transition matrix as flat list (uniform prior)
        self._transitions: list[float] = [1.0] * 9
        self._current_state: int = _FLAT
        self._prev_mid: float = 0.0
        self._signal_ema: float = 0.0
        self._signal: float = 0.0
        self._initialized: bool = False

    @property
    def manifest(self) -> AlphaManifest:
        return _MANIFEST

    def _classify_state(self, mid: float) -> int:
        """Classify price change direction relative to previous mid."""
        diff = mid - self._prev_mid
        if diff > _EPSILON:
            return _UP
        elif diff < -_EPSILON:
            return _DOWN
        return _FLAT

    def _get_transition_probs(self, from_state: int) -> tuple[float, float, float]:
        """Return (p_down, p_flat, p_up) for the given source state."""
        base = from_state * 3
        t0 = self._transitions[base]
        t1 = self._transitions[base + 1]
        t2 = self._transitions[base + 2]
        row_sum = t0 + t1 + t2
        if row_sum < _EPSILON:
            return (1.0 / 3.0, 1.0 / 3.0, 1.0 / 3.0)
        inv = 1.0 / row_sum
        return (t0 * inv, t1 * inv, t2 * inv)

    def update(self, *args: float, **kwargs: float) -> float:
        """Update state and return the current signal.

        Accepts positional ``(bid_qty, ask_qty)`` or keyword args.
        Optional ``mid_price`` keyword for explicit mid-price.
        Also accepts ``bids`` / ``asks`` array-like for protocol compat.
        """
        # --- resolve bid_qty, ask_qty ---
        if args and len(args) >= 2:
            bid_qty = float(args[0])
            ask_qty = float(args[1])
        elif "bid_qty" in kwargs and "ask_qty" in kwargs:
            bid_qty = float(kwargs["bid_qty"])
            ask_qty = float(kwargs["ask_qty"])
        elif "bids" in kwargs and "asks" in kwargs:
            bids = kwargs["bids"]
            asks = kwargs["asks"]
            bid_qty = float(bids[0][1]) if hasattr(bids, "__getitem__") and len(bids) > 0 else 0.0
            ask_qty = float(asks[0][1]) if hasattr(asks, "__getitem__") and len(asks) > 0 else 0.0
        else:
            bid_qty = 0.0
            ask_qty = 0.0

        # --- resolve mid_price ---
        mid_price = float(kwargs.get("mid_price", 0.0)) if kwargs else 0.0

        # If mid_price not provided, use imbalance as directional proxy
        if abs(mid_price) < _EPSILON:
            total = bid_qty + ask_qty
            mid_price = (bid_qty - ask_qty) / max(total, 1.0)

        if not self._initialized:
            self._prev_mid = mid_price
            self._initialized = True
            self._signal = 0.0
            return self._signal

        # Classify new state
        new_state = self._classify_state(mid_price)

        # Update transition matrix with EMA decay (adapt to regime changes)
        base = self._current_state * 3
        for j in range(3):
            target = 1.0 if j == new_state else 0.0
            idx = base + j
            self._transitions[idx] += _DECAY * (target - self._transitions[idx])

        # Compute signal from transition probabilities of new state
        probs = self._get_transition_probs(new_state)
        p_down, _p_flat, p_up = probs
        inertia = probs[new_state]

        # raw signal: directional bias * inertia factor
        raw = (p_up - p_down) * inertia

        # EMA smoothing
        self._signal_ema += _EMA_ALPHA * (raw - self._signal_ema)

        # Clamp to [-1, 1]
        self._signal = max(-1.0, min(1.0, self._signal_ema))

        # Advance state
        self._current_state = new_state
        self._prev_mid = mid_price

        return self._signal

    def reset(self) -> None:
        """Clear all state to initial values."""
        self._transitions = [1.0] * 9
        self._current_state = _FLAT
        self._prev_mid = 0.0
        self._signal_ema = 0.0
        self._signal = 0.0
        self._initialized = False

    def get_signal(self) -> float:
        """Return cached signal from the last update() call."""
        return self._signal


ALPHA_CLASS = MarkovLobInertiaAlpha

__all__ = ["MarkovLobInertiaAlpha", "ALPHA_CLASS"]
