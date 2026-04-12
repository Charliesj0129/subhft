# core — Platform Foundation Utilities

> **Package**: `src/hft_platform/core/`
> **Runtime Plane**: Infrastructure (cross-cutting)
> **Hot-Path**: `timebase.now_ns()`, `pricing.PriceCodec.scale()`

## Overview

Core utilities shared across all platform modules: timestamps, price scaling, instrument metadata, rate limiting, market calendar, and secret validation.

## Files

| File | Key Exports | Purpose |
|------|-------------|---------|
| `timebase.py` | `now_ns()`, `now_s()`, `monotonic_ns()`, `perf_ns()`, `coerce_ns()` | Centralized timestamp utilities (mandatory: never use `datetime.now()`) |
| `pricing.py` | `PriceCodec`, `FixedPriceScaleProvider`, `SymbolMetadataPriceScaleProvider` | Scaled-integer price conversion (x10000) |
| `instrument_registry.py` | `InstrumentRegistry`, `InstrumentProfile`, `FeeStructure`, `TradingHours` | Immutable instrument metadata registry (max 5000 entries) |
| `rate_limiter.py` | `RateLimiter`, `PerSymbolRateLimiter` | Sliding-window rate limiting (soft/hard caps) |
| `market_calendar.py` | `MarketCalendar`, `get_calendar()` | Taiwan market calendar (TWSE/TAIFEX) with cross-midnight night session |
| `order_ids.py` | `OrderIdResolver` | Strategy/order ID mapping (handles Shioaji ordno mutation) |
| `session_hooks.py` | `SessionHookManager`, `SessionPhase` | Pre/post market session hooks (disabled by default) |
| `secret_validator.py` | `validate_secrets()` | Startup credential validation with placeholder detection |

## Key APIs

### timebase

```python
from hft_platform.core.timebase import now_ns, monotonic_ns, coerce_ns

ts = now_ns()           # Wall-clock epoch nanoseconds (UTC)
dur = monotonic_ns()    # Monotonic for duration measurement
ns = coerce_ns(value)   # Auto-detect seconds/ms/us/ns magnitude
```

- `coerce_ns()` accepts `datetime`, `int`, `float`, or `None`
- Naive datetime assumes `HFT_TS_TZ` (default: `Asia/Taipei`)
- Optional Rust fast-path via `HFT_TIMEBASE_RUST_COERCE=1` (default on)

### PriceCodec

```python
from hft_platform.core.pricing import PriceCodec, FixedPriceScaleProvider

codec = PriceCodec(provider=FixedPriceScaleProvider(scale=10_000))
scaled = codec.scale("2330", Decimal("600.5"))   # -> 6005000
price = codec.descale("2330", 6005000)            # -> 600.5
```

- Prefers `Decimal` input to avoid IEEE 754 precision loss
- `float` converted via `str` intermediate as safety measure

### InstrumentRegistry

```python
registry.register(profile, source="static")
profile = registry.get("TXFD6")
chain = registry.get_options_chain("TX", date(2026, 4, 15))
```

- Frozen dataclasses (immutable profiles)
- Max 5000 entries with expired-first eviction
- `"static"` vs `"dynamic"` source tracking for reload safety

### MarketCalendar

```python
cal = get_calendar()
cal.is_trading_day()                          # True/False
cal.is_trading_hours(product_type="future")   # Handles cross-midnight night session
```

- TAIFEX night session: 15:00 - next day 05:00 (cross-midnight aware)
- Graceful degradation: weekday-only fallback if `exchange_calendars` unavailable
- LRU-cached session times (configurable via `HFT_CALENDAR_CACHE_SIZE`)

## Environment Variables

| Variable | Default | Purpose |
|----------|---------|---------|
| `HFT_TS_TZ` | `Asia/Taipei` | Timezone for timestamp coercion |
| `HFT_TIMEBASE_RUST_COERCE` | `1` | Enable Rust fast-path for coerce_ns |
| `HFT_MARKET_EXCHANGE` | `XTAI` | Exchange code for calendar |
| `HFT_CALENDAR_CACHE_SIZE` | `32` | LRU cache size for session times |
| `HFT_SESSION_HOOKS_ENABLED` | `0` | Enable session hook polling |
| `HFT_PER_SYMBOL_RATE_SOFT` | `30` | Per-symbol rate limit soft cap |
| `HFT_PER_SYMBOL_RATE_HARD` | `50` | Per-symbol rate limit hard cap |

## Gotchas

1. **`now_ns()` can jump backward** on NTP adjustments — use `monotonic_ns()` for elapsed durations
2. **Naive datetime silently reinterpreted** to `HFT_TS_TZ` — always use timezone-aware datetimes
3. **Rate limiter soft cap** = warning only (still accepted); hard cap = rejection
4. **Shioaji ordno mutation**: order ordno `"vA0G5"` becomes fill ordno `"vA0G671S"` — `OrderIdResolver` handles prefix-match fallback
5. **MarketCalendar cross-midnight**: 00:00-05:00 checks previous day's trading status
