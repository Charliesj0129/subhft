# R18: SG-LP Shadow Deployment Checklist

**Date**: 2026-03-26
**Strategy**: OpportunisticMM on TMFD6 (SG=5, two-sided, 1-lot cap)
**Mode**: Shadow (no real orders)

---

## Pre-Deploy Verification

| Check | Status |
|-------|--------|
| `strategies.yaml` OPPORTUNISTIC_MM_TMFD6 enabled, SG=5 | PASS |
| `strategy_limits.yaml` max_position=1, max_order_qty=1 | PASS |
| Feature indices match FeatureEngine v2 registry | PASS |
| Latency-adjusted backtest all kill gates PASS | PASS (+4.32 pts/fill OOS) |
| IS/OOS no overfit (OOS > IS excl Mar 23) | PASS |
| All 5 tested days profitable | PASS |

## Config (No Changes Required)

```yaml
# strategies.yaml — already configured
- id: "OPPORTUNISTIC_MM_TMFD6"
  enabled: true
  params:
    spread_threshold_pts: 5
    tick_size_ratio_pct: 50
```

## Required Runtime Guards

Shadow deployment on a single old-computer node is not just `HFT_ORDER_MODE=sim`.

Required runtime state:

```bash
HFT_ORDER_MODE=sim
HFT_ORDER_SHADOW_MODE=1
HFT_GATEWAY_ENABLED=0
```

Notes:
- `HFT_ORDER_MODE=sim` prevents live broker orders, but by itself does not guarantee ClickHouse shadow persistence.
- `HFT_ORDER_SHADOW_MODE=1` is the switch read by `ShadowOrderSink`.
- `HFT_GATEWAY_ENABLED=1` changes the order path to `GatewayService`; on the current code path this can bypass `OrderAdapter.execute()` shadow interception and make `hft.shadow_orders` stay at `0` even when strategies emit intents.
- For single-node shadow rollout, keep the gateway disabled unless you are explicitly validating CE-M2 gateway behavior.

## Shadow Deploy Command

```bash
# Remote: charl@100.91.176.126:~/subhft
# Single-node shadow: keep gateway off, enable explicit shadow intercept
export HFT_ORDER_MODE=sim
export HFT_ORDER_SHADOW_MODE=1
export HFT_GATEWAY_ENABLED=0
docker compose up -d hft-engine
```

## Monitoring (First 3 Sessions)

```bash
# 1. Strategy is actually emitting intents
curl -s http://localhost:9090/metrics | grep '^strategy_intents_total'

# 2. Shadow intercept path is actually recording
curl -s http://localhost:9090/metrics | grep -E '^(shadow_orders_total|shadow_mode_active)'

# 3. Gateway is not silently rejecting orders
curl -s http://localhost:9090/metrics | grep '^gateway_reject_total'

# 4. Platform is not stuck in reduce-only / manual re-arm
curl -s http://localhost:9090/metrics | grep -E '^(platform_reduce_only_active|manual_rearm_required|autonomy_transitions_total)'

# 5. Strategy logs (sampled, useful but not authoritative)
docker compose logs --tail=500 hft-engine 2>&1 | grep -E 'opportunistic_mm|strategy_intent_submit|Shadow order captured'
```

Interpretation:
- `strategy_intents_total{strategy="OPPORTUNISTIC_MM_TMFD6"}` increasing with `shadow_orders_total` flat means the strategy is alive but the shadow intercept/write path is not being hit.
- `gateway_reject_total{reason="PRICE_EXCEEDS_CAP: ... > 50000000"}` indicates the runtime price cap is too low for TMF/TXF futures and orders are being rejected before shadow persistence.
- `platform_reduce_only_active 1` means new opening orders are blocked; clear the underlying dependency fault and perform manual re-arm before expecting shadow order flow.

## Expected Shadow Results

| Metric | Backtest | Shadow Target |
|--------|---------|---------------|
| Fills/session | 458 | 200-600 |
| P&L/fill | +4.32 pts | +2.0-6.0 pts |
| Win rate | 67% | 55-75% |
| Daily NTD | +19,803 | +5,000-30,000 |

## Kill Criteria (Shadow)

- 3 consecutive losing sessions -> pause
- Fills/session < 5 for 3+ sessions -> regime tightened
- StormGuard HALT caused by strategy -> immediate pause

## Known Pitfalls (Observed 2026-03-27)

- `TMFD6` market data may be present and `strategy_intents_total` may be non-zero while `hft.shadow_orders` remains empty.
- The most common causes are:
  - gateway enabled on a single-node shadow deployment
  - price cap too low for futures (`PRICE_EXCEEDS_CAP`)
  - platform stuck in `PLATFORM_REDUCE_ONLY` after `feed_reconnect_unhealthy`
- `docker compose logs ... | grep opportunistic_mm` is only a sampled signal and should not be used as the primary liveness check.
