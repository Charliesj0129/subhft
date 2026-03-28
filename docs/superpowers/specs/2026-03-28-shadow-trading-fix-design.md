# Shadow Trading Fix + Hardening

**Date**: 2026-03-28
**Status**: Approved
**Host**: `THESHOW` (`charl@100.91.176.126:~/subhft`)

## Problem

On 2026-03-27, shadow deployment of `OPPORTUNISTIC_MM_TMFD6` emitted 28,256 strategy intents but recorded zero shadow orders. Three independent blockers formed a kill chain:

1. **`HFT_GATEWAY_ENABLED=1`** on remote — intents entered `GatewayService` which runs `PriceBandValidator` before orders reach `OrderAdapter.ShadowOrderSink`.
2. **`PRICE_EXCEEDS_CAP`** — default `max_price_cap=5000.0` (scaled to 50M) rejects TMFD6 prices (~33k, scaled to 330M). All futures orders rejected.
3. **`PLATFORM_REDUCE_ONLY`** — triggered by `feed_reconnect_unhealthy`, blocks all new opening orders. Requires manual re-arm that never happened.

The strategy was alive. The downstream routing/risk/autonomy pipeline killed all its output.

## Solution Overview

| Section | Scope | Risk | Timeline |
|---------|-------|------|----------|
| S1: Ops fix | Remote `.env` | Zero | Monday morning |
| S2: Per-product-type price caps | `risk/validators.py`, risk YAML | Low | Same PR |
| S3: Shadow bypasses reduce-only | `ops/platform_degrade.py` | Low | Same PR |
| S4: Auto-recovery from reduce-only | `ops/platform_degrade.py`, `platform_inputs.py`, `bootstrap.py` | Medium | Same PR |
| S5: Observability | `order/shadow.py`, `bootstrap.py`, `risk/validators.py` | Zero | Same PR |

---

## S1: Ops Fix (Remote — Monday Morning)

### Changes to `~/subhft/.env`

```bash
HFT_GATEWAY_ENABLED=0          # was 1 — gateway runs risk validators that reject futures prices
HFT_ORDER_SHADOW_MODE=1         # explicit shadow intercept at OrderAdapter level
HFT_ORDER_MODE=sim              # redundant with shadow but safe belt-and-suspenders
```

### Deploy

```bash
docker compose up -d hft-engine
```

### Verification (after market open)

```bash
# 1. Strategy emitting intents
curl -s http://localhost:9090/metrics | grep '^strategy_intents_total'

# 2. Shadow intercept recording
curl -s http://localhost:9090/metrics | grep -E '^(shadow_orders_total|shadow_mode_active)'

# 3. Not stuck in reduce-only
curl -s http://localhost:9090/metrics | grep -E '^(platform_reduce_only_active|manual_rearm_required)'

# 4. ClickHouse shadow table populated
docker exec clickhouse clickhouse-client \
  --query "SELECT count() FROM hft.shadow_orders WHERE toDate(ts_ns/1e9) = today()"
```

**Success criteria**: `shadow_orders_total > 0` AND `hft.shadow_orders` row count > 0 within 30 minutes of market open.

---

## S2: Per-Product-Type Price Caps

### Problem

`PriceBandValidator` uses a single `max_price_cap` (default 5000.0 NTD). Futures prices (20k-35k range) exceed this cap after scaling, causing 100% rejection.

### Design

**Config** (risk YAML, `global_defaults` section):

```yaml
global_defaults:
  max_price_cap: 5000.0            # stocks (unchanged default)
  max_price_cap_futures: 50000.0   # futures — covers TMFD6/TXFD6/MXFD6
  max_price_cap_options: 10000.0   # TXO options — conservative
```

**Resolution order** in `PriceBandValidator.check()`:

1. Per-symbol override (if present in config) — most specific
2. Per-product-type cap (keyed by `product_type` from strategy config: `"FUT"`, `"OPT"`, `"STK"`)
3. Global `max_price_cap` — fallback for unknown product types

**Implementation** in `risk/validators.py`:

```python
class PriceBandValidator(RiskValidator):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._max_price_cap_raw = float(self.defaults.get("max_price_cap", 5000.0))
        self._product_caps_raw: dict[str, float] = {
            "FUT": float(self.defaults.get("max_price_cap_futures", 50000.0)),
            "OPT": float(self.defaults.get("max_price_cap_options", 10000.0)),
        }
        # Cache: symbol -> scaled cap (populated on first access, no hot-path alloc)
        self._max_price_scaled_cache: dict[str, int] = {}

    def _resolve_cap_raw(self, symbol: str, product_type: str | None) -> float:
        # Per-symbol override (future extensibility)
        sym_cap = self.defaults.get(f"max_price_cap_{symbol}")
        if sym_cap is not None:
            return float(sym_cap)
        # Per-product-type
        if product_type and product_type in self._product_caps_raw:
            return self._product_caps_raw[product_type]
        # Global fallback
        return self._max_price_cap_raw
```

**Hot-path constraint**: `_resolve_cap_raw()` is called once per symbol (first encounter), result cached in `_max_price_scaled_cache`. Zero allocations on subsequent ticks.

**Product type source**: `OrderIntent` already carries `strategy_id`. The strategy registry maps `strategy_id -> product_type`. Pass `product_type` through `OrderIntent` metadata or resolve at validator init from strategy config.

### Files Changed

- `src/hft_platform/risk/validators.py` — `PriceBandValidator` class
- `config/base/risk.yaml` (or equivalent) — add `max_price_cap_futures`, `max_price_cap_options`

### Tests

- `test_price_cap_stock_default` — stock price within 5000 passes
- `test_price_cap_futures_pass` — futures price 35000 passes with futures cap
- `test_price_cap_futures_reject_old_default` — futures price 35000 rejected without futures cap (regression guard)
- `test_price_cap_resolution_order` — per-symbol > per-product > global

---

## S3: Shadow Bypasses Reduce-Only

### Problem

`PlatformDegradeController.allow_intent()` blocks new opening orders when `reduce_only_active=True`. This blocks shadow orders that have zero financial risk.

### Design

**Change** in `ops/platform_degrade.py`:

```python
class PlatformDegradeController:
    def __init__(self, *, shadow_mode: bool = False, ...):
        self._shadow_mode = shadow_mode
        # ... existing init

    def allow_intent(self, *, intent_type: IntentType | int | str, opens_risk: bool) -> bool:
        if self._shadow_mode:
            return True  # shadow orders have zero financial risk
        # ... existing logic unchanged
```

**Wiring** in `services/bootstrap.py`:

```python
shadow_mode = os.getenv("HFT_ORDER_SHADOW_MODE", "0") == "1"
degrade_controller = PlatformDegradeController(shadow_mode=shadow_mode, ...)
```

**Safety**: `validate_shadow_lock()` already prevents `shadow_mode=True + HFT_ORDER_MODE=live`. The bypass only activates in shadow mode.

### Files Changed

- `src/hft_platform/ops/platform_degrade.py` — add `shadow_mode` param + bypass
- `src/hft_platform/services/bootstrap.py` — pass flag at init

### Tests

- `test_shadow_mode_allows_all_intents` — all intent types pass when shadow=True + reduce_only=True
- `test_non_shadow_reduce_only_blocks_new_opening` — existing behavior preserved

---

## S4: Auto-Recovery from Reduce-Only

### Problem

`feed_reconnect_unhealthy` triggers `PLATFORM_REDUCE_ONLY` with `manual_rearm_required=True`. If the feed recovers but no operator intervenes, the platform stays stuck for the rest of the session.

### Design

**New method** in `PlatformDegradeController`:

```python
def check_auto_recovery(self, *, current_reasons: list[str], now_ns: int) -> bool:
    """Check if auto-recovery should trigger. Called from supervisor loop.

    Returns True if recovery was performed.
    """
    if not self.reduce_only_active:
        return False
    if not self._auto_recovery_enabled:
        return False
    # Only auto-recover from feed-related triggers
    if self._last_entry_reason not in self._AUTO_RECOVERABLE_REASONS:
        return False

    if current_reasons:
        # Triggers still active — reset timer
        self._recovery_started_ns = 0
        return False

    if self._recovery_started_ns == 0:
        # Start cooldown timer
        self._recovery_started_ns = now_ns
        logger.info("auto_recovery_cooldown_started", cooldown_s=self._auto_recovery_cooldown_s)
        return False

    elapsed_ns = now_ns - self._recovery_started_ns
    if elapsed_ns >= self._auto_recovery_cooldown_ns:
        self.exit_reduce_only(reason=f"auto_recovery: triggers_cleared_{self._auto_recovery_cooldown_s}s")
        self._recovery_started_ns = 0
        return True

    return False
```

**Auto-recoverable reasons** (whitelist):

```python
_AUTO_RECOVERABLE_REASONS: frozenset[str] = frozenset({
    "feed_reconnect_unhealthy",
    "feed_gap_exceeded",
})
```

Operator-initiated halts, risk breaches, and other critical reasons are NOT auto-recoverable.

**Config**:

| Env Var | Default | Purpose |
|---------|---------|---------|
| `HFT_PLATFORM_AUTO_RECOVERY_ENABLED` | `1` | Enable/disable auto-recovery |
| `HFT_PLATFORM_AUTO_RECOVERY_COOLDOWN_S` | `60` | Seconds of clear triggers before auto-exit |

**Integration**: Called from the existing supervisor loop in `services/system.py` (or wherever `reduce_only_reasons()` is polled), immediately after checking reasons:

```python
reasons = inputs.reduce_only_reasons()
if reasons:
    controller.enter_reduce_only(reason=reasons[0])
controller.check_auto_recovery(current_reasons=reasons, now_ns=timebase.now_ns())
```

### Files Changed

- `src/hft_platform/ops/platform_degrade.py` — add `check_auto_recovery()`, config params, reason whitelist
- `src/hft_platform/services/bootstrap.py` — read env vars, pass to controller
- `src/hft_platform/services/system.py` (or supervisor loop location) — call `check_auto_recovery()`

### Tests

- `test_auto_recovery_after_cooldown` — triggers clear, wait 60s, exits reduce-only
- `test_auto_recovery_reset_on_retrigger` — triggers clear then reappear, timer resets
- `test_auto_recovery_skips_non_feed_reasons` — operator halt not auto-recovered
- `test_auto_recovery_disabled` — env var `=0` prevents recovery

---

## S5: Observability Improvements

### 5a: `shadow_mode_active` Gauge

**Where**: `src/hft_platform/order/shadow.py` — `ShadowOrderSink.__init__()`

```python
metrics = _get_metrics()
if metrics and hasattr(metrics, "shadow_mode_active"):
    metrics.shadow_mode_active.set(1 if self._enabled else 0)
```

### 5b: Startup Env Var Log

**Where**: `src/hft_platform/services/bootstrap.py` — after env loading

```python
logger.info(
    "shadow_config_summary",
    shadow_mode=os.getenv("HFT_ORDER_SHADOW_MODE", "0"),
    gateway_enabled=os.getenv("HFT_GATEWAY_ENABLED", "0"),
    order_mode=os.getenv("HFT_ORDER_MODE", "sim"),
    max_price_cap=resolved_price_cap,
    max_price_cap_futures=resolved_futures_cap,
    auto_recovery_enabled=os.getenv("HFT_PLATFORM_AUTO_RECOVERY_ENABLED", "1"),
)
```

### 5c: `price_cap_rejects_total` Counter

**Where**: `src/hft_platform/risk/validators.py` — `PriceBandValidator.check()`

```python
if intent.price > max_price_scaled:
    self._price_cap_rejects.labels(symbol=intent.symbol).inc()
    return False, f"PRICE_EXCEEDS_CAP: {intent.price} > {max_price_scaled}"
```

Dedicated counter with symbol label, separate from the unstructured `gateway_reject_total{reason=...}` string.

### Files Changed

- `src/hft_platform/order/shadow.py` — set gauge
- `src/hft_platform/services/bootstrap.py` — startup log
- `src/hft_platform/risk/validators.py` — dedicated counter

---

## Implementation Order

1. **S1** (ops fix) — deploy Monday morning, zero code changes
2. **S3** (shadow bypass) — simplest code change, immediate value
3. **S2** (price caps) — prevents the root cause rejection
4. **S5** (observability) — low risk, aids debugging
5. **S4** (auto-recovery) — most complex, new state machine logic

All code changes (S2-S5) ship in one PR. S1 is an independent ops action.
