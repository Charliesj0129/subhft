---
name: hft-strategy-lifecycle
description: Use when creating a new trading strategy from scratch, promoting an alpha to shadow/live, or managing the full scaffold-to-production lifecycle. Covers R47-style config-driven enablement.
---

# HFT Strategy Lifecycle

End-to-end workflow from alpha idea to live production, following the R47 TMFD6 maker pattern.

## Phase 1: Research & Scaffold

```bash
# Create governed alpha package
make research-scaffold ALPHA=r<N>_<short_name>

# Structure created:
# research/alphas/r<N>_<short_name>/
#   manifest.yaml        # Alpha metadata
#   alpha.py             # Signal logic
#   test_alpha.py        # Unit tests
#   README.md            # Research notes
```

Use `hft-alpha-research` skill for signal development. Key rules:
- `float` is OK in research (Alpha Module Float Exception, Rule 11)
- Validate on most recent data first (recency bias rule)
- IC must be detrended (monotonically increasing IC = trend contamination)

## Phase 2: Validation Gates (A → C)

```bash
# Run all gates
make research ALPHA=r<N>_<name> OWNER=<you> DATA='path/to/data.npy'
```

| Gate | Check | Pass criteria |
|------|-------|---------------|
| A | Manifest + data fields + complexity | Schema valid, data columns exist |
| B | Unit tests pass | `pytest research/alphas/r<N>/test_alpha.py` |
| C | Backtest + scorecard | Sharpe > threshold, drawdown < limit |

## Phase 3: Strategy Implementation

Create runtime strategy file:

```python
# src/hft_platform/strategies/r<N>_<name>.py
from hft_platform.strategy.base import BaseStrategy

class R<N>Strategy(BaseStrategy):
    __slots__ = ("_param1", "_param2")  # REQUIRED: __slots__

    def __init__(self, strategy_id: str, **kwargs):
        super().__init__(strategy_id, **kwargs)
        self._param1 = kwargs.get("param1", default)

    def on_features(self, event):
        # Use feature engine signals (27 features in v3)
        features = self.ctx.get_features(event.symbol)
        if features is None:
            return
        # ... signal logic using scaled integers ...

    def on_book_update(self, event):
        l1 = self.ctx.get_l1_scaled(event.symbol)
        if not l1:
            return
        ts, bid, ask, mid_x2, spread, bid_depth, ask_depth = l1
        # ... execution logic ...
        self.ctx.place_order(
            symbol=event.symbol,
            side=Side.BUY,
            price=bid,            # scaled int x10000
            qty=1,
            tif="ROD",
            price_type="LMT",
        )
```

**Constitution compliance** (use `hft-hot-path-dev` skill):
- `__slots__` on class
- Prices as scaled integers
- No heap allocations in event handlers
- Use `timebase.now_ns()` not `datetime.now()`

## Phase 4: Configuration

### 4a. Register strategy

```yaml
# config/base/strategies.yaml
strategies:
  R<N>_<NAME>:
    module: hft_platform.strategies.r<N>_<name>
    class: R<N>Strategy
    enabled: true
    symbols: ["TXFD6"]        # or symbol list
    params:
      param1: value
```

### 4b. Set risk limits

```yaml
# config/base/strategy_limits.yaml
strategy_limits:
  R<N>_<NAME>:
    max_position: 1           # Start conservative
    max_order_size: 1
    max_notional: 500000      # scaled int
```

### 4c. Shadow mode (CRITICAL first step)

```bash
# .env or environment
HFT_ORDER_SHADOW_MODE=1      # Orders never reach broker
HFT_ORDER_MODE=sim           # Double safety
```

## Phase 5: Shadow Trading

```bash
# Start shadow session
uv run hft run sim
# or via Docker:
make start-engine
```

Monitor:
```bash
make logs                     # Watch strategy decisions
make callback-latency-report  # Verify latency budget
```

### Shadow evaluation criteria

| Metric | Threshold | Source |
|--------|-----------|--------|
| Fill rate (hypothetical) | > 30% | Shadow log analysis |
| Avg slippage | < 2 ticks | Execution metrics |
| Max position held | Within limit | Position checkpoint |
| No HALT triggers | 0 events | StormGuard metrics |

## Phase 6: Promotion Gates (D → E)

```bash
# Gate D: Scorecard thresholds
hft alpha promote r<N>_<name> --gate d

# Gate E: Shadow session quality
hft alpha promote r<N>_<name> --gate e
```

## Phase 7: Canary & Go-Live

```bash
# Enable canary
hft alpha canary enable r<N>_<name>

# Monitor canary
make canary-snapshot
# ... wait observation period ...
make canary-evaluate

# Graduate to full live
hft alpha canary graduate r<N>_<name>
```

**Go-live checklist:**
- [ ] Shadow results reviewed
- [ ] `max_pos` set conservatively (start with 1)
- [ ] `HFT_ORDER_SHADOW_MODE=0` (explicitly disable shadow)
- [ ] `HFT_ORDER_MODE=live` (WARNING: real money)
- [ ] Pre-market check passes: `make pre-market-check`
- [ ] Latency profile documented in `config/research/latency_profiles.yaml`

## Post-Launch

- Monitor via `make logs` and `make callback-latency-report`
- Daily: `make post-market-check`
- Weekly: review PnL and slippage metrics
- If issues: `hft alpha canary rollback r<N>_<name>` (immediate)

## Anti-Patterns

- Do NOT skip shadow phase — even 1 day of shadow reveals edge cases
- Do NOT set `max_pos` > 1 on first live day
- Do NOT go live without latency profile (Gate D blocker per MB-06)
- Do NOT assume backtest PnL = live PnL — model broker RTT separately (P95 minimum)
