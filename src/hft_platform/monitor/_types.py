"""Type definitions for Signal Monitor TUI."""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from enum import IntEnum, IntFlag, auto
from typing import Any


class MonitorState(IntEnum):
    """Monitor state machine states."""

    INITIALIZING = auto()
    WARMING_UP = auto()
    LIVE = auto()
    STALE = auto()
    PAUSED = auto()
    DISCONNECTED = auto()
    ERROR = auto()


class EventFlag(IntFlag):
    """Bitmask for per-symbol events detected each poll cycle."""

    NONE = 0
    COMPOSITE_CROSS = auto()  # signal flipped sign
    SIGMA_BREAK_UP = auto()  # crossed 1σ or 2σ upward
    SIGMA_BREAK_DOWN = auto()  # fell below 1σ or 2σ
    AGREE_FLIP = auto()  # dominant direction changed
    SPREAD_CONVERGE = auto()  # spread dropped >30%
    SPREAD_WIDEN = auto()  # spread increased >50%
    STALE_ENTER = auto()
    STALE_RESOLVE = auto()


_EVENT_RING_SIZE = 32


@dataclass(slots=True)
class MonitorEvent:
    """A single event entry for the header ticker."""

    symbol: str
    label: str
    fired_ns: int


@dataclass(slots=True, frozen=True)
class WatchlistSymbol:
    """A symbol entry from watchlist.yaml."""

    code: str
    name: str
    product_type: str  # "stock" | "future" | "option"
    alpha_ids: tuple[str, ...] = ()


@dataclass(slots=True, frozen=True)
class MonitorConfig:
    """Parsed monitor configuration."""

    symbols: tuple[WatchlistSymbol, ...]
    source: str = "clickhouse"
    poll_interval_s: float = 2.0
    warmup_ticks: int = 64
    stale_threshold_s: float = 6.0
    no_data_warn_s: float = 10.0
    max_retries: int = 20
    batch_limit_per_symbol: int = 200
    replay_ticks: int = 64
    ch_host: str = "localhost"
    ch_port: int = 8123
    ch_user: str = "default"
    ch_password: str = ""
    redis_host: str = "localhost"
    redis_port: int = 6379
    redis_password: str = ""
    redis_key_prefix: str = "monitor:l1"
    redis_ring_size: int = 256
    promotions_dir: str = "config/strategy_promotions"
    data_source: str = "auto"  # "ch" | "shm" | "auto" (hybrid)
    hybrid_backfill_interval_s: float = 30.0
    # S5: Watchlist auto-derive
    symbol_source: str = ""  # path to symbols.yaml for auto-derive
    auto_filter_skip_expired: bool = True
    pin_symbols: tuple[str, ...] = ()
    default_alpha_ids: tuple[str, ...] = ()


@dataclass(slots=True)
class RowView:
    """Cache-friendly replacement for per-row dict. One instance per CH result row."""

    symbol: str
    ingest_ts: int
    bids_price: Any  # list[int] from CH
    asks_price: Any  # list[int] from CH
    bids_vol: Any  # list[int] from CH
    asks_vol: Any  # list[int] from CH
    price_scaled: int
    volume: int


@dataclass(slots=True)
class AlphaState:
    """Per-alpha runtime state."""

    alpha_id: str
    runtime: Any | None = None
    signal: float = math.nan
    error_count: int = 0
    disabled: bool = False
    z_score: float = 0.0
    # EMA-64 for rolling sigma
    _ema_signal: float = 0.0
    _ema_abs_dev: float = 1e-8
    # Pre-probed dispatch keys (None = full payload, tuple = filtered keys)
    _dispatch_keys: tuple[str, ...] | None = None
    # Pre-allocated filtered payload buffer (avoids per-tick dict allocation)
    _filtered_buf: dict[str, Any] = field(default_factory=dict)
    # Per-alpha sparkline ring buffer (Phase 3)
    _signal_spark_buf: list[float] = field(default_factory=lambda: [0.0] * _SPARKLINE_SIZE)
    _signal_spark_idx: int = 0
    _signal_spark_len: int = 0

    def update_z(self, signal: float, alpha: float = 2.0 / 65.0) -> None:
        """Update EMA-based z-score normalization."""
        self._ema_signal += alpha * (signal - self._ema_signal)
        dev = abs(signal - self._ema_signal)
        self._ema_abs_dev += alpha * (dev - self._ema_abs_dev)
        sigma = max(self._ema_abs_dev, 1e-8)
        self.z_score = (signal - self._ema_signal) / sigma

    def signal_sparkline_append(self, value: float) -> None:
        """Append to per-alpha sparkline ring buffer."""
        self._signal_spark_buf[self._signal_spark_idx] = value
        self._signal_spark_idx = (self._signal_spark_idx + 1) % _SPARKLINE_SIZE
        if self._signal_spark_len < _SPARKLINE_SIZE:
            self._signal_spark_len += 1

    def signal_sparkline_values(self) -> list[float]:
        """Return per-alpha sparkline in order (oldest→newest)."""
        n = self._signal_spark_len
        if n == 0:
            return []
        if n < _SPARKLINE_SIZE:
            return self._signal_spark_buf[:n]
        start = self._signal_spark_idx
        return self._signal_spark_buf[start:] + self._signal_spark_buf[:start]

    def signal_sparkline_clear(self) -> None:
        self._signal_spark_idx = 0
        self._signal_spark_len = 0


_SPARKLINE_SIZE = 20


@dataclass(slots=True)
class SymbolState:
    """Per-symbol runtime state."""

    symbol: WatchlistSymbol
    cursor_ts_ns: int = 0
    tick_count: int = 0
    last_update_ns: int = 0
    last_seen_ts_ns: int = 0
    session_started_ns: int = 0
    session_active: bool = False
    was_session_active: bool = False
    session_display: str = "Closed"

    # Latest enriched values
    last_price: float = 0.0
    spread_bps: float = 0.0
    bid_qty: float = 0.0
    ask_qty: float = 0.0
    prev_bid_qty: float = 0.0
    prev_ask_qty: float = 0.0
    ofi_l1_cum: float = 0.0

    # Alpha signals
    alpha_states: dict[str, AlphaState] = field(default_factory=dict)

    # Composite
    composite: float = 0.0
    composite_sigma: float = 0.0

    # Delta tracking — snapshot before each poll cycle (Phase 1)
    prev_composite: float = 0.0
    prev_agree_direction: int = 0  # -1, 0, +1
    prev_spread_bps: float = 0.0
    prev_is_stale: bool = False
    composite_delta: float = 0.0
    composite_delta_abs: float = 0.0

    # Event flags — reset each cycle (Phase 1)
    event_flags: int = 0  # bitmask of EventFlag
    last_event_ns: int = 0

    # Opportunity score — updated each cycle (Phase 1/2)
    opportunity_score: float = 0.0

    # Sparkline ring buffer (fixed-size list)
    _spark_buf: list[float] = field(default_factory=lambda: [0.0] * _SPARKLINE_SIZE)
    _spark_idx: int = 0
    _spark_len: int = 0
    _spark_cache: list[float] = field(default_factory=list)
    _spark_dirty: bool = True

    # Pre-allocated payload buffer for enrich_tick (12 keys)
    _payload_buf: dict[str, Any] = field(
        default_factory=lambda: {
            "bid_px": 0.0,
            "ask_px": 0.0,
            "bid_qty": 0.0,
            "ask_qty": 0.0,
            "mid_price": 0.0,
            "microprice_x2": 0,
            "spread_scaled": 0,
            "spread_bps": 0.0,
            "imbalance": 0.0,
            "ofi_l1_raw": 0.0,
            "ofi_l1_cum": 0.0,
            "local_ts": 0,
        }
    )

    # S2: previous poll price for delta flash
    prev_poll_price: float = 0.0

    # S7: price sparkline ring buffer (mirrors composite sparkline pattern)
    _price_spark_buf: list[float] = field(default_factory=lambda: [0.0] * _SPARKLINE_SIZE)
    _price_spark_idx: int = 0
    _price_spark_len: int = 0

    # Session status
    is_stale: bool = False
    is_closed: bool = False
    session_label: str = ""  # "", "[PRE]", "[CLOSED]"
    invalid_row_count: int = 0
    last_invalid_reason: str = ""

    def sparkline_append(self, value: float) -> None:
        """Append a value to the sparkline ring buffer."""
        self._spark_buf[self._spark_idx] = value
        self._spark_idx = (self._spark_idx + 1) % _SPARKLINE_SIZE
        if self._spark_len < _SPARKLINE_SIZE:
            self._spark_len += 1
        self._spark_dirty = True

    def sparkline_values(self) -> list[float]:
        """Return sparkline values in order (oldest to newest), cached until next append."""
        if not self._spark_dirty:
            return self._spark_cache
        if self._spark_len == 0:
            self._spark_cache = []
        elif self._spark_len < _SPARKLINE_SIZE:
            self._spark_cache = self._spark_buf[: self._spark_len]
        else:
            # Full buffer: read from _spark_idx (oldest) wrapping around
            start = self._spark_idx
            self._spark_cache = self._spark_buf[start:] + self._spark_buf[:start]
        self._spark_dirty = False
        return self._spark_cache

    def sparkline_clear(self) -> None:
        """Reset sparkline ring buffer."""
        self._spark_idx = 0
        self._spark_len = 0
        self._spark_dirty = True

    # S7: Price sparkline methods
    def price_sparkline_append(self, value: float) -> None:
        """Append a value to the price sparkline ring buffer."""
        self._price_spark_buf[self._price_spark_idx] = value
        self._price_spark_idx = (self._price_spark_idx + 1) % _SPARKLINE_SIZE
        if self._price_spark_len < _SPARKLINE_SIZE:
            self._price_spark_len += 1

    def price_sparkline_values(self) -> list[float]:
        """Return price sparkline values in order (oldest to newest)."""
        n = self._price_spark_len
        if n == 0:
            return []
        if n < _SPARKLINE_SIZE:
            return self._price_spark_buf[:n]
        start = self._price_spark_idx
        return self._price_spark_buf[start:] + self._price_spark_buf[:start]

    def price_sparkline_clear(self) -> None:
        """Reset price sparkline ring buffer."""
        self._price_spark_idx = 0
        self._price_spark_len = 0


@dataclass(slots=True, frozen=True)
class HeaderContext:
    """Data-only header state, computed by engine, rendered by renderer."""

    state: MonitorState
    session_display: str
    time_str: str
    ch_status: str
    stale_symbols: list[str]
    extra: str = ""
    sort_mode: str = "opportunity"
    event_ticker: str = ""
    source_label: str = ""
    # S1: heartbeat
    poll_count: int = 0
    poll_age_s: float = 0.0
    # S3: collapsed closed symbols
    closed_collapsed: bool = True
    n_closed: int = 0


# CH → platform price scale constants
CH_PRICE_SCALE = 1_000_000
PLATFORM_SCALE = 10_000
CH_TO_PLATFORM_DIVISOR = CH_PRICE_SCALE // PLATFORM_SCALE  # 100
