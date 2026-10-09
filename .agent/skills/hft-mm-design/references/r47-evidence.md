# R47 evidence (historical)

Read to understand where the design rules in `SKILL.md` came from. These figures
come from the April 2026 R47 ablations on a specific backtest method and a
12-day window; later review found backtest methods disagree by orders of
magnitude, the cost model was corrected afterwards (round-trip cost and the
tax on both sides), and the live loop runs `max_pos=1`. Use the numbers as
hypotheses, never as settings or as proof. Sources: `research/alphas/r47_maker_pivot/manifest.yaml`,
`docs/incidents/2026-04-24-r47-backtest-credibility-audit.md`,
`docs/runbooks/backtest-engine-selection.md`.

## Structural properties (as observed then)

Observed by ablation on R47 in that window:

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

