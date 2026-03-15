# 136 — Unwinding Toxic Flow with Partial Information

**Authors**: Alexander Barzykin, Robert Boyce, Eyal Neuman
**Year**: 2024
**ArXiv**: 2407.04510

## Core Contribution

Models a central trading desk that aggregates client orders with **unobserved toxicity**
(persistent adverse directionality). The desk chooses to internalize or externalize orders,
formulated as a partially observable stochastic control problem.

## Key Concepts

### Toxicity as Hidden State
- Toxicity is modeled as a latent process (not directly observable)
- Two scenarios: **momentum toxicity** (persistent drift) and **mean-reverting toxicity**
- The desk infers toxicity through filtering from observed order flow and price movements

### Filtering Approach
1. Derive filtered dynamics of inventory and toxicity projected to observed filtration
2. Use variational approach to derive optimal trading strategy
3. Performance gap vs full information: ~0.01% in all tested scenarios

### Observable Proxies for Hidden Toxicity
- Order flow direction persistence (momentum)
- Price movement residuals after accounting for expected flow impact
- Inventory drift patterns

## Relevance to HFT Platform

- Motivates **adverse_momentum** alpha: track OFI→price regression residual as
  observable proxy for hidden toxicity state
- When returns consistently exceed OFI-predicted values → informed traders active
- The OU alpha process from Cartea (2025, paper 131) is the explicit model for the
  hidden toxicity signal; the residual approach is the filtering proxy

## Alpha: `adverse_momentum`

Data fields: `mid_price`, `ofi_l1_ema8`, `spread_scaled`
Complexity: O(1) per tick (rolling regression via EMA)
