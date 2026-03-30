# TXO Options Trading Infrastructure — Design Spec

**Date**: 2026-03-30
**Status**: Draft (v3 — post-review-2)
**Author**: Claude (brainstorming session)

## 1. Overview

Build a complete TXO options trading infrastructure on top of the existing futures HFT platform, culminating in an Electronic Eye system for automated market-making and delta-neutral hedging.

### Design Principles

- **Minimal architectural change**: Reuse existing subscription, recorder, risk, order, and strategy frameworks
- **Research-first**: Offline validation of pricing models before live deployment
- **Phased delivery**: 3 phases (data+compute → risk+execution → strategy), each an independent spec → plan → implementation cycle
- **Respect existing config flow**: `config/symbols.list` is the single source of truth; `_expand_options()` already resolves `OPT@TXO@near@ATM+/-10`

### Scope Summary

| Phase | Name | New Code | Reuse |
|-------|------|----------|-------|
| 1 | Data + Pricing Core | ~600 LOC | symbols.list, _expand_options(), contract cache, recorder, numpy/scipy |
| 2 | Risk + Execution Helpers | ~500 LOC | RiskEngine (modified), strategy base, Monitor TUI |
| 3 | Electronic Eye Strategy | ~500 LOC | StrategyRunner, strategy registry |
| **Total** | | **~1,600 LOC** | |

(LOC estimates exclude tests)

### Dependency Graph

```
Phase 1 (Data + Pricing) ──→ Phase 2 (Risk + Execution) ──→ Phase 3 (Electronic Eye)
```

### Out of Scope (Separate Spec)

- **Combo Orders**: Full combo path (strategy → risk → order → execution normalizer → router) requires boundary changes across 5+ modules. Separate spec: `combo-order-infrastructure.md`.
- SVI / SABR volatility surface fitting
- American option pricing (TXO is European-style)
- Multi-underlying support (only TXO/TX pair)
- Rust kernel for IV solver

### Prerequisites Identified (Must Be Built)

This spec identifies 3 gaps in existing infrastructure that do not exist today and must be built as part of the phased work. They are not assumed to exist:

1. **Risk rejection feedback channel** (Phase 2) — RiskEngine currently only logs/metrics on rejection (`engine.py:354-356`, comment: `# In real system: Feedback to strategy via side channel`). Strategies cannot detect their OrderIntent was rejected. Phase 2 must build this.
2. **OrderIntent price_type field** (Phase 2) — `OrderIntent` (`contracts/strategy.py:32-63`) has no `price_type` field. `order/adapter.py:628` resolves price_type from symbol metadata, defaulting to `"LMT"`. Phase 2 must add this field for MKT+IOC hedging.
3. **Strategy-side publish capability** (Phase 2) — `StrategyContext` (`strategy/base.py:20-35`) is read-only with no Redis or publish slots. Phase 2 must add an async publish callback.

---

## 2. Phase 1: Data Foundation + Pricing Core

### 2.1 TXO Data Ingestion

#### Goal

Fix incomplete option metadata in `_expand_options()` output, validate ClickHouse recording, and add R1/R2 continuous contract support.

#### Current State (Verified)

- `config/symbols.list:30` has `OPT@TXO@near@ATM+/-10` **active**
- `config/_symbols_expansion.py:235` has `_expand_options()` **fully implemented** — resolves root/month/strike selector, picks ATM reference price, expands call+put entries
- `config/base/symbols.yaml:290-340` has manually listed TXO ATM ±5 (hardcoded April 2026 codes) as fallback/override
- `client.py:659` loads via `data.get("symbols", [])` from YAML
- `bootstrap.py:773` loads symbols via `SymbolMetadata(symbols_path)` — runtime depends on YAML having complete metadata fields
- Recorder pipeline handles `TickFOPv1`/`BidAskFOPv1` identically to futures — zero change needed

#### Real Gap

`_expand_options()` calls `build_entry()` (`_symbols_expansion.py:43-74`) which copies metadata from the contract index. If the broker's contract cache doesn't include `tick_size`, `price_scale`, `point_value`, `right`, `strike`, or `expiry` for options, the generated `symbols.yaml` entries will be **incomplete**. This directly breaks:

- `SymbolMetadata.order_params()` — missing `tick_size` → wrong lot/price calculations
- Greeks computation — missing `strike`/`expiry`/`point_value` → wrong delta/gamma/PnL
- Phase 3 Electronic Eye — all pricing depends on correct option metadata

#### Changes

**`config/_symbols_expansion.py` — Harden `build_entry()` for options** (~40 LOC):

1. When `product_type == "option"`, require and populate:
   - `right` (C/P) — parsed from Shioaji contract code (month code A-L = Call, M-X = Put)
   - `strike` — from `contract.strike_price`
   - `expiry` — from `contract.delivery_date`
   - `point_value` — TXO fixed at 50 (configurable per root in `symbols.list` attrs)
   - `tick_size` — TXO: 1.0 for premium ≥ 10, 0.1 for premium < 10 (from contract or default rule)
   - `price_scale` — default 10000 (platform convention)
   - `underlying` — root mapping (TXO → TX)
   - `tax_rate_bps`, `commission_per_lot` — from `symbols.list` attrs or defaults
2. Emit structlog WARNING for any missing required field (fail-loud, not silent default)
3. Add validation in `make sync-symbols`: count expanded TXO entries, assert ≥ expected count

**`contracts_runtime.py` — R1/R2 continuous contract** (~10 LOC):

In `_get_contract()` (line 33), add branch: if code ends with `R1` or `R2`, resolve via `api.Contracts.Futures.<root>.{root}R1`. Used by research/backtest pipeline for continuous data.

**Manual `symbols.yaml` entries (lines 290-340)** — Keep as override/fallback until `_expand_options()` output is validated. Remove once automated expansion is confirmed correct for 2 consecutive monthly rollovers.

#### Validation

```sql
SELECT count(), uniq(code)
FROM hft.market_data
WHERE code LIKE 'TXO%' AND toDate(exch_ts/1e9) = today()
-- Expected: count > 0, uniq(code) >= 22
```

Plus: `make sync-symbols` output compared field-by-field against manual entries for one expiry cycle.

---

### 2.2 Pricing Core — `options/` Module

#### Goal

Build offline-capable options pricing engine: IV solver, Greeks calculator, volatility surface.

#### Module Location

`src/hft_platform/options/` — new package, peer to `alpha/`, `feature/`.

#### Float Policy — Two-Layer Boundary

| Layer | Modules | Precision | Rationale |
|-------|---------|-----------|-----------|
| **Analytics (offline)** | `pricing.py`, `greeks.py`, `surface.py` | `float` | Research/offline computation. Per Rule 25 §11: float permitted in offline-only modules. This package is added to the §11 exemption list alongside `alpha/` and `research/`. |
| **Live Adapter** | `live_adapter.py` (Phase 2) | outputs `int` / `bool` only | Any value crossing into `risk/`, `execution/`, or `strategy/` hot path must be converted. The adapter is the firewall. |

**Boundary contract** — No `float` from `options/` ever enters:
- `OrderIntent` (price, qty, decision_mid, decision_price — all `int`)
- `RiskDecision` (pass: `bool`, reason: `str`)
- `PositionDelta` (all `int`)
- `OrderCommand` (all `int`)

The adapter converts:
- `net_delta: float` → `hedge_lots: int` (rounded)
- `greeks_utilization: float` → `breach: bool` (threshold comparison)
- `worst_case_pnl: float` → Prometheus gauge only (observability path, never decision path)

#### Components

**`options/pricing.py`** (~200 LOC)

```python
def black76_price(F: float, K: float, T: float, sigma: float, r: float, cp: str) -> float:
    """Black-76 option price. F=futures, K=strike, T=years, sigma=vol, r=rate, cp='C'|'P'"""

def solve_iv(market_price: float, F: float, K: float, T: float, r: float, cp: str) -> float:
    """Implied volatility via Newton-Raphson + Brent fallback. Returns NaN for deep OTM."""
```

Design decisions:
- **Model**: Black-76 (standard for futures options; TXO settles against TX futures)
- **IV solver**: Newton-Raphson primary (quadratic convergence), Brent fallback (guaranteed convergence)
- **Initial guess**: Brenner-Subrahmanyam approximation: `σ₀ ≈ √(2π/T) × C/F`
- **Convergence**: `|f(σ)| < 1e-8`, max 50 iterations
- **Boundary**: market_price < 0.5 × tick_size → return `NaN` (deep OTM, unreliable IV)
- **TXO tick size**: 1 point for premium ≥ 10, 0.1 point for premium < 10 (per TAIFEX rules). Already in `symbols.yaml` per-strike after Phase 1 expansion fix.
- **Dependencies**: `scipy.optimize.brentq` (already in requirements), `numpy`

**`options/greeks.py`** (~150 LOC)

```python
@dataclass(slots=True)
class GreeksResult:
    delta: float
    gamma: float
    theta: float  # per day
    vega: float   # per 1% vol move
    rho: float

def compute_greeks(F: float, K: float, T: float, sigma: float, r: float, cp: str) -> GreeksResult:
    """Black-76 closed-form Greeks (analytic, not numerical)."""

@dataclass(slots=True)
class PositionGreeks:
    symbol: str
    qty: int
    greeks: GreeksResult

@dataclass(slots=True)
class AggregatedGreeks:
    net_delta: float      # futures-equivalent lots
    net_gamma: float
    net_theta_ntd: float  # NTD per day
    net_vega_ntd: float   # NTD per 1% vol
    positions: tuple[PositionGreeks, ...]

def portfolio_greeks(positions: list[PositionGreeks], multiplier: float) -> AggregatedGreeks:
    """Linear aggregation of position Greeks. multiplier = contract multiplier (50 for TXO)."""
```

Formulas (Black-76):
- Delta_C = `e^{-rT} × N(d1)`, Delta_P = `e^{-rT} × (N(d1) - 1)`
- Gamma = `e^{-rT} × n(d1) / (F × σ × √T)`
- Theta, Vega, Rho: standard Black-76 closed-form
- Put-call parity check: `Delta_C - Delta_P ≈ e^{-rT}`

**`options/surface.py`** (~200 LOC)

```python
class VolSurface:
    def update(self, strike: float, expiry_date: date, iv: float) -> None:
        """Update a single grid point."""

    def get_iv(self, strike: float, expiry_date: date) -> float:
        """Interpolated IV. Strike: cubic spline. Expiry: linear."""

    def snapshot(self) -> dict[tuple[date, float], float]:
        """Current grid as dict."""

    def skew_25d(self, expiry_date: date) -> float:
        """25-delta put IV - 25-delta call IV."""

    def butterfly_25d(self, expiry_date: date) -> float:
        """0.5 × (25d_put_IV + 25d_call_IV) - ATM_IV."""
```

Design decisions:
- Data structure: `dict[(expiry_date, strike)] → iv` — simple grid, no SVI fitting
- Interpolation: cubic spline on strike dimension, linear on expiry dimension
- Staleness: IV < 0.01 or IV > 2.0 marked stale, excluded from interpolation
- Rebuild: every N seconds from live IV (offline: from ClickHouse batch)

#### Offline vs Real-time

Phase 1 is **offline research only**:
- Offline: `research/experiments/` scripts read ClickHouse TXO data → call `options.*`
- Real-time hookup deferred to Phase 3 (ElectronicEye strategy consumes `options.*` directly)

#### Validation

- Unit test: known BS solutions → pricing error < 0.01 tick
- Historical: back-calculate IV from TXO data, compare to TAIFEX settlement IV (< 0.5% deviation)
- Put-call parity: `Delta_C - Delta_P ≈ e^{-rT}` consistency check

---

## 3. Phase 2: Risk Extension + Execution Helpers

### 3.1 Risk Rejection Feedback Channel (New Infrastructure)

#### Problem

`RiskEngine.evaluate()` returns `RiskDecision` to its internal `run()` loop. When rejection occurs (`engine.py:353-356`), it only logs + increments metrics. The comment `# In real system: Feedback to strategy via side channel` confirms this is a known TODO. No event is published to the bus; `events.py` has no rejection event type; strategies have no way to detect their intent was rejected.

#### Design

Add a **rejection callback** pattern (not an EventBus event — EventBus is for market data, adding risk events would mix concerns and potentially block the hot path).

**`contracts/strategy.py` — New `RiskFeedback` dataclass** (~10 LOC):

```python
@dataclass(slots=True, frozen=True)
class RiskFeedback:
    intent_id: int
    strategy_id: str
    symbol: str
    reason_code: str
    timestamp_ns: int
```

**`risk/engine.py` — Rejection dispatch** (~15 LOC):

Add optional `rejection_sink: asyncio.Queue[RiskFeedback] | None = None` to constructor. In `run()` at line 354, after logging:

```python
if self._rejection_sink is not None:
    try:
        self._rejection_sink.put_nowait(RiskFeedback(
            intent_id=intent.intent_id,
            strategy_id=intent.strategy_id,
            symbol=intent.symbol,
            reason_code=decision.reason_code,
            timestamp_ns=timebase.now_ns(),
        ))
    except asyncio.QueueFull:
        pass  # drop feedback on backpressure, never block risk path
```

Bounded queue (maxsize=256). Drop on full — rejection feedback is best-effort, never blocks the risk hot path.

**`services/bootstrap.py`** — Wire the queue (~5 LOC):

Create the rejection queue and pass to both `RiskEngine` (producer) and `StrategyRunner` (consumer). StrategyRunner drains it and dispatches to the relevant strategy's `on_risk_feedback(feedback: RiskFeedback)` method (default no-op in base class).

**`strategy/base.py` — New callback** (~5 LOC):

```python
def on_risk_feedback(self, feedback: RiskFeedback) -> None:
    """Called when RiskEngine rejects an OrderIntent from this strategy. Override to react."""
    pass
```

ElectronicEye (Phase 3) overrides this to drive Guardian state machine.

---

### 3.2 OrderIntent price_type Field (New Field)

#### Problem

`OrderIntent` (`contracts/strategy.py:32-63`) has `tif: TIF` (LIMIT/IOC/FOK/ROD) but no `price_type` field. `order/adapter.py:628` resolves price_type from `SymbolMetadata.order_params()`, defaulting to `"LMT"`. A strategy cannot request MKT+IOC for hedging.

#### Design

**`contracts/strategy.py` — Add field** (~3 LOC):

```python
@dataclass(slots=True)
class OrderIntent:
    ...
    price_type: str = "LMT"  # "LMT" | "MKT" | "MKP"
```

Default `"LMT"` preserves backward compatibility — all existing strategies emit LMT implicitly.

**`order/adapter.py` — Prefer intent field** (~5 LOC):

At line 628, change priority: use `intent.price_type` if non-default, otherwise fall back to symbol metadata:

```python
# Before:
price_type = self._broker_codec.encode_price_type(str(order_params.get("price_type", "LMT")))

# After:
intent_price_type = getattr(intent, "price_type", "LMT")
raw_price_type = intent_price_type if intent_price_type != "LMT" else str(order_params.get("price_type", "LMT"))
price_type = self._broker_codec.encode_price_type(raw_price_type)
```

**`strategy/base.py` — `place_order()` passthrough** (~3 LOC):

Add `price_type: str = "LMT"` parameter to `StrategyContext.place_order()`, pass through to `OrderIntent`.

**Guard**: `adapter.py` already rejects MKT+ROD (line 629-641). MKT+IOC is allowed. `price=0` for MKT orders (adapter handles this for broker SDK).

---

### 3.3 Greeks Limit Validation — RiskEngine Modification

#### Current State (Verified)

- `engine.py:119-126`: Validators hardcoded in `__init__()` — no `register_validator()`
- `engine.py:391-436`: When `_rust_validator is not None` and passes, **Python validators are skipped entirely**. Python validators only run on Rust error fallback or when Rust is disabled.
- Validator interface: `v.check(intent) → (bool, str)`

#### Design

GreeksLimitValidator cannot be added to the Python `self.validators` list because it would be bypassed by the Rust fast path. Instead, add it as a **post-Rust validator** — a separate check that runs after the Rust/Python validator block, similar to how `StormGuard` (step 1) and `FastGate` (step 0) are independent checks, not part of the validator list.

**`risk/engine.py` — Post-validator Greeks check** (~20 LOC):

After the validator block (after line 436) and before the final decision, add:

```python
# 3. Greeks limit check (options-only, runs after Rust/Python validators)
if self._greeks_validator is not None:
    ok, reason = self._greeks_validator.check(intent)
    if not ok:
        self._emit_trace("risk_reject", intent, {"stage": "greeks_limit", "reason": reason})
        return RiskDecision(False, intent, reason)
```

This mirrors the StormGuard pattern: independent check, not inside the validator list, not affected by Rust fast path.

**`risk/greeks_limit_validator.py`** (~80 LOC):

```python
class GreeksLimitValidator:
    """Post-validator Greeks limit check. Runs after Rust/Python validator block.

    Interface: check(intent) → (bool, str) — same as other validators.
    All internal computation uses float (analytics layer).
    Output is bool — no float crosses into RiskDecision/OrderCommand.
    """

    def __init__(self, config: dict, greeks_provider: GreeksProvider | None):
        self._limits = config.get("greeks_limits", {})
        self._provider = greeks_provider
        self._enabled = bool(self._limits.get("enabled", False))

    def check(self, intent: Any) -> tuple[bool, str]:
        if not self._enabled or self._provider is None:
            return (True, "")
        # Simulate adding this intent, check if limits would breach
        sim = self._provider.simulated_greeks_after(intent)
        if abs(sim.net_delta) > self._limits.get("net_delta_lots", 999999):
            return (False, "GREEKS_DELTA_LIMIT")
        # ... gamma, vega, theta checks
        return (True, "")
```

**Constructor wiring** (`engine.py`): Add `greeks_provider: GreeksProvider | None = None` parameter. `self._greeks_validator = GreeksLimitValidator(self.config, greeks_provider) if greeks_provider else None`.

**`GreeksProvider` protocol** — injected dependency:

```python
class GreeksProvider(Protocol):
    def current_portfolio_greeks(self) -> AggregatedGreeks: ...
    def simulated_greeks_after(self, intent: Any) -> AggregatedGreeks: ...
```

Concrete implementation: `options/live_adapter.py` (§3.5).

#### Configuration

```yaml
# config/base/risk_greeks.yaml
greeks_limits:
  net_delta_lots: 50
  net_gamma_lots: 20
  net_vega_ntd: 500000
  net_theta_ntd: -200000
  enabled: false            # disabled until Phase 3 deployment
```

---

### 3.4 Stress Test — Offline CLI

#### `risk/stress_test.py` (~150 LOC)

```python
@dataclass(slots=True)
class ScenarioConfig:
    name: str
    underlying_shift_pct: float
    vol_shift_abs: float

@dataclass(slots=True)
class ScenarioResult:
    name: str
    underlying_shift_pct: float
    vol_shift_abs: float
    pnl_ntd: float
    greeks_after: AggregatedGreeks

def run_stress_test(
    positions: list[PositionGreeks],
    surface: VolSurface,
    scenarios: list[ScenarioConfig],
    underlying_price: float,
    multiplier: float,
) -> list[ScenarioResult]:
    """Run all scenarios. For each: shift underlying + vol, reprice, compute P&L delta."""
```

Configuration:

```yaml
# config/base/stress_scenarios.yaml
scenarios:
  - name: "underlying_down_3pct"
    underlying_shift_pct: -3.0
    vol_shift_abs: 0.0
  - name: "underlying_down_3pct_vol_up_5"
    underlying_shift_pct: -3.0
    vol_shift_abs: +0.05
  - name: "vol_crush_10"
    underlying_shift_pct: 0.0
    vol_shift_abs: -0.10
  - name: "worst_case"
    underlying_shift_pct: -5.0
    vol_shift_abs: +0.10
```

Execution timing:
- **Phase 2**: Offline only — called from `research/experiments/` scripts or a CLI subcommand added to `cli.py` (existing CLI entry point, add `stress-test` subcommand via click/typer)
- **Phase 3**: ElectronicEye strategy calls `run_stress_test()` every 60 seconds internally; worst-case result drives Guardian (no separate service)
- Prometheus: `hft_stress_pnl_ntd{scenario}` gauge (published by strategy via callback, see §3.6)

---

### 3.5 Live Adapter — Float-to-Int Boundary

#### `options/live_adapter.py` (~60 LOC)

```python
class OptionsLiveAdapter:
    """Bridges float analytics (options/) to live trading path.

    Implements GreeksProvider protocol for RiskEngine.
    All outputs to risk/strategy are int or bool — never float.
    Float values go to Prometheus only (observability, not decision).
    """

    def __init__(self, pricing_fn, surface: VolSurface, position_store):
        ...

    # GreeksProvider protocol
    def current_portfolio_greeks(self) -> AggregatedGreeks: ...
    def simulated_greeks_after(self, intent: Any) -> AggregatedGreeks: ...

    # Hedger support (Phase 3)
    def compute_hedge_lots(self) -> int:
        """Round net_delta to nearest integer. Zero if below threshold."""

    # Guardian support (Phase 3)
    def check_limits(self) -> tuple[bool, str]:
        """Returns (within_limits, reason). Bool output only."""

    def run_stress(self, scenarios: list[ScenarioConfig]) -> tuple[bool, float]:
        """Returns (within_limits, worst_pnl_ntd). worst_pnl_ntd for Prometheus only."""
```

---

### 3.6 Strategy-Side Publish Capability (New Infrastructure)

#### Problem

`StrategyContext` (`strategy/base.py:20-35`) is read-only with `__slots__` containing only data-access callables. No Redis, no publish, no side-channel capability. Redis publisher exists only in `MarketDataService` (`market_data.py:408`), publishing per-symbol market snapshots.

Putting Redis SET directly in strategy `handle_event()` violates the Allocator Law (hot path) and Async Law (blocking I/O).

#### Design

Add an **async publish callback** to `StrategyContext` that offloads to a bounded queue drained by a separate coroutine. The strategy never touches Redis directly.

**`strategy/base.py` — New slot + method** (~15 LOC):

```python
class StrategyContext:
    __slots__ = (
        ...existing slots...,
        "_publish_sink",  # NEW: Callable[[str, dict], None] | None
    )

    def publish_state(self, channel: str, payload: dict) -> None:
        """Non-blocking publish to monitoring. Drops on backpressure. Never blocks hot path."""
        if self._publish_sink is not None:
            try:
                self._publish_sink(channel, payload)
            except Exception:
                pass  # drop silently — monitoring is best-effort
```

**`services/bootstrap.py` — Wire publish sink** (~20 LOC):

1. Create bounded `asyncio.Queue[tuple[str, dict]](maxsize=64)` for strategy state publish
2. Pass `queue.put_nowait` as `_publish_sink` to `StrategyContext`
3. Start a `_strategy_state_publisher` coroutine that drains the queue and writes to Redis (or ClickHouse, or Prometheus — configurable)

The publisher coroutine runs in the event loop but is **not** on the hot path — it drains a bounded queue at its own pace. `put_nowait` in the strategy raises `QueueFull` which `publish_state()` catches and drops.

**Redis schema** for portfolio Greeks (consumed by Monitor panel):

```python
# Key: monitor:portfolio:greeks (SET with 10s TTL)
# Published by _strategy_state_publisher coroutine, NOT by strategy directly
{
    "ts": int,                    # timestamp_ns
    "net_delta_lots": float,      # for display only
    "net_gamma_lots": float,
    "net_theta_ntd": float,
    "net_vega_ntd": float,
    "worst_pnl_ntd": float,
    "eye_state": str,             # "QUOTING"|"NARROW"|"RESTRICT"|"HALT"
    "positions": [{"symbol": str, "qty": int, "delta": float, "iv": float}, ...]
}
```

**Note**: Floats in Redis payload are for **display only** (Monitor TUI). They never re-enter the trading path.

---

### 3.7 Trigger Executor — Strategy-Side Helper

#### Current State (Verified)

- `MarketDataService` has **no pre-strategy hook point** — it publishes to `RingBufferBus`, strategy runners subscribe independently
- Architecture Rule 25 §7: `services` orchestrates but does not own domain logic

#### `execution/trigger_executor.py` (~60 LOC)

```python
class TriggerExecutor:
    """Strategy-side helper for price-triggered order firing.

    Called by strategy.handle_event() on every tick — NOT mounted in MarketDataService.
    Latency is bounded by strategy loop frequency (same as current CBS stop-loss).
    """

    def register(self, symbol: str, condition: TriggerCondition, intent: OrderIntent) -> str:
        """Register trigger. Returns trigger_id. Max 100 active triggers."""

    def cancel(self, trigger_id: str) -> bool:
        """Cancel pending trigger."""

    def on_tick(self, symbol: str, price: int) -> list[OrderIntent]:
        """Check triggers against price. Returns fired intents (one-shot, auto-removed)."""
```

Design decisions:
- `TriggerCondition`: `GE(threshold)` or `LE(threshold)`
- Bounded: max 100 active triggers
- One-shot: trigger fires once then auto-removes
- Non-persistent: lost on restart; strategy rebuilds
- CBS integration: ~10 LOC in `cascade_bounce.py` to delegate stop-loss to `TriggerExecutor.on_tick()` within `handle_event()`
- **No latency improvement** over current CBS — same strategy loop. Benefit is reusability and separation of trigger logic.

---

### 3.8 Monitor Greeks Panel

#### Changes

**`monitor/_redis_publish.py` — New poller** (~30 LOC):

New method to poll `monitor:portfolio:greeks` key (separate from per-symbol market data path). 1-second poll interval. Returns `PortfolioGreeksSnapshot` dataclass.

**Monitor TUI panel** (~50 LOC):

```
┌─ Portfolio Greeks ──────────────┐
│ Net Δ:  +12.3 lots  (lim: 50)  │
│ Net Γ:   +3.1 lots  (lim: 20)  │
│ Net Θ:  -45,200 NTD            │
│ Net V:  +82,000 NTD            │
│ Worst PnL: -156,000 NTD        │
│ State: QUOTING                  │
└─────────────────────────────────┘
```

New `PortfolioGreeksSnapshot` dataclass in monitor data model. Panel subscribes to the Redis key published by the Phase 3 strategy via the `_strategy_state_publisher` coroutine (§3.6).

---

## 4. Phase 3: Electronic Eye Strategy

### Goal

Consume Phase 1 (data + pricing) and Phase 2 (risk + execution helpers) to implement automated TXO market-making with delta-neutral hedging.

### Module

`strategies/electronic_eye.py` (~500 LOC) — new Strategy, implements `handle_event()` interface for `StrategyRunner`.

### Strategy Registration

Registered in `config/base/strategies.yaml` (single flat file, `strategy/registry.py:29`):

```yaml
strategies:
  - id: electronic_eye
    module: hft_platform.strategies.electronic_eye
    class: ElectronicEye
    enabled: false              # start disabled, enable for shadow
    product_type: OPT
    symbol_tags: [options, txo]
    budget_us: 500              # allow more compute than typical strategy
    params:
      quoter:
        min_edge_ticks: 2
        max_contracts_per_strike: 5
        quote_strikes: atm_pm3
        quote_types: [C, P]
        refresh_interval_ms: 500
        cancel_on_stale_ms: 2000
      hedger:
        hedge_instrument: TXFR1
        delta_threshold_lots: 3
        hedge_order_type: MKT     # uses new OrderIntent.price_type field
        hedge_tif: IOC
        hedge_cooldown_ms: 1000
        max_hedge_qty_per_order: 10
      guardian:
        warn_utilization_pct: 80
        stress_interval_s: 60
        max_worst_case_pnl_ntd: -500000
      publish:
        channel: "monitor:portfolio:greeks"
        interval_ms: 1000
```

All params loaded via `StrategyConfig.params` dict (existing mechanism, `registry.py:51`).

### Sub-Engines

#### 4.1 Quoter (Auto-Quoting)

```
BidAsk update → recalc theo price from VolSurface → compare to market → quote if edge exists
```

Logic:
1. `theo = black76_price(F, K, T, σ_surface, r)` — float computation inside strategy
2. `bid_scaled = int((theo - min_edge_ticks * tick_size) * price_scale)` — convert to scaled int
3. `ask_scaled = int((theo + min_edge_ticks * tick_size) * price_scale)` — convert to scaled int
4. Emit `OrderIntent(price=bid_scaled, price_type="LMT", tif=TIF.ROD, ...)` — all int fields
5. Existing order deviated > 1 tick → emit `OrderIntent(intent_type=AMEND)`
6. Edge disappeared → emit `OrderIntent(intent_type=CANCEL)`
7. IV stale > `cancel_on_stale_ms` → cancel all quotes for that strike

**Price conversion happens inside ElectronicEye** before `OrderIntent` construction. No float in intent.

#### 4.2 Hedger (Auto-Hedging)

```
FillEvent (options) → recalc portfolio Greeks → delta exceeds threshold → hedge with futures
```

Logic:
1. Receive `FillEvent` via existing strategy callback (`on_fill` or `handle_event` with fill)
2. `hedge_lots = adapter.compute_hedge_lots()` → `int` (from live adapter, already rounded)
3. `|hedge_lots| > delta_threshold` → emit `OrderIntent`
4. `OrderIntent(price=0, price_type="MKT", tif=TIF.IOC, ...)` — MKT+IOC for immediate fill
5. Direction: hedge_lots > 0 → sell futures; hedge_lots < 0 → buy futures
6. Single-leg futures `OrderIntent` (standard path)
7. Cooldown: ignore hedge signals within `hedge_cooldown_ms` of last hedge

`price_type="MKT"` uses the new `OrderIntent.price_type` field (Phase 2 §3.2). `price=0` signals market order (adapter handles this).

#### 4.3 Guardian (Risk State Machine)

Owned by the strategy. Driven by two inputs:
- **`on_risk_feedback()`**: Overrides `BaseStrategy.on_risk_feedback()` (Phase 2 §3.1). When RiskEngine rejects an intent with `GREEKS_*` reason, Guardian escalates.
- **`adapter.run_stress()`**: Called every `stress_interval_s`. Returns `(within_limits, worst_pnl_ntd)`.

Three-level escalation:

| Level | Condition | Action |
|-------|-----------|--------|
| L1 WARN | `adapter.check_limits()` reports utilization > `warn_utilization_pct` | Narrow `quote_strikes` to ATM ± 1 |
| L2 RESTRICT | `on_risk_feedback()` receives `GREEKS_*` rejection OR stress breach | Quoter cancels all, only CANCEL intents allowed |
| L3 HALT | StormGuard HALT detected (strategy reads StormGuard state via context) OR no market data for 30s | Cancel all pending + emit flatten intents (MKT+IOC per-leg) |

State machine:

```
INIT → QUOTING ⇄ NARROW (L1) ⇄ RESTRICT (L2) → HALT (L3)
```

Borrows StormGuard FSM **pattern** (enum states + transition rules), separate instance owned by strategy. Does not share state with system StormGuard.

#### 4.4 Monitoring Publish

Every `publish.interval_ms`, strategy calls `ctx.publish_state(channel, payload)` (Phase 2 §3.6). This enqueues to the bounded queue, drained by `_strategy_state_publisher` coroutine, which writes to Redis. Strategy never touches Redis.

### Deployment Path

```
Shadow (sim account, enabled=true in strategies.yaml, HFT_ORDER_MODE=sim)
  Validate: Quoter pricing reasonable, Hedger triggers correctly, Guardian limits effective
  Metrics: theoretical P&L, hedge frequency, cancel rate
  Duration: minimum 10 trading days

Canary (live account, max_contracts_per_strike=1)
  Validate: actual fill vs theo price deviation, slippage, hedge latency
  Gate: 5 trading days positive P&L, max drawdown < config
  Duration: minimum 5 trading days

Live (gradual scale-up)
  max_contracts_per_strike incremented via strategies.yaml edit + restart
  Rollback: set enabled=false → strategy cancels all on next tick cycle
```

**Deployment does NOT use `alpha/canary.py`**. That pipeline is designed for alpha_id/weight-based promotion (`canary.py:20-51`) and doesn't fit a strategy that manages its own position lifecycle. We use the **strategy registry enable/disable pattern** (same as CBS) with manual config changes.

### Integration Map

```
MarketDataService → RingBufferBus → StrategyRunner
                                      └── ElectronicEye.handle_event()
                                            ├── Quoter
                                            │   └── options/pricing (float) → int conversion
                                            │       → OrderIntent(price=int, price_type="LMT")
                                            ├── Hedger
                                            │   └── options/live_adapter → compute_hedge_lots() → int
                                            │       → OrderIntent(price=0, price_type="MKT", tif=IOC)
                                            ├── Guardian
                                            │   └── on_risk_feedback() ← RiskEngine rejection queue
                                            │       live_adapter.run_stress() → bool
                                            │       → state transitions → cancel/flatten intents
                                            └── ctx.publish_state() → queue → Redis (async, off hot path)

OrderIntent → RiskEngine
               ├── FastGate (Rust, step 0)
               ├── StormGuard (step 1)
               ├── Rust/Python validators (step 2)
               └── GreeksLimitValidator (step 3, post-validator, new)
               → OrderCommand → OrderGateway → place_order (single-leg only)
                                                  ↕ price_type from OrderIntent field
```

New: 1 strategy file. StrategyRunner framework: zero changes. StrategyContext: +1 slot.

---

## 5. Cross-Cutting Concerns

### 5.1 Configuration Files

| File | Phase | Purpose |
|------|-------|---------|
| `config/symbols.list` (existing, active) | 1 | TXO entry already present, no change |
| `config/base/symbols.yaml` (auto-generated, manual entries to be removed) | 1 | TXO strikes auto-expanded with full metadata |
| `config/base/risk_greeks.yaml` (new) | 2 | Greeks limits (disabled by default) |
| `config/base/stress_scenarios.yaml` (new) | 2 | Stress test scenarios |
| `config/base/strategies.yaml` (existing, append) | 3 | ElectronicEye strategy entry |

### 5.2 New Files

| File | Phase | LOC |
|------|-------|-----|
| `src/hft_platform/options/__init__.py` | 1 | 5 |
| `src/hft_platform/options/pricing.py` | 1 | 200 |
| `src/hft_platform/options/greeks.py` | 1 | 150 |
| `src/hft_platform/options/surface.py` | 1 | 200 |
| `src/hft_platform/options/live_adapter.py` | 2 | 60 |
| `src/hft_platform/risk/greeks_limit_validator.py` | 2 | 80 |
| `src/hft_platform/risk/stress_test.py` | 2 | 150 |
| `src/hft_platform/execution/trigger_executor.py` | 2 | 60 |
| `src/hft_platform/strategies/electronic_eye.py` | 3 | 500 |

### 5.3 Modified Files

| File | Phase | Change | LOC |
|------|-------|--------|-----|
| `config/_symbols_expansion.py` | 1 | Harden `build_entry()` for options metadata | ~40 |
| `feed_adapter/shioaji/contracts_runtime.py` | 1 | R1/R2 alias in `_get_contract()` | ~10 |
| `contracts/strategy.py` | 2 | Add `price_type` field + `RiskFeedback` dataclass | ~15 |
| `risk/engine.py` | 2 | Add `greeks_provider` param, post-validator check, rejection sink | ~40 |
| `order/adapter.py` | 2 | Prefer `intent.price_type` over metadata default | ~5 |
| `strategy/base.py` | 2 | Add `_publish_sink` slot, `publish_state()`, `on_risk_feedback()` | ~20 |
| `services/bootstrap.py` | 2 | Wire rejection queue, publish queue, GreeksProvider | ~30 |
| `strategies/cascade_bounce.py` | 2 | Delegate stop-loss to TriggerExecutor | ~10 |
| `monitor/_redis_publish.py` | 2 | Portfolio Greeks poller (new key) | ~30 |
| `monitor/` (TUI panel) | 2 | Greeks panel + PortfolioGreeksSnapshot model | ~50 |
| `config/base/strategies.yaml` | 3 | Append ElectronicEye entry | ~25 |

### 5.4 Prometheus Metrics (New)

| Metric | Phase | Type |
|--------|-------|------|
| `hft_txo_subscriptions_active` | 1 | Gauge |
| `hft_options_iv_solve_ns` | 1 | Histogram |
| `hft_greeks_utilization{greek}` | 2 | Gauge |
| `hft_stress_pnl_ntd{scenario}` | 2 | Gauge |
| `hft_risk_rejection_feedback_total{reason}` | 2 | Counter |
| `hft_trigger_fired_total` | 2 | Counter |
| `hft_eye_state` | 3 | Gauge (enum) |
| `hft_eye_theo_edge_ticks` | 3 | Histogram |
| `hft_eye_hedge_latency_ms` | 3 | Histogram |
| `hft_eye_hedge_lots` | 3 | Gauge |

### 5.5 Testing Strategy

Each phase includes:
- Unit tests for new modules (≥80% coverage per Rule 50)
- Integration test with simulation mode where applicable
- Phase 1: cross-validation against TAIFEX settlement IV
- Phase 2: RiskEngine with GreeksLimitValidator — rejection flow end-to-end
- Phase 3: Shadow deployment with sim account for 10 trading days minimum

### 5.6 No New External Dependencies

All required libraries already in project:
- `numpy` — array math
- `scipy` — optimization (brentq)
- `structlog` — logging
- `prometheus_client` — metrics

---

## 6. Architecture Governance Compliance

| Rule | Compliance |
|------|-----------|
| Rule 25 §11 (Float exception) | `options/` added to exemption list alongside `alpha/`, `research/`. `live_adapter.py` is the firewall. |
| Rule 25 §7 (Services don't own domain logic) | TriggerExecutor is strategy-side. Publish is via callback. |
| Rule 01 §1 (Allocator Law) | `publish_state()` uses `put_nowait` + drop on full. No allocation in hot path. |
| Rule 01 §3 (Async Law) | Redis writes happen in separate coroutine, not in strategy `handle_event()`. |
| Rule 25 §4 (Precision Law) | `OrderIntent.price` remains `int`. New `price_type` field is `str` (enum-like). No float in intent. |
| Rule 20 (Data Flow) | Rejection feedback via bounded async queue with drop policy. |

---

## 7. Future Work (Post This Spec)

| Item | Trigger | Separate Spec |
|------|---------|---------------|
| Combo Orders infrastructure | When ElectronicEye needs multi-leg atomic execution | Yes |
| SVI/SABR surface fitting | When grid-based surface proves insufficient | No — `surface.py` enhancement |
| Rust IV kernel | When 500ms refresh proves too slow | No — drop-in replacement |
| FeatureEngine IV slot | When IV becomes shared feature for other strategies | No — new feature provider |
| Historical IV database | When research needs term structure backtesting | Yes |
| QuoteConnectionPool integration | When TXO subscription count exceeds single connection limit | See `quote-connection-pool-design.md` |
