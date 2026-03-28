# Shadow Trading Fix + Hardening (v2)

**Date**: 2026-03-28
**Status**: Approved (revised after code review)
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
| S2: Symbol-based price caps | All 3 risk paths (Python, FastGate, Rust) | Low | Same PR |
| S3: Shadow bypasses reduce-only | Shared singleton controller | Low | Same PR |
| S4: Active-reasons reduce-only + auto-recovery | `platform_degrade.py`, supervisor loop | Medium | Same PR |
| S5: Observability | `shadow.py`, `bootstrap.py` | Zero | Same PR |

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

## S2: Symbol-Based Price Caps (All 3 Risk Paths)

### Problem

`PriceBandValidator` uses a single `max_price_cap` (default 5000.0 NTD). Futures prices (20k-35k range) exceed this cap after scaling, causing 100% rejection. The same cap is hardcoded into `FastGate` (Numba JIT) and `RustRiskValidator` — fixing only the Python validator leaves two alternate paths broken.

### Product Type Source: SymbolMetadata (Not Strategy Registry)

**v1 spec assumed** strategy config or OrderIntent metadata carried product type. Neither does — `OrderIntent` has no `product_type` field, and risk validators don't load the strategy registry.

**Actual source**: `SymbolMetadata.product_type(symbol)` in `feed_adapter/normalizer.py:251-282`. It resolves product type from `config/symbols.yaml` fields (`product_type`, `security_type`, `type`, `asset_type`) with exchange-based fallback (`FUT`/`FUTURES`/`TAIFEX` → `"future"`). Results are cached per-symbol.

**Access path in risk**: Validators already hold `self.price_codec` which wraps `SymbolMetadataPriceScaleProvider`, which wraps `SymbolMetadata`. Product type is available via `self.price_codec.provider.metadata.product_type(symbol)` with zero new wiring.

### Config

Add to `config/base/strategy_limits.yaml` (the actual risk config, NOT `risk.yaml`):

```yaml
global_defaults:
  max_price_cap: 5000.0            # stocks (unchanged default)
  max_price_cap_futures: 50000.0   # futures — covers TMFD6/TXFD6/MXFD6
  max_price_cap_options: 10000.0   # TXO options — conservative
```

### Implementation: PriceBandValidator (`risk/validators.py`)

```python
class PriceBandValidator(RiskValidator):
    # Product type string -> config key mapping
    _PRODUCT_CAP_KEYS: dict[str, str] = {
        "future": "max_price_cap_futures",
        "option": "max_price_cap_options",
    }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._max_price_cap_raw = float(self.defaults.get("max_price_cap", 5000.0))
        self._product_caps_raw: dict[str, float] = {}
        for ptype, key in self._PRODUCT_CAP_KEYS.items():
            val = self.defaults.get(key)
            if val is not None:
                self._product_caps_raw[ptype] = float(val)
        # Cache: symbol -> scaled cap (populated on first access, no hot-path alloc)
        self._max_price_scaled_cache: dict[str, int] = {}

    def _resolve_cap_raw(self, symbol: str) -> float:
        """Resolve price cap: per-symbol > per-product-type > global."""
        # Per-symbol override
        sym_cap = self.defaults.get(f"max_price_cap_{symbol}")
        if sym_cap is not None:
            return float(sym_cap)
        # Per-product-type via SymbolMetadata
        metadata = getattr(getattr(self.price_codec, "provider", None), "metadata", None)
        if metadata is not None:
            ptype = metadata.product_type(symbol)  # cached inside SymbolMetadata
            if ptype in self._product_caps_raw:
                return self._product_caps_raw[ptype]
        # Global fallback
        return self._max_price_cap_raw
```

Hot-path constraint: `_resolve_cap_raw()` is called once per symbol (first encounter). Both the product type lookup (cached in `SymbolMetadata._product_type_cache`) and the result (cached in `_max_price_scaled_cache`) are zero-alloc on subsequent ticks.

### Implementation: FastGate (`risk/engine.py:_init_fast_gate`)

FastGate uses a single `max_price_scaled` integer (Numba JIT, no dict lookups). Per-product-type dispatch inside JIT is not feasible.

**Approach**: Raise FastGate's price cap to `max(all configured caps)`. FastGate is a coarse pre-filter (kill switch + extreme fat-finger); the Python `PriceBandValidator` provides precise per-product-type enforcement downstream.

```python
# In _init_fast_gate():
all_caps = [max_price_cap]
for key in ("max_price_cap_futures", "max_price_cap_options"):
    val = defaults.get(key)
    if val is not None:
        all_caps.append(float(val))
fast_gate_cap = max(all_caps)
max_price_scaled = int(fast_gate_cap * max(1, scale))
```

This preserves FastGate's role (reject obviously insane prices at near-zero cost) while not blocking legitimate futures prices.

### Implementation: RustRiskValidator (`risk/engine.py:_init_rust_validator`)

Same approach as FastGate — Rust validator's `max_price_cap_scaled` is set to `max(all configured caps)`. The Rust validator is also a coarse pre-filter; precise per-product-type enforcement remains in Python.

```python
# In _init_rust_validator():
all_caps = [max_price_cap_raw]
for key in ("max_price_cap_futures", "max_price_cap_options"):
    val = defaults.get(key)
    if val is not None:
        all_caps.append(float(val))
rust_cap = max(all_caps)
max_price_cap_scaled = int(rust_cap * max(1, scale))
```

### Files Changed

- `src/hft_platform/risk/validators.py` — `PriceBandValidator`: per-product-type cap resolution via `SymbolMetadata`
- `src/hft_platform/risk/engine.py` — `_init_fast_gate()` and `_init_rust_validator()`: use `max(all caps)` for coarse gates
- `config/base/strategy_limits.yaml` — add `max_price_cap_futures`, `max_price_cap_options` to `global_defaults`

### Tests

- `test_price_cap_stock_default` — stock price within 5000 passes
- `test_price_cap_futures_pass` — futures price 35000 passes with futures cap via SymbolMetadata
- `test_price_cap_futures_reject_old_default` — futures price 35000 rejected WITHOUT futures cap config (regression guard)
- `test_price_cap_resolution_order` — per-symbol > per-product > global
- `test_fast_gate_uses_max_of_all_caps` — FastGate cap = max(stock, futures, options)
- `test_rust_validator_uses_max_of_all_caps` — same for Rust path

---

## S3: Shadow Bypasses Reduce-Only

### Problem

`PlatformDegradeController.allow_intent()` blocks new opening orders when `reduce_only_active=True`. This blocks shadow orders that have zero financial risk.

### Singleton Wiring

The controller is a module-level singleton (`_shared_controller`) accessed via `get_shared_platform_degrade_controller()`. It is NOT created in bootstrap — it's created on first access (typically from `OrderAdapter.__init__`). Adding constructor params to `PlatformDegradeController.__init__()` is not enough; the singleton factory must also accept and forward them.

### Design

**Change 1**: Add `shadow_mode` to `PlatformDegradeController.__init__()`:

```python
class PlatformDegradeController:
    def __init__(self, *, metrics: Any | None = None, evidence_writer: Any | None = None,
                 shadow_mode: bool = False) -> None:
        self._shadow_mode = shadow_mode
        # ... existing init
```

**Change 2**: Update singleton factory:

```python
def get_shared_platform_degrade_controller(
    *, metrics: Any | None = None, shadow_mode: bool | None = None,
) -> PlatformDegradeController:
    global _shared_controller
    with _shared_controller_lock:
        if _shared_controller is None:
            _shadow = shadow_mode if shadow_mode is not None else (
                os.getenv("HFT_ORDER_SHADOW_MODE", "0") == "1"
            )
            _shared_controller = PlatformDegradeController(
                metrics=metrics, shadow_mode=_shadow,
            )
        elif metrics is not None and _shared_controller.metrics is None:
            _shared_controller.metrics = metrics
            _shared_controller._sync_metrics()
        return _shared_controller
```

The factory reads `HFT_ORDER_SHADOW_MODE` directly if no explicit `shadow_mode` is passed. This ensures the singleton is correctly configured regardless of which call site creates it first (OrderAdapter, autonomy_monitor, reconciliation, etc.).

**Change 3**: Bypass in `allow_intent()`:

```python
def allow_intent(self, *, intent_type: IntentType | int | str, opens_risk: bool) -> bool:
    if self._shadow_mode:
        return True  # shadow orders have zero financial risk
    # ... existing logic unchanged
```

**Safety**: `validate_shadow_lock()` in bootstrap already prevents `shadow_mode + live orders` coexistence.

### Files Changed

- `src/hft_platform/ops/platform_degrade.py` — constructor + factory + `allow_intent()`

### Tests

- `test_shadow_mode_allows_all_intents` — all intent types pass when shadow=True + reduce_only=True
- `test_non_shadow_reduce_only_blocks_new_opening` — existing behavior preserved
- `test_singleton_reads_env_var` — factory creates controller with correct shadow_mode from env

---

## S4: Active-Reasons Reduce-Only + Auto-Recovery

### Problem (v1 spec flaw)

The v1 spec keyed auto-recovery off `_last_entry_reason`, but `enter_reduce_only()` returns immediately if already active (line 44). A later non-auto-recoverable cause (e.g., reconciliation drift) is never recorded. Auto-recovery could exit a state that should remain manual.

**Root fix**: The controller needs an active-reasons model, not a single remembered reason.

### Design: Active Reasons Set

Replace the single `last_transition` reason tracking with a set of active reasons:

```python
class PlatformDegradeController:
    def __init__(self, *, metrics=None, evidence_writer=None, shadow_mode=False,
                 auto_recovery_enabled=True, auto_recovery_cooldown_s=60) -> None:
        # ... existing fields
        self._active_reasons: set[str] = set()
        self._auto_recovery_enabled = auto_recovery_enabled
        self._auto_recovery_cooldown_s = auto_recovery_cooldown_s
        self._auto_recovery_cooldown_ns = int(auto_recovery_cooldown_s * 1_000_000_000)
        self._recovery_started_ns: int = 0
```

**Modified `enter_reduce_only()`**: Always records the reason, even if already active:

```python
def enter_reduce_only(self, *, reason: str) -> AutonomyTransition:
    self._active_reasons.add(reason)
    if self.reduce_only_active and self.last_transition is not None:
        logger.info("platform_reduce_only_reason_added", reason=reason,
                     active_reasons=sorted(self._active_reasons))
        return self.last_transition
    # ... existing transition logic (unchanged)
```

**New `remove_reason()` + `check_auto_recovery()`**:

```python
_AUTO_RECOVERABLE_REASONS: frozenset[str] = frozenset({
    "feed_reconnect_unhealthy",
    "feed_gap_exceeded",
})

def check_auto_recovery(self, *, current_reasons: list[str], now_ns: int) -> bool:
    """Called from supervisor loop after checking reduce_only_reasons()."""
    if not self.reduce_only_active or not self._auto_recovery_enabled:
        return False

    # Sync active reasons: remove reasons no longer reported by inputs
    input_reason_set = set(current_reasons)
    # Keep non-input reasons (e.g. reconciliation drift) — they are added
    # by other call sites and cleared only by explicit exit_reduce_only()
    auto_recoverable_active = self._active_reasons & self._AUTO_RECOVERABLE_REASONS
    still_active = auto_recoverable_active & input_reason_set

    # Update: remove auto-recoverable reasons that inputs no longer report
    cleared = auto_recoverable_active - still_active
    if cleared:
        self._active_reasons -= cleared
        logger.info("auto_recovery_reasons_cleared", cleared=sorted(cleared),
                     remaining=sorted(self._active_reasons))

    # If ANY non-auto-recoverable reason remains, do not auto-recover
    non_recoverable = self._active_reasons - self._AUTO_RECOVERABLE_REASONS
    if non_recoverable:
        self._recovery_started_ns = 0
        return False

    # If any active reason remains (auto-recoverable but still firing), reset
    if self._active_reasons:
        self._recovery_started_ns = 0
        return False

    # All reasons cleared — run cooldown timer
    if self._recovery_started_ns == 0:
        self._recovery_started_ns = now_ns
        logger.info("auto_recovery_cooldown_started",
                     cooldown_s=self._auto_recovery_cooldown_s)
        return False

    elapsed_ns = now_ns - self._recovery_started_ns
    if elapsed_ns >= self._auto_recovery_cooldown_ns:
        self.exit_reduce_only(
            reason=f"auto_recovery: all_reasons_cleared_{self._auto_recovery_cooldown_s}s"
        )
        self._recovery_started_ns = 0
        return True

    return False
```

**Modified `exit_reduce_only()`**: Clears active reasons:

```python
def exit_reduce_only(self, *, reason: str) -> AutonomyTransition:
    # ... existing logic
    self._active_reasons.clear()
    self._recovery_started_ns = 0
    # ... rest unchanged
```

### Key Safety Properties

1. **Non-recoverable reason blocks auto-exit**: If `reconciliation_drift` was added while `feed_reconnect_unhealthy` was already active, auto-recovery will NOT fire because `non_recoverable` is non-empty.
2. **Auto-recoverable reasons are synced from inputs**: The supervisor loop reports current reasons each tick. Auto-recoverable reasons are removed when inputs stop reporting them.
3. **Non-auto-recoverable reasons persist until explicit exit**: Only `exit_reduce_only()` clears them. This preserves the manual-rearm semantic for operator/risk-triggered entries.

### Config

| Env Var | Default | Purpose |
|---------|---------|---------|
| `HFT_PLATFORM_AUTO_RECOVERY_ENABLED` | `1` | Enable/disable auto-recovery |
| `HFT_PLATFORM_AUTO_RECOVERY_COOLDOWN_S` | `60` | Seconds of all-clear before auto-exit |

Wired through the singleton factory (same pattern as `shadow_mode` — read from env at creation if not explicitly passed).

### Integration in Supervisor Loop

```python
# In services/system.py (or wherever reduce_only_reasons is polled)
reasons = inputs.reduce_only_reasons()
if reasons:
    controller.enter_reduce_only(reason=reasons[0])
    # Also add any additional reasons
    for r in reasons[1:]:
        controller.enter_reduce_only(reason=r)
controller.check_auto_recovery(current_reasons=reasons, now_ns=timebase.now_ns())
```

### Files Changed

- `src/hft_platform/ops/platform_degrade.py` — active-reasons set, `check_auto_recovery()`, modified `enter/exit_reduce_only()`
- `src/hft_platform/services/system.py` — call `check_auto_recovery()` in supervisor loop

### Tests

- `test_active_reasons_accumulate` — multiple `enter_reduce_only()` calls accumulate distinct reasons
- `test_auto_recovery_blocked_by_non_recoverable_reason` — feed clears but reconciliation_drift remains → no recovery
- `test_auto_recovery_after_all_reasons_cleared` — all reasons clear, cooldown elapses → exits reduce-only
- `test_auto_recovery_reset_on_retrigger` — reasons reappear during cooldown → timer resets
- `test_auto_recovery_disabled` — env var `=0` prevents recovery
- `test_exit_reduce_only_clears_all_reasons` — explicit exit clears the set

---

## S5: Observability Improvements

### 5a: `shadow_mode_active` Gauge

The gauge already exists in `MetricsRegistry` (line 314 in `metrics.py`). It just needs to be `.set()` at init.

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
    auto_recovery_enabled=os.getenv("HFT_PLATFORM_AUTO_RECOVERY_ENABLED", "1"),
)
```

### 5c: Price Cap Reject Observability

The existing `risk_reject_total{reason, strategy}` counter already captures price cap rejections with structured labels. Adding a separate `price_cap_rejects_total{symbol}` counter would add a new cardinality dimension (symbol) that `risk_reject_total` doesn't carry — useful for identifying which symbols hit the cap.

**Decision**: Add the symbol label to the existing `risk_reject_total` reason string instead of creating a new counter. The reason already includes the numeric values (`PRICE_EXCEEDS_CAP: 330000000 > 50000000`); appending the symbol keeps cardinality bounded while improving queryability:

```python
return False, f"PRICE_EXCEEDS_CAP({intent.symbol}): {intent.price} > {max_price_scaled}"
```

This is a minor change and avoids metric proliferation.

### Files Changed

- `src/hft_platform/order/shadow.py` — set gauge
- `src/hft_platform/services/bootstrap.py` — startup log
- `src/hft_platform/risk/validators.py` — symbol in rejection reason string

---

## Implementation Order

1. **S1** (ops fix) — deploy Monday morning, zero code changes
2. **S3** (shadow bypass) — singleton factory + allow_intent bypass
3. **S2** (price caps) — all 3 risk paths (Python precise, FastGate/Rust coarse)
4. **S5** (observability) — low risk, aids debugging
5. **S4** (active-reasons + auto-recovery) — most complex, new state model

All code changes (S2-S5) ship in one PR. S1 is an independent ops action.
