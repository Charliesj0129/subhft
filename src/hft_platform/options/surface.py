"""Volatility surface: grid-based storage with cubic spline interpolation.

Offline analytics module — float arithmetic is permitted (not a live trading path).
"""
from __future__ import annotations

import math
from datetime import date

import numpy as np
from scipy.interpolate import CubicSpline

_NAN = float("nan")
_IV_MIN = 0.01
_IV_MAX = 2.0


class VolSurface:
    """Grid-based implied volatility surface.

    Keys are ``(expiry_date, strike)`` tuples; IVs stored as plain floats.
    Interpolation uses CubicSpline when >= 4 strikes are present, otherwise
    linear interpolation. No extrapolation beyond the observed strike range.
    """

    __slots__ = ("_grid",)

    def __init__(self) -> None:
        self._grid: dict[tuple[date, float], float] = {}

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def update(self, strike: float, expiry_date: date, iv: float) -> None:
        """Store *iv* for *(expiry_date, strike)*.

        If *iv* is outside ``[0.01, 2.0]`` the entry is treated as stale and
        removed (if present).
        """
        key = (expiry_date, strike)
        if _IV_MIN <= iv <= _IV_MAX:
            self._grid[key] = iv
        else:
            self._grid.pop(key, None)

    def get_iv(self, strike: float, expiry_date: date) -> float:
        """Return interpolated IV for *(strike, expiry_date)*.

        Returns NaN when:
        - no data exists for the expiry
        - fewer than 2 strikes are available (cannot interpolate)
        - the requested strike is outside the observed range (no extrapolation)
        """
        key = (expiry_date, strike)
        if key in self._grid:
            return self._grid[key]

        strikes, ivs = self._sorted_for_expiry(expiry_date)
        n = len(strikes)
        if n < 2:
            return _NAN

        lo, hi = strikes[0], strikes[-1]
        if strike < lo or strike > hi:
            return _NAN

        if n >= 4:
            cs = CubicSpline(strikes, ivs)
            return float(cs(strike))
        else:
            return float(np.interp(strike, strikes, ivs))

    def snapshot(self) -> dict[tuple[date, float], float]:
        """Return a shallow copy of the current grid."""
        return dict(self._grid)

    def skew_25d(self, expiry_date: date) -> float:
        """25D risk-reversal approximation: IV(25th pctl strike) - IV(75th pctl strike).

        Returns NaN if fewer than 3 strikes are available.
        """
        _, ivs = self._sorted_for_expiry(expiry_date)
        if len(ivs) < 3:
            return _NAN
        arr = np.array(ivs, dtype=float)
        iv_25 = float(np.percentile(arr, 25))
        iv_75 = float(np.percentile(arr, 75))
        return iv_25 - iv_75

    def butterfly_25d(self, expiry_date: date) -> float:
        """25D butterfly approximation: 0.5*(IV_25 + IV_75) - IV_50.

        Returns NaN if fewer than 3 strikes are available.
        """
        _, ivs = self._sorted_for_expiry(expiry_date)
        if len(ivs) < 3:
            return _NAN
        arr = np.array(ivs, dtype=float)
        iv_25 = float(np.percentile(arr, 25))
        iv_50 = float(np.percentile(arr, 50))
        iv_75 = float(np.percentile(arr, 75))
        return 0.5 * (iv_25 + iv_75) - iv_50

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _sorted_for_expiry(
        self, expiry_date: date
    ) -> tuple[list[float], list[float]]:
        """Return *(strikes, ivs)* sorted ascending by strike for *expiry_date*."""
        pairs = [
            (k[1], v) for k, v in self._grid.items() if k[0] == expiry_date
        ]
        pairs.sort(key=lambda x: x[0])
        if not pairs:
            return [], []
        strikes, ivs = zip(*pairs)
        return list(strikes), list(ivs)
