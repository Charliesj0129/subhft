# strategies — Core Strategy Implementations

> **Package**: `src/hft_platform/strategies/`
> **Runtime Plane**: Decision

## Overview

7 core strategy implementations plus 5 alpha strategies. All extend `BaseStrategy` from the strategy SDK.

## Core Strategies

| Strategy | File | Description |
|----------|------|-------------|
| `SimpleMM` | `simple_mm.py` | Simple market-making with spread quoting |
| `CascadeBounce` | `cascade_bounce.py` | Multi-level bounce detection with cascade entry |
| `MomentumBounce` | `momentum_bounce.py` | Momentum-based bounce trading |
| `OpportunisticMM` | `opportunistic_mm.py` | Opportunistic market-making (spread capture) |
| `ElectronicEye` | `electronic_eye.py` | TXO options MM (Guardian/Quoter/Hedger pattern) |
| `VPINRegimeSwitch` | `vpin_regime_switch.py` | VPIN-based regime detection and switching |
| `RustAlpha` | `rust_alpha.py` | Rust-native strategy executor bridge |

## Alpha Strategies (in `alpha/` subdir)

| Strategy | Description |
|----------|-------------|
| `AlphaOFI` | Order flow imbalance signal |
| `AlphaHawkes` | Hawkes process-based |
| `AlphaDeepHawkes` | Deep Hawkes variant |
| `AlphaMHP` | Multi-horizon prediction |
| `AlphaPropagator` | Price propagation model |

## Strategy Configuration

Strategies are configured in `config/base/strategies.yaml`:

```yaml
strategies:
  - id: cascade_bounce_txfd6
    module: hft_platform.strategies.cascade_bounce
    class: CascadeBounce
    enabled: true
    budget_us: 200
    symbols: [TXFD6]
    product_type: FUTURES
    params:
      bounce_threshold: 3
      cascade_levels: 5
```

## Development Guide

See [Strategy Guide](../guides/strategy-guide.md) for development workflow.

All strategies must:
1. Extend `BaseStrategy`
2. Use scaled integers for prices (Precision Law)
3. Use `self.buy()` / `self.sell()` / `self.cancel()` for orders
4. Handle `on_gap()` to reset stale state
5. Include unit tests (`tests/unit/test_<strategy>.py`)
