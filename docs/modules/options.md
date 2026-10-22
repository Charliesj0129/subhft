# options — Greeks & Pricing

> **Package**: `src/hft_platform/options/`
> **Files**: 4 (`__init__.py` plus three modules)

## Overview

Black-76 pricing and implied-volatility solving, closed-form Greeks with portfolio aggregation, and a grid-based
implied-volatility surface. Black-76 is the model for futures options such as TXO; the package does not use
Black-Scholes. It is offline analytics: its docstrings state a float exception ("Architecture Governance Rule 25
§11") for research computation, so none of it takes the scaled-int x10000 prices of the order path.

## Files

| File | Key Exports | Purpose |
|------|-------------|---------|
| `pricing.py` | `black76_price(F, K, T, sigma, r, cp)`, `solve_iv(market_price, F, K, T, r, cp, tick_size=1.0)` | Black-76 price; implied vol by Newton-Raphson with a Brent fallback |
| `greeks.py` | `compute_greeks(F, K, T, sigma, r, cp)`, `portfolio_greeks(positions, multiplier)`, `GreeksResult`, `PositionGreeks`, `AggregatedGreeks` | delta, gamma, theta (per calendar day), vega (per 1% vol), rho (per 1% rate); net Greeks across positions, theta and vega in NTD |
| `surface.py` | `VolSurface` (`update`, `get_iv`, `snapshot`, `skew_pctl25`, `butterfly_pctl25`) | grid keyed by `(expiry_date, int(strike))`; cubic spline with 4 or more strikes, linear otherwise, no extrapolation |

`cp` is `"C"` or `"P"`; `T` is in years.

## Usage

```python
from hft_platform.options import compute_greeks

greeks = compute_greeks(F=19500.0, K=19000.0, T=30 / 365, sigma=0.20, r=0.01, cp="C")
greeks.delta, greeks.vega
```

Prices here are plain floats in index points, not scaled ints.

## Used by

- `risk/stress_test.py` imports `GreeksResult`, `PositionGreeks` and `VolSurface`.
- `risk/greeks_limit_validator.py` imports `AggregatedGreeks`. `RiskEngine` builds a `GreeksLimitValidator` only when
  it is constructed with a `greeks_provider`; `services/bootstrap.py` does not pass one, so the check does not run in
  the platform as wired. The validator also allows everything when `greeks_limits.enabled` is false, when there is no
  provider, and when the provider raises (it logs `greeks_provider_error`).
- No other module under `src/` imports the package, and the monitor has no Greeks panel.
