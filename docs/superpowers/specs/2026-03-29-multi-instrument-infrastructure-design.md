# Multi-Instrument Infrastructure Design

**Date**: 2026-03-29
**Status**: Draft
**Scope**: Generalize HFT platform from single-instrument-class (futures) to multi-instrument-class (futures + options + equities) on TAIFEX/TWSE

---

## 1. Problem Statement

The platform was built around a flat `symbol: str` namespace optimized for a single futures contract (TMFD6). After 23 rounds of alpha research, L1 microstructure signals on TAIFEX futures are exhausted. The highest-ROI path forward requires:

1. **Options data as signal source** — TXO put/call flow, OI skew, IV surface predict futures direction
2. **Options as tradeable instruments** — direct alpha on TXO, hedging futures positions
3. **Equities as signal source** — TSMC (2330) lead-lag, cross-asset features
4. **Infrastructure that makes adding new instruments cheap**, not month-long projects

### Current Limitations

| Layer | Limitation |
|-------|-----------|
| Events | `symbol: str` flat — no instrument metadata |
| FeatureEngine | All symbols share `lob_shared_v3` — meaningless for options |
| Positions | No delta-equivalent exposure, no underlying grouping |
| Risk | No Greeks limits, global single `tick_size` |
| ClickHouse | No `instrument_type`, no options fields, OHLCV MV polluted |
| Recorder | Hardcoded 13 columns, new fields silently dropped |

### What Already Works

- Shioaji `ContractsRuntime` has options lookup path (`api.Contracts.Options`)
- Quote callbacks (`on_tick_fop_v1`) handle futures + options
- Order gateway routes `"option"` to `futopt_account`
- Config DSL has options expansion syntax (`OPT@TXO@front@ATM+-2`)
- `SymbolMetadata.product_type()` resolves `"option"` / `"future"` / `"stock"`

---

## 2. Design: InstrumentRegistry

### 2.1 Core Abstraction

`InstrumentRegistry` is a singleton that replaces `SymbolMetadata` as the canonical source of per-instrument metadata. Events keep `symbol: str` — all metadata is accessed via registry lookup.

```python
class InstrumentType(enum.Enum):
    FUTURE = "future"
    OPTION = "option"
    EQUITY = "equity"
    INDEX = "index"

class OptionRight(enum.Enum):
    CALL = "C"
    PUT = "P"

@dataclass(frozen=True, slots=True)
class FeeStructure:
    tax_rate_bps: int          # sell-side tax in bps (e.g. 20 = 2.0 bps)
    commission_per_lot: int    # scaled x10000

@dataclass(frozen=True, slots=True)
class TradingHours:
    day_open: str              # "08:45"
    day_close: str             # "13:45"
    night_open: str | None     # "15:00" or None
    night_close: str | None    # "05:00" or None

@dataclass(frozen=True, slots=True)
class InstrumentProfile:
    symbol: str
    instrument_type: InstrumentType
    underlying: str                    # "TX" / "2330" / "" for equities
    exchange: str                      # "TAIFEX" / "TWSE" / "OTC"
    multiplier: int                    # TX=200, MTX=50, TMF=10, TXO=50, stock=1000
    tick_size_scaled: int              # x10000: futures 1pt=10000, stock 0.5=5000
    price_scale: int                   # 10000 (TAIFEX default)
    fee_structure: FeeStructure
    trading_hours: TradingHours
    lot_size: int = 1                  # TAIFEX: 1 lot = 1 contract; TWSE: 1 lot = 1000 shares

    # Options-only (None for non-options)
    strike_scaled: int | None = None   # x10000
    option_right: OptionRight | None = None
    expiry: date | None = None
```

### 2.2 Registry Interface

```python
class InstrumentRegistry:
    def register(self, profile: InstrumentProfile) -> None: ...
    def get(self, symbol: str) -> InstrumentProfile: ...
    def get_by_underlying(self, underlying: str) -> list[InstrumentProfile]: ...
    def get_options_chain(self, underlying: str, expiry: date) -> list[InstrumentProfile]: ...
    def evict_expired(self, as_of: date) -> int: ...
    def bulk_register(self, profiles: Iterable[InstrumentProfile]) -> None: ...
    def contains(self, symbol: str) -> bool: ...

    # Backward-compat with SymbolMetadata
    def price_scale(self, symbol: str) -> int: ...
    def product_type(self, symbol: str) -> str: ...
    def contract_multiplier(self, symbol: str) -> int: ...
```

### 2.3 Population

| Source | When | What |
|--------|------|------|
| `symbols.yaml` | Boot | Static futures + equities profiles |
| `ContractsRuntime.refresh()` | Boot | Dynamic options profiles (bulk_register) |
| `options_chains` config DSL | Boot | Expand ATM±N strikes → register |
| First-seen callback | Runtime | Lazy register with WARNING log |

### 2.4 Cardinality Guard

- `max_instruments: int = 5000` (env: `HFT_MAX_INSTRUMENTS`)
- Eviction priority: expired options → zero-volume LRU → reject with `InstrumentLimitError`
- Eviction logged at WARNING with symbol context

### 2.5 Migration from SymbolMetadata

`SymbolMetadata` becomes a thin wrapper delegating to `InstrumentRegistry`. The wrapper must cover the **full** public API surface (8 methods + 2 attributes + 1 class constant):

**Methods** (all have production callers):

| Method | Callers |
|--------|---------|
| `price_scale(symbol) -> int` | `normalizer.py:420`, `mapper.py:28,44`, `pricing.py:26,43` |
| `contract_multiplier(symbol) -> int` | `positions.py:244,309` |
| `exchange(symbol) -> str` | `adapter.py:562`, `mapper.py:92,115`, `normalizer.py:267` (internal) |
| `product_type(symbol) -> str` | `adapter.py:585`, `validators.py:70` |
| `order_params(symbol) -> dict` | `adapter.py:597` |
| `symbols_for_tags(tags) -> set[str]` | `runner.py:296,303` |
| `reload() -> None` | indirect via `reload_if_changed` |
| `reload_if_changed() -> bool` | `market_data.py:893` |

**Attributes** (directly accessed):

| Attribute | Access |
|-----------|--------|
| `meta: dict[str, dict]` | `market_data.py:894` (read `len()`) |
| `symbols_by_tag: dict[str, set[str]]` | `test_strategy_runner_behavior.py:159,167` (write in tests) |

**Class constant**: `DEFAULT_SCALE = 10_000` — referenced by `MarketDataNormalizer` and tests.

**Migration strategy:**
- Wrapper class preserves all above signatures, delegating to `InstrumentRegistry` internals
- `meta` and `symbols_by_tag` exposed as properties that delegate to registry's internal dicts
- `reload_if_changed()` triggers `InstrumentRegistry` reload from `symbols.yaml`
- `order_params()` delegates to registry (needs `InstrumentProfile` to carry order params or a separate lookup)
- Gradual migration: new code uses `InstrumentRegistry` directly
- `SymbolMetadata` deprecated after all callers migrated (no timeline pressure)

---

## 3. Design: FeatureEngine Multi-Class Dispatch

### 3.1 Feature Set Routing

`FeatureEngine._get_or_create_state()` queries `InstrumentRegistry` to select the feature set:

| InstrumentType | Feature Set | Features |
|----------------|-------------|----------|
| FUTURE | `lob_shared_v3` | 27 (existing, unchanged) |
| EQUITY | `lob_shared_v3` | 27 (same LOB features apply) |
| OPTION | `option_flow_v1` | 6 (new) |
| INDEX | None | Skip feature computation |

### 3.2 option_flow_v1 Feature Set

New file: `src/hft_platform/feature/option_features.py` (~200 LOC)

| Slot | Feature | Source | Description |
|------|---------|--------|-------------|
| [0] | `put_call_volume_ratio_x1000` | tick | Rolling P/C volume ratio |
| [1] | `oi_change_net_x1000` | snapshot API | OI delta (needs periodic fetch) |
| [2] | `atm_iv_spread_x1000` | computed | Call IV − Put IV at ATM |
| [3] | `volume_weighted_strike_x100` | tick | Volume-weighted strike centroid |
| [4] | `option_depth_imbalance_x1000` | bidask | Bid/ask depth across chain |
| [5] | `flow_toxicity_x1000` | tick | Large/small order ratio |

### 3.3 CrossInstrumentEngine

New file: `src/hft_platform/feature/cross_instrument.py` (~150 LOC)

- Separate from per-symbol FeatureEngine
- Triggered when underlying OR any linked option receives an event
- Aggregates option chain state → emits `FeatureUpdateEvent` with `symbol = underlying`
- Examples: net delta across chain, IV term structure slope, options volume surge

### 3.4 Skip Logic

- OTM options with zero volume in last 60s → skip feature computation
- `warmup_min_events` per instrument_type: options=100, futures=2400
- Stale book (last update > 60s) → mark features as stale, don't propagate

### 3.5 Change Scope

**Corrected entry points** (no `_get_or_create_state()` method exists):

- Main entry: `process_lob_update()` (`engine.py:346`) — called from `MarketDataService._maybe_update_features()`
- Tick entry: `on_tick()` (`engine.py:765`) — **already wired** in `market_data.py:960` (not a pending fix)
- State init: `_lob_kernel_states` is lazily populated by `process_lob_update`, not by `on_tick`. `on_tick` silently no-ops if kernel state doesn't exist for the symbol.

**Instrument-type routing** must be added in `process_lob_update()`, not a hypothetical `_get_or_create_state()`:

```python
# In process_lob_update(), before kernel state init:
profile = self._registry.get(symbol)  # InstrumentRegistry lookup
if profile.instrument_type == InstrumentType.INDEX:
    return  # skip
feature_set_id = _INSTRUMENT_FEATURE_SET[profile.instrument_type]
# Use feature_set_id for state init and computation
```

| File | Change |
|------|--------|
| `feature/engine.py` | Add instrument_type routing in `process_lob_update()` (~15 LOC). Inject `InstrumentRegistry` in `__init__`. |
| `feature/registry.py` | Register `option_flow_v1` feature set |
| `feature/option_features.py` | NEW: option-specific feature computations |
| `feature/cross_instrument.py` | NEW: cross-instrument aggregation engine |
| Existing futures/equity path | Zero changes |

**Note**: The `on_tick()` wiring fix listed in Phase 1 is **already done** (`market_data.py:960`). Removed from Phase 1 scope. However, `on_tick` for options symbols needs the kernel state to be initialized first by a `process_lob_update` call — this ordering dependency is already guaranteed by the existing pipeline (BidAsk events arrive before/alongside Tick events).

---

## 4. Design: Position & Risk — Options Extension

### 4.1 PortfolioView (Read-Only Overlay)

`Position` dataclass is **not modified**. Greeks exposure is computed on-demand:

```python
@dataclass(frozen=True, slots=True)
class UnderlyingExposure:
    underlying: str
    futures_delta: int          # net_qty × multiplier (existing positions)
    options_delta: int          # Σ(net_qty × delta × multiplier) per option
    options_gamma: int          # Σ(net_qty × gamma × multiplier)
    options_vega: int           # Σ(net_qty × vega × multiplier)
    net_delta: int              # futures + options combined
    option_positions: list      # all option positions on this underlying

class PortfolioView:
    """Read-only aggregation over PositionStore + InstrumentRegistry + GreeksProvider."""

    def __init__(self, position_store, instrument_registry, greeks_provider): ...
    def get_underlying_exposure(self, account, strategy, underlying) -> UnderlyingExposure: ...
    def mark_to_market_portfolio(self, underlying, mid_prices) -> PortfolioPnL: ...
```

- No new state — queries `PositionStore` and `InstrumentRegistry` on each call
- Greeks sourced from `GreeksProvider` interface (pluggable)
- Options M2M uses option mid price (market-based, not theoretical)

### 4.2 GreeksProvider

```python
class GreeksProvider(Protocol):
    def get_greeks(self, symbol: str) -> Greeks: ...

@dataclass(frozen=True, slots=True)
class Greeks:
    delta_x10000: int
    gamma_x10000: int
    theta_x10000: int
    vega_x10000: int
```

| Implementation | Method | Accuracy | Latency |
|----------------|--------|----------|---------|
| `Black76GreeksProvider` (v1) | Black-76 formula | ±5% for ATM | <1ms |
| `BrokerGreeksProvider` (v2) | Shioaji snapshot API | Broker-grade | ~100ms |

v1 ships first. Uses conservative vol assumption (30%) until IV can be derived from market data. Update frequency: every 60s or when underlying moves > 0.5%.

### 4.3 Risk Validators (Options-Specific)

**Wiring prerequisite**: The current `RiskValidator` base class (`validators.py:16`) only accepts `(config, price_scale_provider, lob)`. The `check(intent: OrderIntent) -> Tuple[bool, str]` interface takes only an `OrderIntent`. Greeks validators need `PositionStore`, `GreeksProvider`, and `InstrumentRegistry` at construction time.

**Required changes to existing code:**

1. `RiskEngine.__init__` (`engine.py:94`): Add 3 optional kwargs: `position_store`, `greeks_provider`, `instrument_registry`. Forward to new validators only.
2. New validators subclass `RiskValidator` directly, inject deps in their own `__init__`, bypass base class for new deps (zero-footprint path — existing validators untouched).
3. `check()` contract unchanged — new validators read from `self.position_store` / `self.greeks_provider` internally.
4. Bootstrap caller must pass the new deps when constructing `RiskEngine`.

**New validators** (only activated for `instrument_type == OPTION`):

| Validator | Config Key | Logic |
|-----------|-----------|-------|
| `DeltaLimitValidator` | `max_net_delta_lots` | Queries `PortfolioView.get_underlying_exposure()`, reject if \|new_net_delta\| > limit per underlying |
| `GammaLimitValidator` | `max_net_gamma` | Queries `PortfolioView`, strict on expiry week (pin risk) |
| `VegaLimitValidator` | `max_portfolio_vega` | Total portfolio vega cap via `PortfolioView` |

Each validator skips non-option intents early: `if registry.get(intent.symbol).instrument_type != OPTION: return (True, "OK")`.

### 4.4 tick_size Fix

`RiskEngine._init_rust_validator()` currently uses a single global `tick_size`. Change to:
- `PriceBandValidator`: lookup `InstrumentRegistry.get(symbol).tick_size_scaled`
- `RustRiskValidator`: pass per-symbol tick_size (or use conservative minimum)

### 4.5 Expiry Handling

- Daily job: `evict_expired_positions(as_of=today)` — close expired option positions
- ITM auto-exercise: log WARNING only, do not auto-process (requires human confirmation)
- Expiry-day risk: `PositionLimitValidator` tightens limits 50% on expiry day

---

## 5. Design: ClickHouse Schema & Recorder

### 5.1 Schema Migration (Additive)

Migration file: `20260330_001_add_instrument_columns.sql`

```sql
-- hft.market_data
ALTER TABLE hft.market_data ADD COLUMN IF NOT EXISTS
    instrument_type LowCardinality(String) DEFAULT '';
ALTER TABLE hft.market_data ADD COLUMN IF NOT EXISTS
    underlying LowCardinality(String) DEFAULT '';
ALTER TABLE hft.market_data ADD COLUMN IF NOT EXISTS
    strike_scaled Int64 DEFAULT 0;
ALTER TABLE hft.market_data ADD COLUMN IF NOT EXISTS
    option_right LowCardinality(String) DEFAULT '';
ALTER TABLE hft.market_data ADD COLUMN IF NOT EXISTS
    expiry Date DEFAULT '1970-01-01';

-- hft.orders
ALTER TABLE hft.orders ADD COLUMN IF NOT EXISTS
    instrument_type LowCardinality(String) DEFAULT '';
ALTER TABLE hft.orders ADD COLUMN IF NOT EXISTS
    oc_type LowCardinality(String) DEFAULT '';

-- hft.fills
ALTER TABLE hft.fills ADD COLUMN IF NOT EXISTS
    instrument_type LowCardinality(String) DEFAULT '';
ALTER TABLE hft.fills ADD COLUMN IF NOT EXISTS
    oc_type LowCardinality(String) DEFAULT '';
```

### 5.2 OHLCV Materialized View Fix

```sql
-- Drop and recreate to exclude options
DROP TABLE IF EXISTS hft.ohlcv_1m_mv;

CREATE MATERIALIZED VIEW hft.ohlcv_1m_mv TO hft.ohlcv_1m AS
SELECT ...
FROM hft.market_data
WHERE type = 'Tick'
  AND price_scaled > 0
  AND instrument_type IN ('', 'future', 'equity')   -- NEW filter
...
```

Execute during non-trading hours. Rebuild < 5s.

### 5.3 New Table: options_chain_snapshot

```sql
CREATE TABLE IF NOT EXISTS hft.options_chain_snapshot (
    underlying          LowCardinality(String),
    expiry              Date,
    strike_scaled       Int64,
    option_right        LowCardinality(String),
    snapshot_ts         Int64,
    bid_price_scaled    Int64,
    ask_price_scaled    Int64,
    last_price_scaled   Int64,
    volume              Int64,
    open_interest       Int64,
    iv_x10000           Int64,
    delta_x10000        Int64,
    gamma_x10000        Int64,
    vega_x10000         Int64,
    theta_x10000        Int64
) ENGINE = MergeTree()
ORDER BY (underlying, expiry, strike_scaled, option_right, snapshot_ts)
PARTITION BY toYYYYMMDD(toDateTime(snapshot_ts / 1000000000))
TTL toDateTime(snapshot_ts / 1000000000) + INTERVAL 3 MONTH;
```

### 5.4 Recorder Pipeline Changes

| File | Change |
|------|--------|
| `recorder/worker.py` | Extend `MARKET_DATA_COLUMNS` with 5 new fields; update `_extract_market_data_values()` |
| `recorder/mapper.py` | `map_event_to_record()`: add InstrumentRegistry lookup to populate instrument metadata |
| `recorder/options_snapshot_recorder.py` | NEW (~100 LOC): aggregate active options LOB → batch write every 60s |

### 5.5 WAL Compatibility

**Important**: INSERT uses explicit `column_names=cols` (not schema-less append). Two independent column lists exist:

| List | Location | Used by |
|------|----------|---------|
| `MARKET_DATA_COLUMNS` | `recorder/worker.py:16` | Live ingest Batcher path |
| `_MARKET_DATA_COLS` | `recorder/_loader_batch.py:23` | WAL replay loader |

**Compatibility matrix:**

| Scenario | Result | Action |
|----------|--------|--------|
| Old WAL → new schema | Safe — new columns get DEFAULT values | No action needed |
| New WAL → old schema | **FAILS** — INSERT references columns that don't exist | **Forward replay requires migration first** |
| New ingest code → old schema | **FAILS** — same column mismatch | Migration must run before deploying new recorder code |

**Deployment order (strict)**:
1. Run ClickHouse migration (add columns with DEFAULT)
2. Deploy new recorder code (extended column lists)
3. WAL files written after step 2 can only replay on migrated schema

**Both column lists must be updated in sync** — `worker.py:MARKET_DATA_COLUMNS` and `_loader_batch.py:_MARKET_DATA_COLS`. A mismatch between them would cause live-ingest and WAL-replay to produce different column sets, leading to silent data loss or insert errors.

---

## 6. Design: OrderIntent & Shioaji Wiring

### 6.1 Contract Layer Changes

```python
class OCType(enum.Enum):
    AUTO = "auto"      # OrderAdapter determines from position state
    OPEN = "open"      # Explicit new position
    CLOSE = "close"    # Explicit close position

# OrderIntent — add 1 field
@dataclass(slots=True)
class OrderIntent:
    ...                         # all existing fields unchanged
    oc_type: OCType = OCType.AUTO   # NEW

# FillEvent — add 1 field
@dataclass(slots=True)
class FillEvent:
    ...                         # all existing fields unchanged
    oc_type: str = ""               # NEW: broker-reported open/close
```

No changes to `OrderCommand`, `PositionDelta`, or other contracts.

### 6.2 Shioaji Adapter Wiring

| Component | Change |
|-----------|--------|
| `ContractsRuntime` | On `refresh()`, call `InstrumentRegistry.bulk_register()` with options profiles extracted from Shioaji contract objects (strike, right, expiry) |
| `SubscriptionManager` | No change — subscribe() is product-type-agnostic |
| `QuoteRuntime` | No change — `on_tick_fop_v1` handles futures + options |
| `OrderGateway._place_order_typed()` | Read `OrderIntent.oc_type`: AUTO → query PositionStore; OPEN/CLOSE → direct map to `sj.constant.FuturesOCType` |

### 6.3 symbols.yaml Extension

```yaml
# Static futures (existing)
- code: "TXFC0"
  exchange: FUT
  tags: [futures, front_month, txf]

# Static equities (existing)
- code: "2330"
  exchange: TSE
  tags: [stocks, tw50]

# Dynamic options chains (NEW)
options_chains:
  - root: TXO
    expiry: front          # front-month
    strikes: ATM+-5        # ATM and 5 strikes each side
    rights: [C, P]
  - root: TXO
    expiry: near           # near-month
    strikes: ATM+-3
    rights: [C, P]
```

Bootstrap flow: load `symbols.yaml` → expand `options_chains` via existing `_expand_options()` DSL → `ContractsRuntime` resolves contracts → `InstrumentRegistry.bulk_register()`.

### 6.4 on_tick() Wiring — Already Done

`feature_engine.on_tick()` is already called at `market_data.py:960` inside `_maybe_update_features()`, guarded by `isinstance(event, TickEvent) and event.trade_direction != 0`. No fix needed.

**Caveat for options**: `on_tick()` is a no-op if `_lob_kernel_states[symbol]` hasn't been initialized by a prior `process_lob_update()` call. This is fine — BidAsk events arrive before/alongside Tick events in the existing pipeline, ensuring kernel state exists before `on_tick` runs.

---

## 7. Phase Plan

| Phase | Scope | Duration | Deliverable |
|-------|-------|----------|-------------|
| **1: Foundation** | InstrumentRegistry, SymbolMetadata wrapper, CH migration (+ deploy order gate), recorder extension (worker.py + _loader_batch.py sync), vrr cleanup | ~1 week | Existing futures pipeline unchanged, new columns in CH |
| **2: Options Data Path** | symbols.yaml DSL wiring, ContractsRuntime → registry, TXO subscription, options_chain_snapshot recorder, FeatureEngine type dispatch | ~1 week | TXO data flowing into ClickHouse |
| **3: Options Features & Risk** | option_flow_v1 (6 features), GreeksProvider (Black-76), PortfolioView, Delta/Gamma/Vega validators, OrderIntent.oc_type, OrderGateway wiring | ~1 week | Shadow-mode options orders with Greeks risk gates |
| **4: Cross-Instrument & Equity** | CrossInstrumentEngine, equity subscription, cross-asset lead-lag signal pipeline | ~1 week | Cross-instrument features to FeatureUpdateEvent |
| **5: EMO + Signal Research** | EMO trade classifier integration, signed OFI features, options flow → futures alpha research | Ongoing | New alpha research lines unlocked |

### Phase Dependencies

```
Phase 1 → Phase 2 → Phase 3
                  ↘ Phase 4
Phase 5 depends on Phase 2 (data) + EMO (independent)
```

---

## 8. Risk Assessment

### High Risk

| Risk | Impact | Mitigation |
|------|--------|------------|
| Options contract explosion → memory | OOM if 500+ BookStates with numpy arrays | Cardinality guard (5000), skip zero-volume OTM, evict expired |
| ClickHouse MV recreate → OHLCV gap | Missing 1-min candles during rebuild | Execute during non-trading hours, rebuild < 5s |
| Greeks calculation inaccuracy → wrong risk gates | Orders rejected/accepted incorrectly | Conservative vol (30%), compare with broker snapshot, log discrepancies |

### Medium Risk

| Risk | Impact | Mitigation |
|------|--------|------------|
| Options tick volume → recorder throughput | Batcher queue full, data dropped | Only record active strikes, snapshot at 60s, monitor `recorder_queue_depth` |
| SymbolMetadata replacement → hidden coupling | Runtime errors from callers using old API | Backward-compat wrapper, deprecation warnings, grep for all call sites |
| TXO data quality (99.7% quotes per R17) | option_flow features have no signal | Data pipeline still valuable for accumulation; features gated by min volume |

### Low Risk

| Risk | Impact | Mitigation |
|------|--------|------------|
| Shioaji options API instability | Quote gaps | Existing reconnect + flap detection applies |
| WAL compatibility | Data loss on replay | Analyzed: bidirectional safe (defaults fill missing fields) |

---

## 9. Out of Scope (YAGNI)

- Rich event types (`OptionTickEvent`) — registry lookup sufficient
- Real-time IV surface fitting — v1 uses single-point Black-76
- Automatic exercise handling — log WARNING, human decides
- Cross-exchange support — TAIFEX + TWSE/OTC only
- Options pricing engine for theoretical value trading
- Event bus architecture changes — current RingBufferBus sufficient
- Backfill existing ClickHouse data with instrument_type

---

## 10. Success Criteria

| Criterion | Measurement |
|-----------|-------------|
| Existing futures pipeline unaffected | `make ci` passes, production metrics unchanged |
| TXO data flowing | ClickHouse query returns TXO ticks within 5 min of market open |
| Options features computed | `option_flow_v1` features non-zero for ATM options with volume |
| Greeks risk gates functional | Shadow-mode order rejected when delta limit exceeded |
| Cross-instrument signal | FeatureUpdateEvent emitted for TX underlying from TXO chain data |
| Cardinality stable | InstrumentRegistry size < 5000 after full trading day with options |
| No regression | Zero new test failures, coverage ≥ 70% maintained |
