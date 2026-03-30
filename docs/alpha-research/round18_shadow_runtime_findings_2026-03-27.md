# Round 18 Shadow Runtime Findings

**Date:** 2026-03-27  
**Host:** `THESHOW` (`charl@100.91.176.126`)  
**Project root:** `~/subhft`

## Summary

Observed runtime behavior on 2026-03-27 does not match the current Round 18 shadow checklist.

The most important result is:

- `TMFD6` market data is present.
- `OPPORTUNISTIC_MM_TMFD6` is registered.
- `strategy_intents_total{strategy="OPPORTUNISTIC_MM_TMFD6"}` is non-zero.
- `hft.shadow_orders` remains empty.

This is not a "strategy dead" scenario. It is a downstream routing / configuration / autonomy-state problem.

## Verified Facts

### 1. Strategy activity exists

Remote metrics showed:

- `strategy_intents_total{strategy="OPPORTUNISTIC_MM_TMFD6"} = 28256`

This proves the strategy emitted intents during the session.

### 2. TMFD6 feed exists

Remote ClickHouse `hft.market_data` contained `TMFD6` rows on 2026-03-27 with intraday timestamps extending into the afternoon session.

### 3. Shadow table is still empty

Remote ClickHouse `hft.shadow_orders` returned:

- total rows: `0`
- rows for 2026-03-27: `0`

### 4. Gateway is enabled

Remote container environment included:

- `HFT_GATEWAY_ENABLED=1`

For a single-node old-computer shadow deployment, this is a risky divergence from the documented safe default.

### 5. Gateway is actively rejecting intents

Remote Prometheus exposed a large set of:

- `gateway_reject_total{reason="PRICE_EXCEEDS_CAP: ... > 50000000"}`

This matches the current risk validator default:

- `max_price_cap = 5000.0`
- `scale = 10000`
- scaled cap = `50000000`

That cap is too low for `TMFD6` / `TXFD6` futures prices around the 33k range.

### 6. Platform is in reduce-only

Remote Prometheus exposed:

- `autonomy_mode{scope="platform"} 2`
- `platform_reduce_only_active 1`
- `manual_rearm_required{scope="platform"} 1`
- `autonomy_transitions_total{from_mode="NORMAL",reason="feed_reconnect_unhealthy",scope="platform",to_mode="PLATFORM_REDUCE_ONLY"} 1`

This means the platform has already degraded and requires explicit operator re-arm.

## Root-Cause Chain

The current runtime failure chain is:

1. Strategy emits intents.
2. Because `HFT_GATEWAY_ENABLED=1`, intents first enter `GatewayService`.
3. Many intents are rejected by risk/validator checks due to `PRICE_EXCEEDS_CAP`.
4. Independently, the platform is already in `PLATFORM_REDUCE_ONLY` because of `feed_reconnect_unhealthy`.
5. The current gateway path dispatches approved commands directly to the order adapter API queue, which means `hft.shadow_orders` is not a reliable proof of strategy inactivity.

## Additional Documentation Gaps Found

### Shadow checklist mismatch

The current checklist states or implies:

- `HFT_ORDER_MODE=sim` is enough for shadow
- `docker compose logs ... | grep opportunistic_mm` is a primary liveness signal
- `curl ... | grep -E "hft_strategy_(intents|fills|pnl)"` is the relevant metric check

These assumptions are not reliable on the current runtime.

### `shadow_mode_active` metric mismatch

The metric exists in code, but there is no confirmed runtime `.set()` path tied to `ShadowOrderSink.enabled`.
As a result, `shadow_mode_active=0` cannot currently be trusted as a definitive statement that shadow mode is off.

### `shadow_orders` schema/query mismatch

Operational queries that assume:

- a `ts` column
- a `simulated_pnl` column

do not match the actual deployed schema, which currently contains:

- `ts_ns`
- `strategy_id`
- `symbol`
- `side`
- `price`
- `qty`
- `intent_type`
- `intent_id`
- `inserted_at`

### Daily report scheduling mismatch

The runtime crontab observed on 2026-03-27 runs:

- `scripts/soak_acceptance.py daily` at `16:10`

This differs from the assumption that `scripts/shadow_daily_report.py` is the primary scheduled daily report path.

## Operator Guidance

Before trusting shadow-session conclusions on the old computer, verify all of the following:

1. `strategy_intents_total{strategy="OPPORTUNISTIC_MM_TMFD6"}` is increasing.
2. `gateway_reject_total` is either absent or stable for the intended runtime mode.
3. `platform_reduce_only_active` is `0`.
4. `manual_rearm_required{scope="platform"}` is `0`.
5. `HFT_GATEWAY_ENABLED=0` unless the explicit goal is to validate the CE-M2 gateway path.
6. `HFT_ORDER_SHADOW_MODE=1` is set if shadow interception is expected.
7. `hft.shadow_orders` is interpreted only as evidence of the intercept/write path, not as a proxy for strategy liveness.
