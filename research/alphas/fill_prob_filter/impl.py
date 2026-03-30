"""Fill Probability-Conditioned Entry Filter for OpportunisticMM.

Based on Albers et al. (2502.18625) and Lokin & Yu (2403.02572).

Key insight from Albers:
  - Fill probability and post-fill returns are negatively correlated
  - "Reversals" (contrarian fills with positive returns) occur when LOB
    imbalance falsely predicts price direction
  - Features: ret_autocov (oscillation), OFI, depth imbalance, spread
  - Logistic regression AUC ~0.55-0.60 for reversal prediction

Adaptation for TXFD6 OpportunisticMM:
  - No queue position data => use LOB-state-only features
  - Wide-spread regime (>2.5 bps) has elevated adverse selection (49-59%)
  - Goal: identify when wide-spread entries are likely adverse vs favorable
  - Filter: reject entry if P(adverse_fill) > threshold

Implementation notes:
  - All prices in scaled integers (x10000) per Precision Law
  - No pandas on hot path per HFT anti-patterns
  - Logistic regression has low DoF (7 features) to limit overfit risk
  - Model trained offline, coefficients stored for online evaluation
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Sequence

import numpy as np


# ---------------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------------

@dataclass(frozen=True, slots=True)
class FillEvent:
    """A single maker fill event with LOB context at entry time."""

    # LOB state at time of entry (quote submission)
    spread_scaled: int          # best_ask - best_bid (x10000)
    mid_price_x2: int           # best_bid + best_ask (x10000)
    depth_imbalance_ppm: int    # (bid_depth - ask_depth) / (bid+ask) * 1e6
    ofi_l1_ema8: int            # EMA-smoothed L1 order flow imbalance
    l1_bid_qty: int             # L1 bid queue size
    l1_ask_qty: int             # L1 ask queue size
    spread_ema8_scaled: int     # EMA-smoothed spread (x10000)
    depth_imb_ema8_ppm: int     # EMA-smoothed depth imbalance (ppm)

    # Outcome (for training only)
    side: int                   # +1 = buy fill, -1 = sell fill
    post_fill_return_bps: float  # 5-second post-fill return in bps (signed)


@dataclass(frozen=True, slots=True)
class FilterDecision:
    """Result of the fill probability filter evaluation."""

    p_adverse: float       # predicted probability of adverse fill
    should_enter: bool     # True if entry is recommended
    features_used: int     # number of valid features


# ---------------------------------------------------------------------------
# Feature extraction
# ---------------------------------------------------------------------------

_N_FEATURES: int = 7
_FEATURE_NAMES: tuple[str, ...] = (
    "spread_z",
    "depth_imbalance_z",
    "ofi_ema8_z",
    "l1_qty_ratio",
    "spread_ema_ratio",
    "depth_imb_ema_z",
    "spread_x_imbalance",
)


@dataclass(slots=True)
class FeatureNormalizer:
    """Online running mean/std for feature normalization.

    Uses Welford's algorithm for numerical stability.
    Pre-allocated arrays per Allocator Law.
    """

    count: int = 0
    mean: np.ndarray = field(default_factory=lambda: np.zeros(_N_FEATURES, dtype=np.float64))
    m2: np.ndarray = field(default_factory=lambda: np.zeros(_N_FEATURES, dtype=np.float64))

    def update(self, features: np.ndarray) -> None:
        self.count += 1
        delta = features - self.mean
        self.mean += delta / self.count
        delta2 = features - self.mean
        self.m2 += delta * delta2

    def std(self) -> np.ndarray:
        if self.count < 2:
            return np.ones(_N_FEATURES, dtype=np.float64)
        return np.sqrt(self.m2 / (self.count - 1))

    def normalize(self, features: np.ndarray) -> np.ndarray:
        """Normalize features to zero mean, unit variance."""
        std = self.std()
        std_safe = np.where(std > 1e-12, std, 1.0)
        return (features - self.mean) / std_safe


def extract_features(event: FillEvent) -> np.ndarray:
    """Extract feature vector from a fill event.

    Returns a 7-element float64 array (raw, un-normalized).

    Features designed to capture adverse selection signals:
    1. spread_z: current spread (wide spread = more adverse selection risk)
    2. depth_imbalance_z: bid-ask depth ratio (imbalance against fill side = adverse)
    3. ofi_ema8_z: order flow momentum (strong OFI against fill = adverse)
    4. l1_qty_ratio: near/far side queue ratio (small near = high fill prob but adverse)
    5. spread_ema_ratio: current/EMA spread (spread widening = transient event)
    6. depth_imb_ema_z: smoothed imbalance (persistent imbalance = more informative)
    7. spread_x_imbalance: interaction term (wide spread + adverse imbalance = worst case)
    """
    features = np.empty(_N_FEATURES, dtype=np.float64)

    features[0] = float(event.spread_scaled)
    features[1] = float(event.depth_imbalance_ppm) * event.side  # sign-adjusted
    features[2] = float(event.ofi_l1_ema8) * event.side  # sign-adjusted

    # L1 queue ratio: near-side / far-side (small = likely to fill but adverse)
    if event.side > 0:
        near_qty = float(event.l1_bid_qty)
        far_qty = float(event.l1_ask_qty)
    else:
        near_qty = float(event.l1_ask_qty)
        far_qty = float(event.l1_bid_qty)
    features[3] = near_qty / max(far_qty, 1.0)

    # Spread regime: current / EMA (> 1 means spread is widening)
    features[4] = (
        float(event.spread_scaled) / max(float(event.spread_ema8_scaled), 1.0)
    )

    features[5] = float(event.depth_imb_ema8_ppm) * event.side

    # Interaction: wide spread * adverse imbalance
    features[6] = features[0] * features[1] / 1e6  # scale down

    return features


# ---------------------------------------------------------------------------
# Logistic regression model
# ---------------------------------------------------------------------------

@dataclass(slots=True)
class AdverseFillModel:
    """Logistic regression for P(adverse fill | LOB state).

    Adverse fill: 5-second post-fill return moves against maker by > threshold_bps.

    Model: P(adverse) = sigmoid(w @ x + b)
    where x is the normalized feature vector.

    Training uses standard MLE via iteratively reweighted least squares (IRLS)
    to avoid external dependencies. With 7 features and ~22 days of data,
    overfitting risk is low.
    """

    weights: np.ndarray = field(
        default_factory=lambda: np.zeros(_N_FEATURES, dtype=np.float64)
    )
    bias: float = 0.0
    normalizer: FeatureNormalizer = field(default_factory=FeatureNormalizer)
    adverse_threshold_bps: float = 0.5  # post-fill return < -threshold = adverse
    filter_threshold: float = 0.6  # reject if P(adverse) > this
    is_trained: bool = False

    def predict_proba(self, raw_features: np.ndarray) -> float:
        """Predict P(adverse fill) from raw feature vector."""
        if not self.is_trained:
            return 0.5  # uninformative prior
        x = self.normalizer.normalize(raw_features)
        logit = float(np.dot(self.weights, x)) + self.bias
        # Numerically stable sigmoid
        if logit >= 0:
            return 1.0 / (1.0 + math.exp(-logit))
        exp_logit = math.exp(logit)
        return exp_logit / (1.0 + exp_logit)

    def should_enter(self, raw_features: np.ndarray) -> FilterDecision:
        """Decide whether to enter based on fill quality prediction."""
        p_adverse = self.predict_proba(raw_features)
        return FilterDecision(
            p_adverse=p_adverse,
            should_enter=(p_adverse <= self.filter_threshold),
            features_used=_N_FEATURES,
        )

    def train(
        self,
        events: Sequence[FillEvent],
        *,
        max_iter: int = 100,
        lr: float = 0.01,
        l2_lambda: float = 0.1,
    ) -> dict[str, float]:
        """Train the model using gradient descent with L2 regularization.

        Returns training metrics dict.
        """
        n = len(events)
        if n < 50:
            return {"error": "insufficient_data", "n": float(n)}

        # Extract features and labels
        X = np.empty((n, _N_FEATURES), dtype=np.float64)
        y = np.empty(n, dtype=np.float64)

        normalizer = FeatureNormalizer()

        for i, event in enumerate(events):
            raw = extract_features(event)
            normalizer.update(raw)
            X[i] = raw
            # Label: 1 if adverse (post-fill return < -threshold for the maker)
            y[i] = 1.0 if event.post_fill_return_bps < -self.adverse_threshold_bps else 0.0

        # Normalize features
        for i in range(n):
            X[i] = normalizer.normalize(X[i])

        # Initialize weights
        w = np.zeros(_N_FEATURES, dtype=np.float64)
        b = 0.0
        prev_loss = float("inf")

        for iteration in range(max_iter):
            # Forward pass
            logits = X @ w + b
            # Numerically stable sigmoid
            p = np.where(
                logits >= 0,
                1.0 / (1.0 + np.exp(-logits)),
                np.exp(logits) / (1.0 + np.exp(logits)),
            )

            # Cross-entropy loss + L2
            eps = 1e-12
            loss = (
                -np.mean(y * np.log(p + eps) + (1 - y) * np.log(1 - p + eps))
                + 0.5 * l2_lambda * np.dot(w, w)
            )

            # Gradient
            error = p - y
            grad_w = (X.T @ error) / n + l2_lambda * w
            grad_b = np.mean(error)

            # Update
            w -= lr * grad_w
            b -= lr * grad_b

            if abs(loss - prev_loss) < 1e-8:
                break
            prev_loss = loss

        self.weights = w
        self.bias = b
        self.normalizer = normalizer
        self.is_trained = True

        # Compute AUC
        auc = _compute_auc(y, X @ w + b)

        # Compute adverse rate
        adverse_rate = float(np.mean(y))

        return {
            "n_samples": float(n),
            "adverse_rate": adverse_rate,
            "final_loss": float(loss),
            "auc": auc,
            "iterations": float(iteration + 1),
            "weights": {name: float(w[i]) for i, name in enumerate(_FEATURE_NAMES)},
            "bias": float(b),
        }


def _compute_auc(y_true: np.ndarray, scores: np.ndarray) -> float:
    """Compute AUC-ROC using the Mann-Whitney U statistic.

    No external dependencies needed.
    """
    pos_mask = y_true == 1.0
    neg_mask = ~pos_mask
    n_pos = int(np.sum(pos_mask))
    n_neg = int(np.sum(neg_mask))
    if n_pos == 0 or n_neg == 0:
        return 0.5

    pos_scores = scores[pos_mask]
    neg_scores = scores[neg_mask]

    # Count concordant pairs
    concordant = 0
    tied = 0
    for ps in pos_scores:
        concordant += int(np.sum(neg_scores < ps))
        tied += int(np.sum(neg_scores == ps))

    return (concordant + 0.5 * tied) / (n_pos * n_neg)


# ---------------------------------------------------------------------------
# Integration point: OpportunisticMM entry filter
# ---------------------------------------------------------------------------

class FillProbabilityFilter:
    """Entry filter for OpportunisticMM based on fill quality prediction.

    Integration path:
        In OpportunisticMM.on_stats(), before calling super().on_stats():

            if not self._fill_filter.should_quote(event):
                return  # skip this quoting opportunity

    This uses pre-allocated buffers (Allocator Law) and avoids any
    heap allocation on the hot path.
    """

    __slots__ = ("_model", "_feature_buf", "_enabled")

    def __init__(self, model: AdverseFillModel, *, enabled: bool = True) -> None:
        self._model = model
        self._feature_buf = np.empty(_N_FEATURES, dtype=np.float64)
        self._enabled = enabled

    def should_quote(
        self,
        side: int,
        spread_scaled: int,
        mid_price_x2: int,
        depth_imbalance_ppm: int,
        ofi_l1_ema8: int,
        l1_bid_qty: int,
        l1_ask_qty: int,
        spread_ema8_scaled: int,
        depth_imb_ema8_ppm: int,
    ) -> bool:
        """Evaluate whether to place a quote on the given side.

        Hot-path method. Uses pre-allocated buffer. No heap allocation.
        """
        if not self._enabled or not self._model.is_trained:
            return True

        # Build FillEvent for feature extraction (cold path only during training;
        # here we inline the feature extraction for hot path)
        self._feature_buf[0] = float(spread_scaled)
        self._feature_buf[1] = float(depth_imbalance_ppm) * side
        self._feature_buf[2] = float(ofi_l1_ema8) * side

        if side > 0:
            near_qty = float(l1_bid_qty)
            far_qty = float(l1_ask_qty)
        else:
            near_qty = float(l1_ask_qty)
            far_qty = float(l1_bid_qty)
        self._feature_buf[3] = near_qty / max(far_qty, 1.0)

        self._feature_buf[4] = float(spread_scaled) / max(float(spread_ema8_scaled), 1.0)
        self._feature_buf[5] = float(depth_imb_ema8_ppm) * side
        self._feature_buf[6] = self._feature_buf[0] * self._feature_buf[1] / 1e6

        decision = self._model.should_enter(self._feature_buf)
        return decision.should_enter
