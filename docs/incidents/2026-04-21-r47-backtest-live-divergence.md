# 2026-04-21 — R47 Live/Backtest Divergence Root Cause + Fix Design

**Loss**: -1,722 NTD net (48 fills, 24 trips, flat EOD)
**Backtest prediction (qf=0.5, MakerEngine, same day, same data)**: +7,701 NTD
**Gap**: 9,423 NTD with identical fill count, identical trip count

## TL;DR

The strategy cannot cancel stale orders when market conditions fail a gate
(`spread_threshold_pts`, toxicity, PE regime). Today the median spread was
3 pts but the gate is set at ≥5 pts, so for most of the session the strategy
short-circuited its own cancel path. Combined with Shioaji's ~792 ms round-trip
and `_active_*_oid` only tracking ONE order per side, individual orders sat
7–78 seconds at stale prices during a +180 pt open rally, getting adversely
selected one at a time.

The backtest assumed instantaneous placement/cancel and always-active
bookkeeping, so none of these failure modes appeared.

---

## Evidence

### 1. Order lifetimes by side (CH `hft.orders`, 2026-04-21 TMFE6)

| Side | N orders | avg ms | p50 ms | p95 ms | max ms  | min ms |
|------|----------|--------|--------|--------|---------|--------|
| BUY  | 114      | 1,601  | 315    | 7,057  | 41,323  | 94     |
| SELL | 47       | 5,060  | 460    | 32,363 | 78,736  | 127    |

Min lifetimes (94 / 127 ms) are the Shioaji RTT floor — orders cancelled
as soon as physically possible. Everything above that floor is wasted time.
Backtest (`MakerEngine`) assumes tick-granularity (<1 ms).

### 2. Abandoned-order case study — `v004N`

```
08:48:32.250  v004K BUY 37753 CANCELLED
08:48:32.915  v004N BUY 37754 SUBMITTED    ← placed
   ... 41.3 s, spread 1–3 pts, mid rises 37754 → 37784 ...
08:49:14.238  v004N BUY 37754 CANCELLED    ← finally
```

During the 41-second window:
- No other BUY order was submitted (so not an `_active_buy_oid` overwrite race).
- Best bid moved across **29 pt range** (37755 → 37784) — `bid_moved` should
  have been True on dozens of ticks.
- **But strategy's `on_stats` returned early on the spread gate**, bypassing
  the cancel-stale branch.

The same pattern explains why SELL p95 lifetime is 32 s: SELL quotes posted
early in the rally sit waiting for market to sweep up into them, adversely
filling one at a time 10–80 s later.

### 3. `cancel_already_terminal` spam

22 distinct orders had duplicate cancel dispatches (210 total). Root cause:

```python
if bid_moved:
    old_buy_oid = self._active_buy_oid.get(exec_sym)
    if old_buy_oid:
        self.cancel(exec_sym, old_buy_oid)
# NOTE: _active_buy_oid is NOT cleared here; only on_order(CANCELLED) clears it.
```

With 792 ms broker RTT and Shioaji's CANCELLED callback taking even longer,
the strategy fires cancel repeatedly against the same `order_id` until the
terminal callback finally arrives.

### 4. Backtest vs live mismatch structure

`research/backtest/maker_engine.py` (the production-reliable path):
- Strategy decides → order appears at price instantly
- Cancel decision → order gone instantly
- `queue_fraction` probability model for passive fill
- No async callbacks, no order-id tracking, no gate short-circuits

Live pipeline:
- Strategy → intent → async dispatch → broker → ~792 ms → SUBMITTED callback
- Cancel → 792 ms → CANCELLED callback
- Gate short-circuit (`return`) skips cancel branch
- `_active_*_oid` tracks 1 order per side only (multi-order races lose orders)

---

## Root-cause hierarchy

1. **Primary — gate short-circuit**: `spread_threshold_pts=5` with median
   live spread of 3 pts means the strategy `return`s from `on_stats` without
   executing the cancel-stale branch for the majority of ticks. Stale orders
   sit until a rare tick widens spread ≥ 5 pts.
2. **Secondary — cooldown shorter than RTT**: `_QUOTE_COOLDOWN_NS=200 ms`
   but Shioaji RTT is ~792 ms. Up to 4 orders can be in flight per side
   before the first SUBMITTED callback acknowledges.
3. **Tertiary — single-slot oid tracking**: `_active_buy_oid` / `_active_sell_oid`
   hold ONE broker oid per side. If the SUBMITTED callback order interleaves
   with new placements, the tracking dict points at the wrong order.
4. **Tertiary — repeated cancel on same oid**: cancel-fire path does not
   optimistically clear the oid, so every `bid_moved` tick re-cancels.

---

## Fix design — backtest-consistent execution system

**Core principle**: treat the strategy's internal model as the source of
truth, and make the live adapter a *reliable* executor of that model.

### D1 — decouple cancel-stale from quote gates

`on_stats` must never `return` before reconciling active orders against the
current mid. Quote gates should *suppress placement* but always *permit cleanup*.

```python
def on_stats(self, event):
    # ... validity guard ...
    self._reconcile_active_quotes(event)   # ALWAYS runs
    if self._gates_block(event):
        return
    self._generate_quotes(symbol, event, ...)
```

`_reconcile_active_quotes` cancels any active order whose price deviates
from current mid by more than `max_quote_distance_ticks` (default 2),
regardless of gate state.

### D2 — cooldown ≥ broker RTT + safety margin

```yaml
# strategies.yaml (R47_MAKER_TMF)
quote_cooldown_ms: 1000  # was 200; must exceed Shioaji P95 RTT
```

Backed by a metric `hft_strategy_quote_cooldown_violation_total` to catch
regressions if Shioaji latency drifts.

### D3 — intent-id based tracking (replaces broker-oid tracking)

The strategy must track ALL client-side `intent_id`s until the adapter
confirms terminal state, not just the most recent SUBMITTED broker oid.

```python
# pseudo-code
self._inflight_buy: dict[str, set[str]] = {}   # sym -> {intent_id, ...}
self._inflight_sell: dict[str, set[str]] = {}
```

Place → add intent_id to inflight set. SUBMITTED callback → move from
inflight to active (keyed by intent_id, carrying broker oid). FILL/CANCEL
callback → remove.

The adapter already maps intent_id → broker order, so `cancel(intent_id)`
is supported on the existing path.

### D4 — optimistic oid clear after cancel

```python
if bid_moved:
    for intent_id in list(self._active_buy.keys()):  # not just one
        self.cancel(exec_sym, intent_id)
        self._cancel_inflight[exec_sym].add(intent_id)
    self._active_buy.pop(exec_sym, None)  # optimistic
```

Re-cancel only if ack times out (>2× RTT). Eliminates the 210/day duplicate
cancel spam.

### D5 — gate-aware shadow backtest mode

Add a CLI flag `--live-simulator` to `MakerEngine` that injects:
- configurable placement latency (draw from live distribution)
- configurable cancel latency
- gate evaluation at each tick identical to live `on_stats`
- no perfect inventory reset between ticks

Run today's live trace through it; should match within ±1000 NTD instead
of ±9400 NTD.

### D6 — divergence detector

New Prometheus metrics:
- `hft_order_lifetime_seconds` histogram (label: side)
- `hft_quote_distance_from_mid_ticks` histogram (label: side)
- `hft_stale_quote_cancel_total` (triggered by D1)
- `hft_cancel_already_terminal_rate` (existing) — alert when > 5/min

Alert: if p95 order lifetime > 2 × broker RTT for > 5 min, page ops.

---

## Implementation order (recommendation)

| Step | Change | Risk | Expected impact |
|------|--------|------|-----------------|
| 1 | Raise `quote_cooldown_ms` to 1000 (config-only) | None — config | -40% duplicate cancels |
| 2 | Add `_reconcile_active_quotes` (D1) | Low — new code path | -80% stale quotes, closes primary loss path |
| 3 | Intent-id tracking (D3) | Medium — state refactor | Fixes multi-order race |
| 4 | Shadow simulator (D5) | None — research path | Calibrates future backtests |
| 5 | Divergence detector (D6) | Low — metrics only | Prevents silent regression |

Step 1 + 2 together should recover the bulk of the 9.4 K gap. They can ship
independently, pre-GA-tested in sim mode, and rolled out with the existing
1-lot canary.

## What this does NOT claim

- Today's -1,722 NTD does not prove the strategy is structurally unprofitable;
  it proves the live *executor* is not faithful to the *strategy*.
- qf=0.5 backtest of +7,701 NTD is also optimistic by construction (perfect
  placement, no gate short-circuit). Realistic fix target is somewhere in the
  middle — flat-to-slightly-positive — which is consistent with historical
  R47 TMFD6 deployment results (-27 K to +61 K across methods; see
  `r47_maker_strategy.md` memory).
