---
name: hft-strategy
description: "Runtime strategy development (BaseStrategy, StrategyContext, hooks, registration, limits) and the alpha-to-production lifecycle. Use when writing or changing a strategy under strategy/ or strategies/ or preparing promotion. Not for research-only signal work (hft-alpha-research)."
---

# Strategy development

## Frozen state

The live registry is frozen to `r47_tmf_v1` (loop_v1 L11, charter
`docs/loop_v1_stabilization_charter.md`): adding or enabling a second live
strategy, or any `HFT_ORDER_MODE=live` change, is a red line needing the user's
explicit instruction. You may build, test, and shadow-run strategies in sim.

## Contract

- Subclass `BaseStrategy` (`strategy/base.py`); `__slots__` on the class.
- Hooks (all optional): `on_tick`, `on_book_update`, `on_stats`, `on_features`,
  `on_fill`, `on_order`, `on_gap`, `on_risk_feedback`. Emit intents only through
  `ctx.place_order` or `buy` / `sell` / `cancel`.
- Prices are scaled ints (x10000); `Decimal` is auto-scaled by `ctx.scale_price`;
  floats are rejected when `HFT_STRICT_PRICE_MODE=1`. Snap to the tick grid
  (round bids down, asks up).
- Hot-path rules from `AGENTS.md` apply inside handlers. A runner cap of 20
  intents per event applies (`HFT_MAX_INTENTS_PER_EVENT`); the per-strategy
  circuit breaker trips after repeated failures; `budget_us` is a soft limit.
- Required handlers that strategies forget: `on_gap` must reset ALL mutable
  state (bus overflow can lose fills and cancels); `on_risk_feedback` must
  release pending counters; `on_fill` updates local position tracking. Gate
  requotes on price movement so ROD orders do not stack. Check
  `ctx.is_feature_stale` before using features. Spread thresholds are in
  points, not bps.

Details, the position-tracking pattern, and the StrategyContext API:
`references/sdk.md`.

## Register and limit

Live registry: `config/live/strategies.yaml` (read by `strategy/registry.py`;
only enabled, live-valid entries). Risk limits: `config/base/strategy_limits.yaml`
(max position, order qty, intraday PnL limits). Loop binding: `config/loops/<id>.yaml`.
Start `max_pos` at 1. Required features: `required_feature_set_id` and
`required_feature_ids` in the strategy entry.

## Lifecycle

Scaffold (`make research-scaffold ALPHA=...`) -> Gates A-C (`make research ...`,
`hft alpha validate`) -> runtime strategy + tests -> shadow in sim
(`HFT_ORDER_SHADOW_MODE=1`, `HFT_ORDER_MODE=sim`; note `sim` still dispatches) ->
Gates D-E (`hft alpha promote --alpha-id ... --owner ...`) -> canary evaluation
(`hft alpha canary status|evaluate`, `make canary-snapshot canary-evaluate`) ->
live (user-driven, subject to the freeze). Gate criteria, thresholds, and the
go-live checklist: `references/lifecycle.md` and `validation-gate`.

## Verify

```bash
uv run hft strat test --symbol 2330 --strategy-id my_strategy
make test-file FILE=tests/unit/test_strategy_runner_behavior.py
make discipline
```

## Done when

Tests cover the hooks you implemented (including `on_gap` and `on_risk_feedback`),
scaled-int and one-sided-book cases pass, discipline is clean, and no registry
or limit file changed without being intended.

## References

- `references/sdk.md` — read when implementing hooks, order calls, or position tracking.
- `references/lifecycle.md` — read when scaffolding an alpha, shadow-running, or preparing promotion.
