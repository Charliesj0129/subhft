"""Data quality profiler — per-symbol anomaly detection.

Detects: price outliers (>3σ), volume spikes (>5x median), gaps (>5s).
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any

import numpy as np


@dataclass(frozen=True)
class SymbolProfile:
    """Quality profile for a single symbol on a single date."""

    symbol: str
    date: str
    tick_count: int
    min_price_scaled: int
    max_price_scaled: int
    median_volume: float
    mean_spread_scaled: float
    gap_count: int
    max_gap_seconds: float
    anomaly_flags: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "symbol": self.symbol,
            "date": self.date,
            "tick_count": self.tick_count,
            "min_price_scaled": self.min_price_scaled,
            "max_price_scaled": self.max_price_scaled,
            "median_volume": self.median_volume,
            "mean_spread_scaled": self.mean_spread_scaled,
            "gap_count": self.gap_count,
            "max_gap_seconds": self.max_gap_seconds,
            "anomaly_flags": list(self.anomaly_flags),
        }


class DataProfiler:
    """Profile market data records for a symbol.

    Each record is expected to have at minimum:
        price_scaled (int), volume (int/float), timestamp_ns (int).
    Optionally: spread_scaled (int).
    """

    def __init__(
        self,
        price_sigma_threshold: float = 3.0,
        volume_spike_multiplier: float = 5.0,
        gap_threshold_seconds: float = 5.0,
    ) -> None:
        self.price_sigma_threshold = price_sigma_threshold
        self.volume_spike_multiplier = volume_spike_multiplier
        self.gap_threshold_seconds = gap_threshold_seconds

    def profile_symbol(
        self,
        symbol: str,
        date: str,
        records: Sequence[dict[str, Any]],
    ) -> SymbolProfile:
        """Profile a symbol's data for a single date.

        Args:
            symbol: Symbol identifier.
            date: Date string (YYYY-MM-DD).
            records: List of dicts with keys: price_scaled, volume, timestamp_ns.
                     Optional: spread_scaled.

        Returns:
            SymbolProfile with anomaly flags.
        """
        if not records:
            return SymbolProfile(
                symbol=symbol,
                date=date,
                tick_count=0,
                min_price_scaled=0,
                max_price_scaled=0,
                median_volume=0.0,
                mean_spread_scaled=0.0,
                gap_count=0,
                max_gap_seconds=0.0,
                anomaly_flags=["no_data"],
            )

        prices = np.array([r["price_scaled"] for r in records], dtype=np.int64)
        volumes = np.array([r["volume"] for r in records], dtype=np.float64)
        timestamps = np.array([r["timestamp_ns"] for r in records], dtype=np.int64)
        spreads = np.array(
            [r.get("spread_scaled", 0) for r in records],
            dtype=np.float64,
        )

        anomaly_flags: list[str] = []

        # Price outlier detection (3σ)
        if len(prices) > 1:
            price_mean = float(np.mean(prices))
            price_std = float(np.std(prices))
            if price_std > 0:
                deviations = np.abs(prices - price_mean) / price_std
                n_outliers = int(np.sum(deviations > self.price_sigma_threshold))
                if n_outliers > 0:
                    anomaly_flags.append(f"price_outlier_count={n_outliers}")

        # Volume spike detection (>5x median)
        median_vol = float(np.median(volumes)) if len(volumes) > 0 else 0.0
        if median_vol > 0:
            n_spikes = int(np.sum(volumes > median_vol * self.volume_spike_multiplier))
            if n_spikes > 0:
                anomaly_flags.append(f"volume_spike_count={n_spikes}")

        # Gap detection (>5s)
        gap_count = 0
        max_gap_s = 0.0
        if len(timestamps) > 1:
            sorted_ts = np.sort(timestamps)
            diffs_ns = np.diff(sorted_ts)
            diffs_s = diffs_ns / 1e9
            gap_mask = diffs_s > self.gap_threshold_seconds
            gap_count = int(np.sum(gap_mask))
            max_gap_s = float(np.max(diffs_s)) if len(diffs_s) > 0 else 0.0
            if gap_count > 0:
                anomaly_flags.append(f"gap_count={gap_count}")

        return SymbolProfile(
            symbol=symbol,
            date=date,
            tick_count=len(records),
            min_price_scaled=int(np.min(prices)),
            max_price_scaled=int(np.max(prices)),
            median_volume=round(median_vol, 2),
            mean_spread_scaled=round(float(np.mean(spreads)), 2),
            gap_count=gap_count,
            max_gap_seconds=round(max_gap_s, 3),
            anomaly_flags=anomaly_flags,
        )
