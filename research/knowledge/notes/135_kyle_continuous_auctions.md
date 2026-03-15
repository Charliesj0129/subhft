# 135 — Continuous Auctions and Insider Trading

**Authors**: Albert S. Kyle
**Year**: 1985
**Journal**: Econometrica 53(6), 1315-1335

## Core Contribution

The foundational model of informed trading in continuous-time markets. Introduces Kyle's
lambda (λ), the price impact coefficient measuring information content per unit of signed
order flow.

## Key Concepts

### Market Structure
- One informed trader (insider) with private signal about asset value
- Noise (liquidity) traders with exogenous order flow
- Market maker who sets prices = expected value conditional on total order flow

### Kyle's Lambda

```
λ = Cov(ΔP, signed_volume) / Var(signed_volume)
```

Lambda measures adverse selection: higher λ → more information per unit of signed volume →
more toxic flow environment for market makers.

### Properties
- λ is constant in the basic model but varies intraday in extensions (Hasbrouck 2009)
- Higher λ for less liquid stocks (more information asymmetry)
- λ increases before earnings announcements and other information events
- Can be estimated as rolling regression coefficient from tick data

## Relevance to HFT Platform

- Kyle's lambda directly measures adverse selection intensity
- Rising lambda → informed traders active → toxic flow regime
- Can be computed O(1) per tick with exponential-weighted online covariance
- Distinct from spread-based or imbalance-based toxicity measures

## Alpha: `kyle_lambda`

Data fields: `mid_price`, `volume`, `bid_qty`, `ask_qty` (for tick-rule trade signing)
Complexity: O(1) per tick (Welford-style EMA covariance)
