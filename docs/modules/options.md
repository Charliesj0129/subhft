# options — Greeks & Pricing

> **Package**: `src/hft_platform/options/`
> **Files**: 4

## Overview

Black-Scholes pricing, Greeks computation, implied volatility surface, and live option quote adapter.

## Files

| File | Key Exports | Purpose |
|------|-------------|---------|
| `greeks.py` | `GreeksCalculator` | Delta, gamma, theta, vega, rho |
| `pricer.py` | `OptionsPricer` | Black-Scholes pricing |
| `vol_surface.py` | `VolatilitySurface` | IV surface construction |
| `live_adapter.py` | `OptionsLiveAdapter` | Live option quote integration |

## Usage

```python
calculator = GreeksCalculator()
greeks = calculator.compute(
    spot=19500_0000,        # Scaled int
    strike=19000_0000,      # Scaled int
    days_to_expiry=30,
    iv=0.20,
    right="C"
)
```

Used by:
- `GreeksLimitValidator` (risk module) for portfolio Greeks limits
- `ElectronicEye` strategy for hedging decisions
- Monitor TUI for Greeks panel display
