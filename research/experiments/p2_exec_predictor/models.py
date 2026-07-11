"""Self-contained binary logistic for fill / adverse predictors.

Why not reuse ``research/alphas/fill_prob_filter/impl.py``?
    That module's ``AdverseFillModel.train`` consumes a list of
    ``FillEvent`` dataclasses, which is the wrong shape for the panel
    workflow here (we have NumPy arrays already). We keep the same
    Welford-style normalizer + GD-with-L2 logistic but expose an
    ``(X, y)`` fit interface and add per-decile calibration support.

No external ML dependency (no sklearn) — staying numpy-only matches the
rest of the research toolkit.
"""
from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np


@dataclass(slots=True)
class FeatureNormalizer:
    """Batch z-score normalizer (mean / sample std)."""

    n_features: int = 0
    mean: np.ndarray = field(default_factory=lambda: np.zeros(0))
    m2: np.ndarray = field(default_factory=lambda: np.zeros(0))
    count: int = 0

    @classmethod
    def fit(cls, X: np.ndarray) -> "FeatureNormalizer":
        n_features = X.shape[1]
        mean = X.mean(axis=0)
        var = X.var(axis=0, ddof=1) if X.shape[0] > 1 else np.ones(n_features)
        return cls(
            n_features=n_features,
            mean=mean,
            m2=var * max(X.shape[0] - 1, 1),
            count=X.shape[0],
        )

    def std(self) -> np.ndarray:
        if self.count < 2:
            return np.ones(self.n_features, dtype=np.float64)
        return np.sqrt(self.m2 / (self.count - 1))

    def transform(self, X: np.ndarray) -> np.ndarray:
        std = self.std()
        std_safe = np.where(std > 1e-12, std, 1.0)
        return (X - self.mean) / std_safe


@dataclass(slots=True)
class LogisticBinary:
    """Binary logistic regression, GD + L2."""

    weights: np.ndarray = field(default_factory=lambda: np.zeros(0))
    bias: float = 0.0
    normalizer: FeatureNormalizer = field(default_factory=FeatureNormalizer)
    is_trained: bool = False

    def fit(
        self,
        X: np.ndarray,
        y: np.ndarray,
        *,
        max_iter: int = 200,
        lr: float = 0.05,
        l2_lambda: float = 0.05,
        tol: float = 1e-7,
    ) -> dict[str, float | int | bool]:
        if X.ndim != 2 or y.ndim != 1 or X.shape[0] != y.shape[0]:
            raise ValueError(f"shape mismatch X={X.shape} y={y.shape}")
        n, d = X.shape
        if n < 50:
            return {"error_insufficient_data": True, "n": n}

        self.normalizer = FeatureNormalizer.fit(X)
        Xn = self.normalizer.transform(X)
        w = np.zeros(d, dtype=np.float64)
        b = 0.0
        prev_loss = float("inf")
        loss = float("inf")
        n_iter = 0
        for n_iter in range(1, max_iter + 1):
            logits = Xn @ w + b
            p = _sigmoid_vec(logits)
            eps = 1e-12
            loss = (
                -float(np.mean(y * np.log(p + eps) + (1 - y) * np.log(1 - p + eps)))
                + 0.5 * l2_lambda * float(w @ w)
            )
            error = p - y
            grad_w = (Xn.T @ error) / n + l2_lambda * w
            grad_b = float(error.mean())
            w -= lr * grad_w
            b -= lr * grad_b
            if abs(prev_loss - loss) < tol:
                break
            prev_loss = loss

        self.weights = w
        self.bias = b
        self.is_trained = True

        scores = Xn @ w + b
        return {
            "n_samples": int(n),
            "n_features": int(d),
            "iterations": int(n_iter),
            "final_loss": float(loss),
            "auc": float(compute_auc(y, scores)),
            "brier": float(np.mean((_sigmoid_vec(scores) - y) ** 2)),
            "base_rate": float(y.mean()),
        }

    def predict_proba(self, X: np.ndarray) -> np.ndarray:
        if not self.is_trained:
            return np.full(X.shape[0], 0.5, dtype=np.float64)
        Xn = self.normalizer.transform(X)
        return _sigmoid_vec(Xn @ self.weights + self.bias)

    def to_dict(self) -> dict[str, list[float] | float | bool | int]:
        return {
            "weights": self.weights.tolist(),
            "bias": float(self.bias),
            "norm_mean": self.normalizer.mean.tolist(),
            "norm_m2": self.normalizer.m2.tolist(),
            "norm_count": int(self.normalizer.count),
            "is_trained": bool(self.is_trained),
        }


def _sigmoid_vec(x: np.ndarray) -> np.ndarray:
    out = np.empty_like(x, dtype=np.float64)
    pos = x >= 0
    out[pos] = 1.0 / (1.0 + np.exp(-x[pos]))
    e = np.exp(x[~pos])
    out[~pos] = e / (1.0 + e)
    return out


def compute_auc(y_true: np.ndarray, scores: np.ndarray) -> float:
    """ROC-AUC via Mann-Whitney U with tie-aware ranks."""
    pos = y_true == 1.0
    n_pos = int(pos.sum())
    n_neg = int(y_true.size - n_pos)
    if n_pos == 0 or n_neg == 0:
        return 0.5
    order = np.argsort(scores, kind="mergesort")
    ranks = np.empty_like(order, dtype=np.float64)
    ranks[order] = np.arange(1, scores.size + 1)
    sorted_scores = scores[order]
    i = 0
    while i < sorted_scores.size:
        j = i
        while j + 1 < sorted_scores.size and sorted_scores[j + 1] == sorted_scores[i]:
            j += 1
        if j > i:
            avg = (i + j + 2) / 2.0
            ranks[order[i : j + 1]] = avg
        i = j + 1
    sum_ranks_pos = float(ranks[pos].sum())
    return float((sum_ranks_pos - n_pos * (n_pos + 1) / 2.0) / (n_pos * n_neg))


def calibration_by_decile(
    y_true: np.ndarray, p_pred: np.ndarray, n_bins: int = 10
) -> list[dict[str, float | int]]:
    if y_true.size == 0:
        return []
    edges = np.quantile(p_pred, np.linspace(0, 1, n_bins + 1))
    edges[0] -= 1e-9
    edges[-1] += 1e-9
    bins = np.clip(np.digitize(p_pred, edges) - 1, 0, n_bins - 1)
    out: list[dict[str, float | int]] = []
    for b in range(n_bins):
        mask = bins == b
        n = int(mask.sum())
        if n == 0:
            continue
        out.append(
            {
                "bin": int(b),
                "n": n,
                "mean_p_pred": float(p_pred[mask].mean()),
                "empirical_rate": float(y_true[mask].mean()),
            }
        )
    return out


def stratified_metric(
    y_true: np.ndarray,
    scores: np.ndarray,
    strat: np.ndarray,
    n_bins: int = 5,
) -> list[dict[str, float | int]]:
    """Per-stratum AUC + base rate.

    Strata defined by quantile-binning ``strat`` (typically spread).
    Used to detect non-stationary cohorts (the F1-C / R65 failure mode).
    """
    if y_true.size == 0:
        return []
    finite = np.isfinite(strat)
    if not finite.any():
        return []
    edges = np.quantile(strat[finite], np.linspace(0, 1, n_bins + 1))
    edges[0] -= 1e-9
    edges[-1] += 1e-9
    bins = np.clip(np.digitize(strat, edges) - 1, 0, n_bins - 1)
    out: list[dict[str, float | int]] = []
    for b in range(n_bins):
        mask = (bins == b) & finite
        n = int(mask.sum())
        if n < 50:
            continue
        out.append(
            {
                "bin": int(b),
                "n": n,
                "stratum_mean": float(strat[mask].mean()),
                "auc": compute_auc(y_true[mask], scores[mask]),
                "base_rate": float(y_true[mask].mean()),
            }
        )
    return out
