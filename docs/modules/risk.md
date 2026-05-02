# risk — Risk Engine & StormGuard

> **Package**: `src/hft_platform/risk/`
> **Runtime Plane**: Decision
> **Hot-Path**: `RiskEngine.evaluate()`, `FastGate`

## Overview

Synchronous validation chain with 5 validators, StormGuard FSM (circuit breaker), Greeks limit validation, and liquidity gate. Supports Rust FFI fast-path and hot-reload via SIGHUP.

## Files (10)

| File | Key Exports | Purpose |
|------|-------------|---------|
| `engine.py` | `RiskEngine` | Synchronous validator chain orchestrator |
| `storm_guard.py` | `StormGuard` | NORMAL→WARM→HALT FSM (circuit breaker) |
| `validators.py` | `PriceBandValidator`, `MaxNotionalValidator`, `PositionLimitValidator`, `DailyLossValidator` | 4 core risk validators |
| `greeks_limit.py` | `GreeksLimitValidator` | Options Greeks exposure limits |
| `liquidity_gate.py` | `LiquidityGate` | Spread/depth-based order rejection |
| `fast_gate.py` | `FastGate` | Numba-accelerated shared-memory risk check |
| `halt_flattener.py` | `HaltFlattener` | Auto-cancel and flatten on HALT |
| `drift_burst_detector.py` | `DriftBurstDetector` | Price drift detection for risk |
| `_rust_accel.py` | — | Rust FFI integration helpers |
| `__init__.py` | — | Package exports |

## RiskEngine

```python
engine = RiskEngine(validators=[...], storm_guard=sg)
decision = engine.evaluate(intent)  # Returns RiskDecision (synchronous)
command = engine.create_command(intent)  # Returns OrderCommand
```

### Validator Chain

| Validator | Check | Rejection Reason |
|-----------|-------|-----------------|
| `PriceBandValidator` | Price within band from reference | `PRICE_BAND` |
| `MaxNotionalValidator` | Order notional < limit | `MAX_NOTIONAL` |
| `PositionLimitValidator` | Position within per-symbol limit | `POSITION_LIMIT` |
| `DailyLossValidator` | Daily realized PnL > loss limit | `DAILY_LOSS` |
| `GreeksLimitValidator` | Delta/gamma/vega within limits | `GREEKS_LIMIT` |

All validators return `(approved: bool, reason: str)`. Chain short-circuits on first rejection.

### Hot-Reload

- Strategy limits hot-reloadable via SIGHUP
- Config: `config/base/strategy_limits.yaml`
- No restart required

## StormGuard FSM

```
NORMAL → WARM → STORM → HALT
  ↑                        │
  └────── manual rearm ────┘
```

| State | Behavior |
|-------|----------|
| NORMAL | All orders allowed |
| WARM | Warning logged, all orders allowed |
| STORM | DEGRADE mode (cancel-only via GatewayPolicy) |
| HALT | Block all new orders, trigger cancellation |

### Triggers
- Feed gap > `HFT_STORMGUARD_FEED_GAP_HALT_S` (30s) → HALT
- Daily loss limit → HALT
- Bus overflow cascade → HALT
- Reconciliation critical mismatch → HALT

### Halt-Exempt Strategies
- Configured per-strategy in risk config
- Allowed to trade even during HALT
- Used for R29b event momentum strategies

## LiquidityGate

Rejects orders when market liquidity is insufficient:
- Spread > threshold → reject
- Depth < minimum → reject
- Prevents adverse selection in thin markets

## FastGate

Numba-accelerated shared-memory risk check:
- Uses `multiprocessing.shared_memory` for zero-copy
- `@njit`-compiled validation kernel
- Sub-microsecond latency for simple checks

## DailyLossLimitValidator

Stateful validator tracking cumulative realized PnL per strategy + platform-wide unrealized:

- **Reset boundary**: 05:00 Taiwan (UTC+8) = 21:00 UTC previous day
- **Intraday watermark** (opt-in via `intraday_pnl` config):
  - `soft_limit_ntd`: Soft block with cooldown recovery
  - `hard_limit_ntd`: Triggers HALT
  - `peak_drawdown_pct`: % drawdown from intraday peak
  - Cooldown-guarded recovery via `soft_recovery_ntd` and `soft_limit_cooldown_s`

## DriftBurstDetector

Microstructure toxicity scoring based on Christensen, Oomen, Reno (2022):

- Test statistic: `T(t) = drift_estimate / sqrt(bpv_estimate)`
- BPV: bipower variation (jump-robust volatility estimate)
- Sigmoid toxicity score: `toxicity = 2 / (1 + exp(-|T| / threshold)) - 1`
- Pre-allocated arrays (Allocator Law compliant)
- Integrated with StormGuard via `update_with_lob()`

## Configuration

| Variable | Default | Purpose |
|----------|---------|---------|
| `HFT_STORMGUARD_FEED_GAP_STORM_S` | `1.0` | Feed gap → STORM |
| `HFT_STORMGUARD_FEED_GAP_HALT_S` | `30` | Feed gap → HALT (deprecated alias) |
| `HFT_STORMGUARD_HALT_COOLDOWN_S` | `60` | HALT recovery cooldown |
| `HFT_STORMGUARD_STORM_COOLDOWN_S` | `30` | STORM recovery cooldown |
| `HFT_STORMGUARD_DE_ESCALATE_N` | `5` | Consecutive clears before de-escalation |
| `HFT_STORMGUARD_HALT_EXEMPT_STRATEGIES` | — | Comma-separated exempt strategy IDs |
| `HFT_RISK_FAST_GATE` | `0` | Enable Numba FastGate |
| `HFT_RISK_RUST_VALIDATOR` | `0` | Enable Rust validator FFI |
| `HFT_RISK_DLQ_TTL_S` | `30` | Risk DLQ entry TTL |

## Rust FFI

Optional `RustRiskValidator`, `RustStormGuardValidator`, `RustCircuitBreaker` via `hft_platform.rust_core`.
Error codes: 0=OK, 1=PRICE_ZERO_OR_NEG, 2=PRICE_EXCEEDS_CAP, 3=PRICE_OUTSIDE_BAND, 4=MAX_NOTIONAL.
Fallback to Python implementations if unavailable.
