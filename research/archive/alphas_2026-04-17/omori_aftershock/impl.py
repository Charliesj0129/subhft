"""Omori Aftershock Trader — R30 Alpha Prototype.

Detects large price moves (mainshocks) and trades the post-event
volatility relaxation using Omori power-law decay framework.

References:
    - Petersen et al. (2010) arXiv:1006.1882
    - Lillo & Mantegna (2002) cond-mat/0111257
    - Rai et al. (2020) arXiv:2012.03012
"""

from __future__ import annotations

import dataclasses
from typing import Literal

import numpy as np


@dataclasses.dataclass(frozen=True, slots=True)
class MainshockEvent:
    """Detected mainshock event."""

    timestamp_ns: int
    price_at_detection: int  # scaled int (x10000 or platform convention)
    change_pts: float  # signed price change in index points
    direction: Literal["UP", "DOWN"]
    window_minutes: int


@dataclasses.dataclass(frozen=True, slots=True)
class OmoriParams:
    """Fitted Omori decay parameters: n(t) = K * (t + c)^{-p}."""

    K: float  # amplitude
    c: float  # onset offset (avoids singularity at t=0)
    p: float  # decay exponent (p~1 for exogenous, p~0.5 for endogenous)
    n_aftershocks: int  # total aftershocks observed during fit


@dataclasses.dataclass(frozen=True, slots=True)
class TradeSignal:
    """Signal emitted by the strategy."""

    timestamp_ns: int
    direction: Literal["BUY", "SELL"]
    entry_price_pts: float
    stop_loss_pts: float
    take_profit_pts: float
    max_hold_minutes: int
    event_id: str
    confidence: float  # 0-1


class MainshockDetector:
    """Rolling window mainshock detector.

    Monitors price changes over a configurable rolling window and
    fires when the absolute change exceeds a threshold.
    """

    __slots__ = (
        "_threshold_pts",
        "_window_minutes",
        "_min_gap_minutes",
        "_price_buffer",
        "_ts_buffer",
        "_last_event_ts",
        "_scale",
    )

    def __init__(
        self,
        threshold_pts: float = 150.0,
        window_minutes: int = 30,
        min_gap_minutes: int = 30,
        price_scale: int = 1_000_000,
    ) -> None:
        self._threshold_pts = threshold_pts
        self._window_minutes = window_minutes
        self._min_gap_minutes = min_gap_minutes
        self._price_buffer: list[int] = []
        self._ts_buffer: list[int] = []
        self._last_event_ts: int = 0
        self._scale = price_scale

    def update(self, timestamp_ns: int, price_scaled: int) -> MainshockEvent | None:
        """Feed a new price observation. Returns MainshockEvent if detected."""
        self._price_buffer.append(price_scaled)
        self._ts_buffer.append(timestamp_ns)

        # Trim buffer to window
        window_ns = self._window_minutes * 60 * 1_000_000_000
        while self._ts_buffer and (timestamp_ns - self._ts_buffer[0]) > window_ns:
            self._ts_buffer.pop(0)
            self._price_buffer.pop(0)

        if len(self._price_buffer) < 2:
            return None

        # Check gap constraint
        gap_ns = self._min_gap_minutes * 60 * 1_000_000_000
        if timestamp_ns - self._last_event_ts < gap_ns:
            return None

        # Check price change from oldest in buffer to current
        change_raw = price_scaled - self._price_buffer[0]
        change_pts = change_raw / self._scale

        if abs(change_pts) >= self._threshold_pts:
            self._last_event_ts = timestamp_ns
            return MainshockEvent(
                timestamp_ns=timestamp_ns,
                price_at_detection=price_scaled,
                change_pts=change_pts,
                direction="UP" if change_pts > 0 else "DOWN",
                window_minutes=self._window_minutes,
            )
        return None

    def reset(self) -> None:
        self._price_buffer.clear()
        self._ts_buffer.clear()
        self._last_event_ts = 0


class OmoriDecayTracker:
    """Track and fit Omori power-law decay after a mainshock.

    Counts aftershock events (|1-min return| > threshold) in time bins
    after the mainshock and fits n(t) = K * (t + c)^{-p}.
    """

    __slots__ = (
        "_aftershock_threshold_pts",
        "_max_tracking_ns",
        "_event_ts",
        "_event_price",
        "_aftershock_times",
        "_scale",
    )

    def __init__(
        self,
        aftershock_threshold_pts: float = 10.0,
        max_tracking_minutes: int = 120,
        price_scale: int = 1_000_000,
    ) -> None:
        self._aftershock_threshold_pts = aftershock_threshold_pts
        self._max_tracking_ns = max_tracking_minutes * 60 * 1_000_000_000
        self._event_ts: int = 0
        self._event_price: int = 0
        self._aftershock_times: list[float] = []  # seconds since mainshock
        self._scale = price_scale

    def start(self, mainshock: MainshockEvent) -> None:
        """Start tracking aftershocks from a mainshock."""
        self._event_ts = mainshock.timestamp_ns
        self._event_price = mainshock.price_at_detection
        self._aftershock_times = []

    def update(
        self, timestamp_ns: int, price_scaled: int, prev_price_scaled: int
    ) -> bool:
        """Feed a price update. Returns True if an aftershock was detected."""
        if self._event_ts == 0:
            return False

        elapsed_ns = timestamp_ns - self._event_ts
        if elapsed_ns > self._max_tracking_ns or elapsed_ns <= 0:
            return False

        change_pts = abs(price_scaled - prev_price_scaled) / self._scale
        if change_pts >= self._aftershock_threshold_pts:
            elapsed_sec = elapsed_ns / 1_000_000_000
            self._aftershock_times.append(elapsed_sec)
            return True
        return False

    @property
    def is_active(self) -> bool:
        return self._event_ts > 0

    @property
    def aftershock_count(self) -> int:
        return len(self._aftershock_times)

    def fit_omori(self) -> OmoriParams | None:
        """Fit Omori decay parameters from observed aftershock times.

        Uses simple binned least-squares on log-log scale:
        log(n_bin / dt) = log(K) - p * log(t_mid + c)
        """
        if len(self._aftershock_times) < 5:
            return None

        times = np.array(sorted(self._aftershock_times))

        # Bin into log-spaced bins
        t_max = times[-1]
        if t_max <= 1.0:
            return None

        bin_edges = np.logspace(0, np.log10(t_max), num=10)
        counts, _ = np.histogram(times, bins=bin_edges)

        # Compute rate = count / bin_width at bin midpoint
        valid = counts > 0
        if valid.sum() < 3:
            return None

        bin_mids = 0.5 * (bin_edges[:-1] + bin_edges[1:])
        bin_widths = np.diff(bin_edges)
        rates = counts[valid] / bin_widths[valid]
        mids = bin_mids[valid]

        # Fit log(rate) = log(K) - p * log(t + c)
        # Start with c = 1.0 (standard Omori)
        c = 1.0
        log_t = np.log(mids + c)
        log_r = np.log(rates)

        # Linear regression: log_r = a + b * log_t
        coeffs = np.polyfit(log_t, log_r, 1)
        p = -coeffs[0]
        K = np.exp(coeffs[1])

        return OmoriParams(K=K, c=c, p=p, n_aftershocks=len(self._aftershock_times))

    def reset(self) -> None:
        self._event_ts = 0
        self._event_price = 0
        self._aftershock_times = []


class AftershockStrategy:
    """Entry/exit logic based on mainshock detection and Omori decay.

    Design constraints (from Execution review):
    - Spread-gating: NO entry if spread > max_spread_pts
    - Session filter: only during allowed hours
    - StormGuard respect
    - Daily loss limit + max events/day
    """

    __slots__ = (
        "_detector",
        "_tracker",
        "_max_spread_pts",
        "_entry_delay_ns",
        "_entry_mode",
        "_stop_loss_pts",
        "_take_profit_pts",
        "_max_hold_ns",
        "_max_events_per_day",
        "_daily_loss_limit_pts",
        "_min_trade_gap_ns",
        "_allowed_hours",
        "_scale",
        "_pending_event",
        "_pending_entry_after_ns",
        "_daily_events",
        "_daily_pnl_pts",
        "_current_day",
        "_last_trade_ts",
        "_event_counter",
    )

    def __init__(
        self,
        detector: MainshockDetector,
        tracker: OmoriDecayTracker,
        max_spread_pts: float = 5.0,
        entry_delay_seconds: int = 60,
        entry_mode: str = "continuation",
        stop_loss_pts: float = 30.0,
        take_profit_pts: float = 80.0,
        max_hold_minutes: int = 30,
        max_events_per_day: int = 5,
        daily_loss_limit_pts: float = 200.0,
        min_trade_gap_minutes: int = 15,
        allowed_hours: list[tuple[int, int, int, int]] | None = None,
        price_scale: int = 1_000_000,
    ) -> None:
        self._detector = detector
        self._tracker = tracker
        self._max_spread_pts = max_spread_pts
        self._entry_delay_ns = entry_delay_seconds * 1_000_000_000
        self._entry_mode = entry_mode
        self._stop_loss_pts = stop_loss_pts
        self._take_profit_pts = take_profit_pts
        self._max_hold_ns = max_hold_minutes * 60 * 1_000_000_000
        self._max_events_per_day = max_events_per_day
        self._daily_loss_limit_pts = daily_loss_limit_pts
        self._min_trade_gap_ns = min_trade_gap_minutes * 60 * 1_000_000_000
        self._allowed_hours = allowed_hours or [(8, 45, 13, 30)]
        self._scale = price_scale
        self._pending_event: MainshockEvent | None = None
        self._pending_entry_after_ns: int = 0
        self._daily_events: int = 0
        self._daily_pnl_pts: float = 0.0
        self._current_day: str = ""
        self._last_trade_ts: int = 0
        self._event_counter: int = 0

    def _is_within_session(self, hour: int, minute: int) -> bool:
        for start_h, start_m, end_h, end_m in self._allowed_hours:
            start_total = start_h * 60 + start_m
            end_total = end_h * 60 + end_m
            current_total = hour * 60 + minute
            if start_total <= current_total <= end_total:
                return True
        return False

    def on_tick(
        self,
        timestamp_ns: int,
        price_scaled: int,
        spread_pts: float,
        hour_local: int,
        minute_local: int,
        day_str: str,
        storm_guard_halt: bool = False,
    ) -> TradeSignal | None:
        """Process a tick. Returns TradeSignal if entry conditions met."""
        # Reset daily counters on new day
        if day_str != self._current_day:
            self._current_day = day_str
            self._daily_events = 0
            self._daily_pnl_pts = 0.0

        # Feed detector
        event = self._detector.update(timestamp_ns, price_scaled)
        if event is not None:
            self._pending_event = event
            self._pending_entry_after_ns = timestamp_ns + self._entry_delay_ns
            self._tracker.start(event)

        # Check if we have a pending event ready for entry
        if self._pending_event is None:
            return None

        if timestamp_ns < self._pending_entry_after_ns:
            return None  # Still in delay period

        # === Gate checks ===

        if storm_guard_halt:
            self._pending_event = None
            return None

        if not self._is_within_session(hour_local, minute_local):
            self._pending_event = None
            return None

        if spread_pts > self._max_spread_pts:
            return None  # Wait for spread to narrow (don't cancel event)

        if self._daily_events >= self._max_events_per_day:
            self._pending_event = None
            return None

        if self._daily_pnl_pts <= -self._daily_loss_limit_pts:
            self._pending_event = None
            return None

        if timestamp_ns - self._last_trade_ts < self._min_trade_gap_ns:
            self._pending_event = None
            return None

        # === Entry ===

        evt = self._pending_event
        self._pending_event = None
        self._daily_events += 1
        self._last_trade_ts = timestamp_ns
        self._event_counter += 1

        price_pts = price_scaled / self._scale

        if self._entry_mode == "continuation":
            direction = "BUY" if evt.direction == "UP" else "SELL"
        else:
            direction = "SELL" if evt.direction == "UP" else "BUY"

        if direction == "BUY":
            sl = price_pts - self._stop_loss_pts
            tp = price_pts + self._take_profit_pts
        else:
            sl = price_pts + self._stop_loss_pts
            tp = price_pts - self._take_profit_pts

        return TradeSignal(
            timestamp_ns=timestamp_ns,
            direction=direction,
            entry_price_pts=price_pts,
            stop_loss_pts=sl,
            take_profit_pts=tp,
            max_hold_minutes=int(self._max_hold_ns / 60_000_000_000),
            event_id=f"omori_{self._event_counter:04d}",
            confidence=min(1.0, abs(evt.change_pts) / 300.0),
        )

    def record_trade_result(self, pnl_pts: float) -> None:
        """Record PnL from a completed trade for daily limit tracking."""
        self._daily_pnl_pts += pnl_pts

    def reset(self) -> None:
        self._detector.reset()
        self._tracker.reset()
        self._pending_event = None
        self._daily_events = 0
        self._daily_pnl_pts = 0.0
        self._current_day = ""
        self._last_trade_ts = 0
