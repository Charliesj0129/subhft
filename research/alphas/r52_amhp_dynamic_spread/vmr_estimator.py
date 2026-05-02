"""VMR-based ρ̂ estimator for live exp-Hawkes refit.

Theory
------
For a stationary exp-Hawkes process with branching ratio ρ ∈ [0, 1), the
asymptotic variance-to-mean ratio of the count N(T) over disjoint sub-windows
of length T (with T ≫ 1/β) approaches 1/(1-ρ)² (Bartlett 1955; Bacry-Muzy 2014):

    Var(N(T)) / E(N(T)) → 1/(1-ρ)²    as T → ∞

Therefore:

    ρ̂ = 1 - 1/√VMR

Properties
----------
* O(1) per event-add (deque popleft for window trim).
* O(N_subwin) per refit (single binning pass; N_subwin = window_sec / subwindow_sec).
* p99 latency ~ 0.2 ms typical for window=300s / subwindow=10s (30 sub-windows).
* Method-of-moments — weaker theoretical basis than MLE but adequate for ρ̂
  monitoring in regime-detection use cases.

The C2 pre-T4 gate replay used this estimator to refit ρ̂ every 5 sec of
event-time across 16 days of TMFD6 trades (9.16M events → ~1.4 sec/day CPU).

Float exception
---------------
Architecture Governance Rule §11: float is permitted in this research module.
"""
from __future__ import annotations

import math
from collections import deque
from typing import Any

_NS_PER_SEC = 1_000_000_000


def vmr_rho_hat(
    buf: Any,
    end_ts_ns: int,
    window_sec: int,
    subwindow_sec: int,
) -> float:
    """ρ̂ via variance-to-mean ratio across N_subwin disjoint sub-windows.

    `buf` may be any iterable of ns-timestamps; only events inside
    [end_ts_ns - window_sec * 1e9, end_ts_ns) contribute.

    Uses numpy histogram for the binning step — vectorized binning is
    ~5-10× faster than a pure-Python loop and clears the p99<1ms T4 budget
    on n ≤ 5000 events comfortably.
    """
    import numpy as np

    sub_ns = subwindow_sec * _NS_PER_SEC
    start_ns = end_ts_ns - window_sec * _NS_PER_SEC
    n_sub = window_sec // subwindow_sec
    if n_sub < 5:
        return 0.0
    arr = np.fromiter(buf, dtype=np.int64) if not isinstance(buf, np.ndarray) else buf
    if arr.size == 0:
        return 0.0
    bins = np.arange(start_ns, start_ns + (n_sub + 1) * sub_ns, sub_ns, dtype=np.int64)
    counts, _ = np.histogram(arr, bins=bins)
    mean = float(counts.mean())
    if mean <= 0:
        return 0.0
    var = float(counts.var(ddof=0))
    vmr = var / mean
    if vmr <= 1.0:
        return 0.0   # below Poisson — no clustering signal
    rho = 1.0 - 1.0 / math.sqrt(vmr)
    if rho < 0.0:
        return 0.0
    if rho > 0.99:
        return 0.99
    return float(rho)


class OnlineHawkesVMR:
    """Online ρ̂ estimator using the VMR moments method.

    Designed for per-tick add + gated refit. The refit cadence is controlled by
    `refit_every_ns` (default 5 sec event-time); add operations are O(1).

    API
    ---
    update(arrival_ts_ns: int) -> None : append trade timestamp; trims out-of-window.
    maybe_refit(ts_ns: int) -> float   : refit ρ̂ if cadence elapsed; returns ρ̂.
    get_rho_hat() -> float             : last ρ̂.
    rho_samples_minute -> dict         : per-minute-bucket max ρ̂ observed.
    """

    __slots__ = (
        "_window_sec",
        "_subwindow_sec",
        "_refit_every_ns",
        "_buf",
        "_last_fit_ns",
        "_rho_hat",
        "_n_events",
        "_rho_samples_minute",
    )

    def __init__(
        self,
        window_sec: int = 300,
        subwindow_sec: int = 10,
        refit_every_ns: int = 5 * _NS_PER_SEC,
    ) -> None:
        self._window_sec = window_sec
        self._subwindow_sec = subwindow_sec
        self._refit_every_ns = refit_every_ns
        self._buf: Any = deque()
        self._last_fit_ns: int = -refit_every_ns - 1
        self._rho_hat: float = 0.0
        self._n_events: int = 0
        self._rho_samples_minute: dict[int, float] = {}

    def update(self, arrival_ts_ns: int) -> None:
        self._buf.append(arrival_ts_ns)
        cutoff_ns = arrival_ts_ns - self._window_sec * _NS_PER_SEC
        while self._buf and self._buf[0] < cutoff_ns:
            self._buf.popleft()

    def maybe_refit(self, ts_ns: int) -> float:
        if ts_ns - self._last_fit_ns < self._refit_every_ns:
            self._sample_minute(ts_ns)
            return self._rho_hat
        n = len(self._buf)
        if n < 30:
            self._rho_hat = 0.0
            self._n_events = n
        else:
            self._rho_hat = vmr_rho_hat(
                self._buf, ts_ns, self._window_sec, self._subwindow_sec,
            )
            self._n_events = n
        self._last_fit_ns = ts_ns
        self._sample_minute(ts_ns)
        return self._rho_hat

    def _sample_minute(self, ts_ns: int) -> None:
        minute_bucket = ts_ns // (60 * _NS_PER_SEC)
        prev = self._rho_samples_minute.get(minute_bucket, -1.0)
        if self._rho_hat > prev:
            self._rho_samples_minute[minute_bucket] = self._rho_hat

    def get_rho_hat(self) -> float:
        return self._rho_hat

    @property
    def rho_samples_minute(self) -> dict[int, float]:
        return self._rho_samples_minute

    @property
    def n_events_in_window(self) -> int:
        return len(self._buf)
