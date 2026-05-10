---
name: hft-mm-design
description: Use when designing or modifying market-making strategies (quoting, inventory management, spread gating, adverse selection filters). Encodes R47 three-layer pattern and structural properties validated on TAIFEX.
---

# HFT Market-Making Design

Design patterns for TAIFEX market-making strategies. Based on R47 Maker (the only profitable strategy, +4,534 pts / 12 days) and structural analysis of what improvements help vs kill profitability.

## R47 Three-Layer Architecture

```
L1: Spread Gate       → Only quote when spread ≥ threshold (cost viability)
L2: Signal Layers     → D1-D4 decision gates (suppress/widen/skew)
L3: Execution Layer   → Price snapping, pending tracking, gap resilience
```

### L1: Spread Gate (Non-Negotiable)

```python
def _should_quote(self, spread_scaled: int) -> bool:
    # TMFD6 breakeven = 4 pts. Only quote when profitable.
    return spread_scaled >= self.spread_threshold_pts * PRICE_SCALE

# spread_threshold_pts: POINTS not bps
# TMFD6 default: 5 (= 50,000 scaled)
# TXFD6 default: 3 (= 30,000 scaled)
```

**Why this is the only gate that matters**: Every profitable session correlates with average spread. PnL ∝ avg_spread. Time-of-day and volume effects are entirely explained by spread regime.

### L2: Signal Layers (D1-D4)

#### D1: Permutation Entropy Gate (Regime Filter)

```python
# Block quoting in trending markets (low entropy)
# H (normalized PE) ranges 0-1; low H = trending, high H = random
if h < pe_danger_threshold:  # default: 0.0 (disabled — produces best results)
    return  # Don't quote

# CRITICAL FINDING: Setting PE threshold > 0 is net-negative
# Kills V-shape recovery days where profits come from surviving the trend
```

#### D2: Queue Survival Suppression (Adverse Selection)

```python
# M/M/1 gambler's ruin: probability near-side queue depletes before far-side
# If high probability, suppress that side's quote
if p_depletion_bid > queue_cancel_threshold:  # default: 1.0 (disabled)
    suppress_bid = True

# CRITICAL FINDING: PowerProb model is 14x too pessimistic
# Disabling (threshold=1.0) produces best results
# p_depl > 1.0 is never true → effectively disabled
```

#### D3: MFG Inventory Skew (Flow-Based Widening)

```python
# Cumulative signed flow → detect capitulation (one-sided pressure)
# Widen the side under pressure
if mfg_capitulation_z > mfg_skew_z_threshold:  # default: 100 (disabled)
    if flow_direction > 0:
        widen_ask += skew_mult * tick_size
    else:
        widen_bid += skew_mult * tick_size

# CRITICAL FINDING: MFG never triggers is correct
# Academic inventory models don't apply to 1-lot CLOB
# Fixed skew (0.2 ticks/contract) outperforms Avellaneda-Stoikov
```

#### D4: QI Adverse-Selection Skew

```python
# L1 imbalance-based spread asymmetry
# When buying pressure detected, widen ask (protect short side)
qi = (bid_depth - ask_depth) / (bid_depth + ask_depth)
if abs(qi) > qi_skew_threshold:  # default: 0.10
    if qi > qi_skew_threshold:
        widen_ask += qi_widen_ticks * tick_size
    elif qi < -qi_skew_threshold:
        widen_bid += qi_widen_ticks * tick_size

# This is the ONE signal layer that helps (marginally)
# Keep threshold high (0.10) — lower thresholds over-trigger
```

### L3: Execution Layer

See `hft-strategy-sdk` skill for: pending tracking, price-movement gate, tick grid snapping, gap resilience, risk feedback handling.

## Structural Properties (Validated, Non-Negotiable)

These properties were validated by systematic ablation on R47. Violating them kills profitability:

### 1. max_pos ≥ 3 is Non-Linearly Essential

```
max_pos=1 → -1,407 pts (losing)
max_pos=2 → -557 pts (losing)
max_pos=3 → +4,534 pts (profitable!)
max_pos=5 → +4,899 pts (marginal improvement)
```

**Why**: When market moves against you, positions 2-3 are "dollar-cost averaging" that enables V-shape recovery. Position 1 alone gets stopped out before recovery.

### 2. V-Shape Recovery is the Core Mechanism

```
Typical winning day pattern:
  09:00-11:00: -1,500 to -2,250 pts drawdown
  11:00-13:30: Recovery to +500 pts net positive

The profit comes from SURVIVING adverse swings, not avoiding them.
Any circuit breaker that cuts losses at -500 to -1,500 pts is net-negative.
```

### 3. Minimal Inventory Skew is Optimal

```
Fixed skew: 0.2 ticks per contract → BEST
GLT skew: calibrated → WORSE
A-S skew: theoretical → WORSE

Why: TMFD6 is CLOB at 1-pt tick. You compete at the tick level.
Academic frameworks assume dealer markets with continuous quotes.
73% of trades are qty=1 — skew at this scale is noise.
```

### 4. Fresh Quotes Beat Stale Quotes

```
Stale quote suppression (preserving queue priority): 0/12 days improved
Fresh order replacement: default behavior, BEST

Why: Queue position value < information value of updated price.
Market moves while you sit in queue → adverse selection.
```

## MM Economics Formula

```
Profit per RT = half_spread - adverse_selection - cost

TMFD6 at spread=5 pts:
  half_spread = 2.5 pts
  adverse_selection ≈ 1.6 pts (empirical)
  cost = 4.0 pts RT → 2.0 pts per side
  net = 2.5 - 1.6 - 2.0 = -1.1 pts (single trade)

But position accumulation (max_pos=3) changes the math:
  Average entry across 3 positions is better than single entry
  V-shape recovery converts paper loss to realized profit
  Net over 12 days: +4,534 pts
```

## Configuration Template

```yaml
# New MM strategy config (conservative defaults)
- id: "MY_MM_STRATEGY"
  module: "hft_platform.strategies.my_mm"
  class: "MyMMStrategy"
  enabled: true
  product_type: "FUT"
  symbols: ["TMFD6"]
  params:
    # L1: Spread gate (CRITICAL)
    spread_threshold_pts: 5        # Must be > RT cost (4 pts for TMFD6)

    # L2: Signal layers (start disabled, enable one at a time)
    pe_danger_threshold: 0.0       # 0.0 = disabled (recommended)
    queue_cancel_threshold: 1.0    # 1.0 = disabled (recommended)
    mfg_skew_z_threshold: 100      # 100 = never triggers (recommended)
    qi_skew_threshold: 0.10        # Only useful signal layer
    qi_widen_ticks: 1

    # L3: Position management
    max_pos: 3                     # Non-negotiable minimum for profitability
    inventory_skew_ticks: 0.2      # Fixed skew per contract

    # Safety
    max_daily_loss_pts: 0          # 0 = no circuit breaker (recommended)
```

## Design Process for New MM Strategy

1. **Start with spread gate only** (all signal layers disabled)
2. **Backtest with CK direct** (not hftbacktest alone)
3. **Enable ONE signal layer at a time**, measure incremental PnL
4. **If layer doesn't improve**: disable it permanently (default disabled)
5. **Shadow trade** for ≥ 1 full session before live
6. **Start with max_pos=1** in live, increase to 3 after 3 stable days

## Improvements That KILL Profitability

| "Improvement" | What happens | Why |
|---------------|-------------|-----|
| Add circuit breaker (stop at -N pts) | Cuts V-shape recovery | False positives on winning days dominate |
| Reduce max_pos to 1 | Turns +4,534 to -1,407 | Can't dollar-cost average through adverse swings |
| Add Hawkes intensity gate | Kills trending-day profits | Trending days are high-spread = high-profit days |
| Use GLT/A-S inventory model | Worse than fixed 0.2 | Academic models don't fit 1-lot CLOB |
| Preserve stale quotes (queue priority) | 0/12 days improved | Information decay > queue position value |
| Vol-adaptive spread narrowing | spread=3 < cost=4 | Narrowing below cost is structural loss |

## Anti-Patterns

- Do NOT add signal layers without CK-validated incremental PnL evidence
- Do NOT assume "smarter inventory" = better — minimal skew is optimal on CLOB
- Do NOT add circuit breakers without V-shape recovery analysis
- Do NOT test MM strategies with hftbacktest default — use CK direct
- Do NOT start live with max_pos > 1 on day 1 (ramp up over 3+ days)
- Do NOT use bps for spread thresholds — use points
