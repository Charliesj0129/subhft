from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

import numpy as np

from research.registry.schemas import AlphaManifest, AlphaStatus, AlphaTier

try:
    from scipy import stats as scipy_stats
except Exception:  # pragma: no cover - optional dependency fallback
    scipy_stats = None


@dataclass
class RegimeResult:
    score: float
    p_value: float
    is_shift: bool
    kl_div: float


class KLRegimeDetector:
    """KL-divergence based regime-shift detector."""

    def __init__(
        self,
        window_recent: int = 3000,
        window_ref: int = 18000,
        n_bins: int = 20,
        threshold_p: float = 0.01,
    ):
        self.window_recent = window_recent
        self.window_ref = window_ref
        self.n_bins = n_bins
        self.threshold_p = threshold_p
        self.capacity = window_ref + window_recent + 1000
        self.returns_buffer = np.zeros(self.capacity)
        self.ptr = 0
        self.count = 0
        self.is_full = False
        self._signal = 0.0
        self.last_result: RegimeResult | None = None

    def reset(self) -> None:
        self.returns_buffer = np.zeros(self.capacity)
        self.ptr = 0
        self.count = 0
        self.is_full = False
        self._signal = 0.0
        self.last_result = None

    def add_return(self, ret: float) -> None:
        self.returns_buffer[self.ptr] = ret
        self.ptr = (self.ptr + 1) % self.capacity
        if self.count < self.capacity:
            self.count += 1
        else:
            self.is_full = True

    def _get_recent_and_ref(self) -> tuple[np.ndarray, np.ndarray]:
        if self.count < (self.window_recent + self.window_ref):
            return np.array([]), np.array([])
        if not self.is_full:
            full_data = self.returns_buffer[: self.count]
        else:
            full_data = np.concatenate((self.returns_buffer[self.ptr :], self.returns_buffer[: self.ptr]))
        recent = full_data[-self.window_recent :]
        ref = full_data[-(self.window_recent + self.window_ref) : -self.window_recent]
        return recent, ref

    def _calculate_kl(self, p: np.ndarray, q: np.ndarray) -> float:
        epsilon = 1e-10
        p = p + epsilon
        q = q + epsilon
        p /= np.sum(p)
        q /= np.sum(q)
        return float(np.sum(p * np.log(p / q)))

    def compute(self, current_return: float) -> Optional[RegimeResult]:
        self.add_return(current_return)
        if self.count < (self.window_recent + self.window_ref):
            return None

        recent, ref = self._get_recent_and_ref()
        combined = np.concatenate([recent, ref])
        min_val, max_val = np.percentile(combined, [1, 99])
        if not np.isfinite(min_val) or not np.isfinite(max_val) or min_val == max_val:
            return None
        recent = np.clip(recent, min_val, max_val)
        ref = np.clip(ref, min_val, max_val)
        hist_recent, _ = np.histogram(recent, bins=self.n_bins, range=(min_val, max_val), density=False)
        hist_ref, _ = np.histogram(ref, bins=self.n_bins, range=(min_val, max_val), density=False)
        if np.sum(hist_recent) == 0 or np.sum(hist_ref) == 0:
            return None

        p = hist_recent / np.sum(hist_recent)
        q = hist_ref / np.sum(hist_ref)
        kl = self._calculate_kl(p, q)
        n = len(recent)
        m = len(ref)
        t_stat = float(2 * (n * m / (n + m)) * kl)
        p_val = _chi2_sf(t_stat, df=self.n_bins - 1)
        result = RegimeResult(
            score=t_stat,
            p_value=p_val,
            is_shift=(p_val < self.threshold_p),
            kl_div=kl,
        )
        self.last_result = result
        self._signal = 1.0 if result.is_shift else 0.0
        return result

    def get_signal(self) -> float:
        return self._signal


class KLRegimeAlpha(KLRegimeDetector):
    @property
    def manifest(self) -> AlphaManifest:
        return AlphaManifest(
            alpha_id="kl_regime",
            hypothesis="Distribution shift in returns indicates regime break and risk-state change.",
            formula="T = 2 * (nm/(n+m)) * D_KL(P_recent || P_ref)",
            paper_refs=("009",),
            data_fields=("current_return",),
            complexity="O(N)",
            status=AlphaStatus.DRAFT,
            tier=AlphaTier.ENSEMBLE,
            rust_module=None,
        )

    def update(self, *args, **kwargs) -> float:
        if args:
            current_return = float(args[0])
        else:
            current_return = float(kwargs.get("current_return", kwargs.get("ret", 0.0)))
        self.compute(current_return=current_return)
        return self.get_signal()

    def update_batch(self, data) -> np.ndarray:
        arr = np.asarray(data)
        if arr.size == 0:
            return np.zeros(0, dtype=np.float64)
        if arr.dtype.names:
            if "current_return" in arr.dtype.names:
                values = np.asarray(arr["current_return"], dtype=np.float64).reshape(-1)
            elif "ret" in arr.dtype.names:
                values = np.asarray(arr["ret"], dtype=np.float64).reshape(-1)
            elif "returns" in arr.dtype.names:
                values = np.asarray(arr["returns"], dtype=np.float64).reshape(-1)
            elif "price" in arr.dtype.names:
                px = np.asarray(arr["price"], dtype=np.float64).reshape(-1)
                values = np.zeros(px.size, dtype=np.float64)
                if px.size > 1:
                    base = px[:-1]
                    diff = np.diff(px)
                    values[:-1] = np.divide(diff, base, out=np.zeros_like(diff), where=base != 0)
            else:
                values = np.asarray(arr, dtype=np.float64).reshape(-1)
        else:
            values = np.asarray(arr, dtype=np.float64).reshape(-1)
        out = np.zeros(values.size, dtype=np.float64)
        for i, value in enumerate(values):
            out[i] = self.update(float(value))
        return out


ALPHA_CLASS = KLRegimeAlpha

__all__ = ["RegimeResult", "KLRegimeDetector", "KLRegimeAlpha", "ALPHA_CLASS"]


def _chi2_sf(value: float, df: int) -> float:
    if value <= 0 or df <= 0:
        return 1.0
    if scipy_stats is not None:
        return float(scipy_stats.chi2.sf(value, df=df))
    # Wilson-Hilferty transform normal approximation fallback.
    z = ((value / df) ** (1.0 / 3.0) - (1.0 - (2.0 / (9.0 * df)))) / np.sqrt(2.0 / (9.0 * df))
    return float(0.5 * np.erfc(z / np.sqrt(2.0)))
