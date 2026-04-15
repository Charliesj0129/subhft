# hftbacktest Queue Position Models — Complete Reference

Source: `hftbacktest/src/backtest/models/queue.rs` (v2.4.3) + official tutorials

## Core Concept

When you place a limit order, it enters a queue at that price level. The queue model estimates HOW MANY contracts are ahead of you, and HOW your position changes as the book updates.

```
Order placed → queue_ahead = book_qty * queue_fraction_assumption
Trade at your price → queue_ahead -= trade_volume
Depth change → queue_ahead adjusted by probability model
queue_ahead <= 0 → ORDER FILLS
```

## QueueModel Trait (Rust)

```rust
pub trait QueueModel<MD> {
    fn new_order(&self, order: &mut Order, depth: &MD);
    fn trade(&self, order: &mut Order, qty: f64, depth: &MD);
    fn depth(&self, order: &mut Order, prev_qty: f64, new_qty: f64, depth: &MD);
    fn is_filled(&self, order: &Order, depth: &MD) -> f64;
}
```

## Model 1: RiskAdverseQueueModel

**Python**: `asset.risk_adverse_queue_model()`

**Mechanism**: Most conservative. Queue position advances ONLY when trades execute at your price. Depth changes (cancellations) are assumed to happen BEHIND you.

```
new_order:  front_q = book_qty_at_price
trade:      front_q -= trade_qty
depth:      front_q = min(front_q, new_qty)  # never increases
is_filled:  front_q < 0 → fill
```

**Bias**: Overestimates queue wait. Your order rarely fills. Good for worst-case analysis.

## Model 2: ProbQueueModel (Recommended)

**Python**: `asset.power_prob_queue_model(n)` / `asset.log_prob_queue_model()` etc.

**Mechanism**: When depth decreases, your position advances by a PROBABILISTIC amount. The probability depends on how much of the queue is ahead vs behind you.

### Internal State

```rust
struct QueuePos {
    front_q_qty: f64,     // quantity ahead of your order
    cum_trade_qty: f64,   // cumulative trades at this price since last depth update
}
```

### Algorithm (depth change)

When book quantity at your price changes from `prev_qty` to `new_qty`:

```
1. actual_change = (prev_qty - new_qty) - cum_trade_qty
   // Subtract trades already counted to avoid double-counting

2. Reset cum_trade_qty = 0

3. If quantity INCREASED (actual_change < 0):
   front_q = min(front_q, new_qty)
   // New orders go behind you

4. If quantity DECREASED (actual_change >= 0):
   back = prev_qty - front_q   // quantity behind you
   prob = probability_fn(front_q, back)
   // prob = probability that a cancelled order was BEHIND you

   front_q = front_q - (1 - prob) * actual_change
             + min(0, back - prob * actual_change)
   front_q = min(front_q, new_qty)
```

**Intuition**: If you're near the FRONT of the queue (small front_q), most cancellations are behind you (high prob), so your position barely changes. If you're at the BACK, cancellations are likely ahead of you (low prob), advancing your position more.

### Probability Functions

All functions map `(front, back) → prob ∈ [0, 1]`:

| Function | Python Method | Formula | Parameter |
|----------|--------------|---------|-----------|
| PowerProbQueueFunc | `power_prob_queue_model(n)` | back^n / (back^n + front^n) | n (power) |
| PowerProbQueueFunc2 | `power_prob_queue_model2(n)` | back^n / (back + front)^n | n (power) |
| PowerProbQueueFunc3 | `power_prob_queue_model3(n)` | back^n / (back^n + front^n) | n (power, same as Func1) |
| LogProbQueueFunc | `log_prob_queue_model()` | log(1+back) / (log(1+back) + log(1+front)) | none |
| LogProbQueueFunc2 | `log_prob_queue_model2()` | log(1+back) / log(1+back+front) | none |

### Effect of Power Parameter (n)

```
n = 0.5:  prob changes slowly → conservative (fills less)
n = 1.0:  linear proportion → neutral
n = 2.0:  prob changes faster → aggressive (fills more)
n = 3.0:  very aggressive → may overestimate fills on shallow books
```

For TAIFEX shallow queues (5-50 contracts):
- n=3.0 (our default) is likely too aggressive → 14x pessimistic bias paradox
- The issue: with shallow queues, back^3 / (back^3 + front^3) produces extreme probabilities
- At queue position 25/50: prob = 25^3/(25^3+25^3) = 0.5 (neutral)
- At queue position 10/50: prob = 40^3/(40^3+10^3) = 0.985 (almost all behind)
- At queue position 40/50: prob = 10^3/(10^3+40^3) = 0.015 (almost all ahead)
- With n=3, the model is VERY sensitive to initial queue position

**Recommendation for TAIFEX**: Start with n=1.0 (linear) or log_prob_queue_model(), then calibrate against live fill data.

## Model 3: L3FIFOQueueModel

**Python**: `asset.l3_fifo_queue_model()`

**Mechanism**: For Level 3 (market-by-order) data. Tracks individual orders in the queue. Exact FIFO matching. Not applicable to TAIFEX (we only have L1-L5 aggregated data).

## Calibration Procedure

From the official tutorial:

### Step 1: Run backtest with different queue models
```python
# Conservative
asset_conservative = BacktestAsset().risk_adverse_queue_model()

# Moderate
asset_moderate = BacktestAsset().power_prob_queue_model(1.0)

# Aggressive
asset_aggressive = BacktestAsset().power_prob_queue_model(3.0)

# Log-based
asset_log = BacktestAsset().log_prob_queue_model2()
```

### Step 2: Compare with live trading results
- Run same strategy live/paper for 30+ days
- Compare: fill rate, PnL/fill, adverse selection rate
- The queue model whose backtest most closely matches live results is the correct one

### Step 3: Fine-tune parameter
- If power model, sweep n from 0.5 to 3.0 in 0.25 steps
- Minimize |backtest_fill_rate - live_fill_rate|

## Large Tick Size Assets (TAIFEX-Relevant)

From the "Queue-Based Market Making in Large Tick Size Assets" tutorial:

### Key Insight
When tick_size is large relative to typical spread:
- Price almost always sits at best bid/ask (spread = 1 tick)
- Queue position is the PRIMARY factor for fill, not price
- Strategy should think in "queue terms" not "price terms"

### Queue-Based Strategy Pattern
```python
qty_threshold = 250_000  # calibrate per instrument

# Back off when your side is thin (about to be eaten)
if best_bid_qty < qty_threshold and position > 0:
    bid_price = best_bid - tick_size  # step back
else:
    bid_price = best_bid  # stay at front

# Dynamic threshold based on inventory
qty_threshold_bid = qty_threshold * (1 + skew_val)
qty_threshold_ask = qty_threshold * (1 - skew_val)
```

### Use `wait_next_feed()` Instead of `elapse()`
For large-tick assets, react to EVERY book update immediately:
```python
while hbt.wait_next_feed(True, 0) == 0:
    # Process immediately — don't wait fixed intervals
    # This helps avoid adverse selection
```

## For TAIFEX Specifically

TAIFEX TMFD6/TXFD6 characteristics:
- tick_size = 1 point
- spread = typically 1-5 ticks
- queue depth = 5-50 contracts
- No maker rebates (retail pays both sides)

### Recommended Starting Configuration
```python
asset = (
    BacktestAsset()
    .data(npz_files)
    .linear_asset(1.0)        # 1 contract = 1x price
    .constant_order_latency(
        36_000_000,            # 36ms entry (Shioaji P95)
        36_000_000,            # 36ms response
    )
    .no_partial_fill_exchange()
    .power_prob_queue_model(1.0)  # START HERE, calibrate later
    .flat_per_trade_fee_model(
        2.0,                   # TMFD6: 2.0 pts/side (comm+tax)
        2.0,                   # Same for taker
    )
    .tick_size(1.0)
    .lot_size(1.0)
    .roi_lb(0.0)
    .roi_ub(30000.0)          # TXFD6 max price range
)
```
