# Shadow Trading Fix + Hardening Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Fix three independent blockers preventing shadow order recording on the remote host, then harden the platform so these failures cannot recur.

**Architecture:** The fix operates across three subsystems: (1) risk validators get per-product-type price caps resolved via the existing `SymbolMetadata` object already accessible through `price_codec.provider.metadata`; (2) the `PlatformDegradeController` singleton gets a shadow-mode bypass and active-reasons tracking with auto-recovery; (3) the `ShadowOrderSink` gets proper metric initialization. FastGate (Numba JIT) and RustRiskValidator get their single price cap raised to `max(all configured caps)` since they are coarse pre-filters — precise per-product enforcement stays in the Python `PriceBandValidator`.

**Tech Stack:** Python 3.12, pytest, structlog, Prometheus client, SymbolMetadata (config/symbols.yaml)

**Spec:** `docs/superpowers/specs/2026-03-28-shadow-trading-fix-design.md`

---

### Task 1: Shadow Bypass in PlatformDegradeController (S3)

**Files:**
- Modify: `src/hft_platform/ops/platform_degrade.py:24-32` (constructor), `:111-119` (allow_intent), `:177-185` (singleton factory)
- Test: `tests/unit/test_platform_degrade_shadow.py` (new)

- [ ] **Step 1: Write failing tests for shadow bypass**

Create `tests/unit/test_platform_degrade_shadow.py`:

```python
"""Tests for PlatformDegradeController shadow mode bypass + singleton env wiring."""

from __future__ import annotations

from hft_platform.contracts.strategy import IntentType
from hft_platform.ops.platform_degrade import (
    PlatformDegradeController,
    get_shared_platform_degrade_controller,
    reset_shared_platform_degrade_controller,
)


class TestShadowModeBypass:
    def test_shadow_mode_allows_new_opening_when_reduce_only(self):
        ctrl = PlatformDegradeController(shadow_mode=True)
        ctrl.enter_reduce_only(reason="feed_reconnect_unhealthy")
        assert ctrl.allow_intent(intent_type=IntentType.NEW, opens_risk=True) is True

    def test_shadow_mode_allows_all_intent_types_when_reduce_only(self):
        ctrl = PlatformDegradeController(shadow_mode=True)
        ctrl.enter_reduce_only(reason="feed_reconnect_unhealthy")
        for itype in (IntentType.NEW, IntentType.CANCEL, IntentType.AMEND, IntentType.FORCE_FLAT):
            assert ctrl.allow_intent(intent_type=itype, opens_risk=True) is True

    def test_non_shadow_blocks_new_opening_when_reduce_only(self):
        ctrl = PlatformDegradeController(shadow_mode=False)
        ctrl.enter_reduce_only(reason="feed_reconnect_unhealthy")
        assert ctrl.allow_intent(intent_type=IntentType.NEW, opens_risk=True) is False

    def test_non_shadow_allows_cancel_when_reduce_only(self):
        ctrl = PlatformDegradeController(shadow_mode=False)
        ctrl.enter_reduce_only(reason="feed_reconnect_unhealthy")
        assert ctrl.allow_intent(intent_type=IntentType.CANCEL, opens_risk=True) is True


class TestSingletonEnvWiring:
    def setup_method(self):
        reset_shared_platform_degrade_controller()

    def teardown_method(self):
        reset_shared_platform_degrade_controller()

    def test_singleton_reads_shadow_mode_from_env(self, monkeypatch):
        monkeypatch.setenv("HFT_ORDER_SHADOW_MODE", "1")
        ctrl = get_shared_platform_degrade_controller()
        assert ctrl._shadow_mode is True

    def test_singleton_defaults_shadow_mode_off(self, monkeypatch):
        monkeypatch.delenv("HFT_ORDER_SHADOW_MODE", raising=False)
        ctrl = get_shared_platform_degrade_controller()
        assert ctrl._shadow_mode is False

    def test_singleton_explicit_shadow_mode_overrides_env(self, monkeypatch):
        monkeypatch.setenv("HFT_ORDER_SHADOW_MODE", "1")
        ctrl = get_shared_platform_degrade_controller(shadow_mode=False)
        assert ctrl._shadow_mode is False
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/unit/test_platform_degrade_shadow.py -v`
Expected: FAIL — `PlatformDegradeController.__init__` does not accept `shadow_mode`

- [ ] **Step 3: Implement shadow_mode in PlatformDegradeController**

In `src/hft_platform/ops/platform_degrade.py`:

1. Add `import os` at the top (after existing imports).

2. Modify constructor (line 25):

```python
class PlatformDegradeController:
    def __init__(self, *, metrics: Any | None = None, evidence_writer: Any | None = None,
                 shadow_mode: bool = False) -> None:
        self._shadow_mode = shadow_mode
        self.metrics = metrics or self._default_metrics()
        self.evidence_writer = evidence_writer or get_shared_autonomy_evidence_writer()
        self.reduce_only_active = False
        self.last_transition: AutonomyTransition | None = None
        self._reference_positions: dict[str, int] = {}
        self._reference_close_reservations: dict[str, int] = {}
        self._sync_metrics()
```

3. Add shadow bypass at the top of `allow_intent()` (line 111):

```python
    def allow_intent(self, *, intent_type: IntentType | int | str, opens_risk: bool) -> bool:
        if self._shadow_mode:
            return True
        normalized_intent = self._normalize_intent_type(intent_type)
        # ... rest unchanged
```

4. Update singleton factory `get_shared_platform_degrade_controller()` (line 177):

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

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/unit/test_platform_degrade_shadow.py -v`
Expected: All 7 tests PASS

- [ ] **Step 5: Run existing tests to check for regressions**

Run: `uv run pytest tests/unit/test_autonomy_monitor.py tests/unit/test_force_flat_intent.py tests/unit/test_order_adapter_force_flat.py -v`
Expected: All PASS (no behavior change for non-shadow mode)

- [ ] **Step 6: Commit**

```bash
git add src/hft_platform/ops/platform_degrade.py tests/unit/test_platform_degrade_shadow.py
git commit -m "feat(ops): shadow mode bypasses reduce-only gate (S3)

PlatformDegradeController.allow_intent() now returns True when
shadow_mode=True. Singleton factory reads HFT_ORDER_SHADOW_MODE
env var at creation time. Zero financial risk in shadow mode."
```

---

### Task 2: Per-Product-Type Price Caps in PriceBandValidator (S2 — Python path)

**Files:**
- Modify: `src/hft_platform/risk/validators.py:44-68` (PriceBandValidator)
- Modify: `config/base/strategy_limits.yaml:9-13` (global_defaults)
- Test: `tests/unit/test_price_cap_product_type.py` (new)

- [ ] **Step 1: Write failing tests**

Create `tests/unit/test_price_cap_product_type.py`:

```python
"""Tests for per-product-type price caps in PriceBandValidator (S2)."""

from __future__ import annotations

from unittest.mock import MagicMock

from hft_platform.contracts.strategy import TIF, IntentType, OrderIntent, Side
from hft_platform.core.pricing import PriceCodec, SymbolMetadataPriceScaleProvider
from hft_platform.risk.validators import PriceBandValidator


def _intent(symbol: str = "AAA", price: int = 10000, qty: int = 1) -> OrderIntent:
    return OrderIntent(
        intent_id=1,
        strategy_id="strat",
        symbol=symbol,
        intent_type=IntentType.NEW,
        side=Side.BUY,
        price=price,
        qty=qty,
        tif=TIF.LIMIT,
        target_order_id=None,
        timestamp_ns=0,
    )


def _make_metadata_provider(product_type_map: dict[str, str]) -> SymbolMetadataPriceScaleProvider:
    """Create a provider with a mock SymbolMetadata that returns product types."""
    metadata = MagicMock()
    metadata.price_scale.return_value = 10000
    metadata.product_type.side_effect = lambda sym: product_type_map.get(sym, "")
    provider = SymbolMetadataPriceScaleProvider(metadata=metadata)
    return provider


class TestProductTypePriceCaps:
    def test_futures_price_passes_with_futures_cap(self):
        """TMFD6 at 33000 NTD passes when max_price_cap_futures=50000."""
        cfg = {
            "global_defaults": {
                "max_price_cap": 5000.0,
                "max_price_cap_futures": 50000.0,
            }
        }
        provider = _make_metadata_provider({"TMFD6": "future"})
        v = PriceBandValidator(cfg, price_scale_provider=provider)
        ok, reason = v.check(_intent("TMFD6", price=33000 * 10000))
        assert ok, f"Expected pass, got: {reason}"

    def test_futures_price_rejected_without_futures_cap(self):
        """TMFD6 at 33000 NTD rejected by default 5000 cap."""
        cfg = {"global_defaults": {"max_price_cap": 5000.0}}
        provider = _make_metadata_provider({"TMFD6": "future"})
        v = PriceBandValidator(cfg, price_scale_provider=provider)
        ok, reason = v.check(_intent("TMFD6", price=33000 * 10000))
        assert not ok
        assert "PRICE_EXCEEDS_CAP" in reason

    def test_stock_price_uses_global_cap(self):
        """Stock at 4000 NTD passes global 5000 cap."""
        cfg = {
            "global_defaults": {
                "max_price_cap": 5000.0,
                "max_price_cap_futures": 50000.0,
            }
        }
        provider = _make_metadata_provider({"2330": "stock"})
        v = PriceBandValidator(cfg, price_scale_provider=provider)
        ok, _ = v.check(_intent("2330", price=4000 * 10000))
        assert ok

    def test_stock_price_rejected_above_global_cap(self):
        """Stock at 6000 NTD rejected by global 5000 cap (not lifted by futures cap)."""
        cfg = {
            "global_defaults": {
                "max_price_cap": 5000.0,
                "max_price_cap_futures": 50000.0,
            }
        }
        provider = _make_metadata_provider({"2330": "stock"})
        v = PriceBandValidator(cfg, price_scale_provider=provider)
        ok, reason = v.check(_intent("2330", price=6000 * 10000))
        assert not ok
        assert "PRICE_EXCEEDS_CAP" in reason

    def test_per_symbol_override_beats_product_type(self):
        """Per-symbol cap overrides per-product-type cap."""
        cfg = {
            "global_defaults": {
                "max_price_cap": 5000.0,
                "max_price_cap_futures": 50000.0,
                "max_price_cap_TXFD6": 40000.0,
            }
        }
        provider = _make_metadata_provider({"TXFD6": "future"})
        v = PriceBandValidator(cfg, price_scale_provider=provider)
        # 35000 passes futures cap (50000) but exceeds per-symbol cap (40000)
        ok, reason = v.check(_intent("TXFD6", price=41000 * 10000))
        assert not ok
        assert "PRICE_EXCEEDS_CAP" in reason

    def test_options_cap(self):
        """Options product type uses max_price_cap_options."""
        cfg = {
            "global_defaults": {
                "max_price_cap": 5000.0,
                "max_price_cap_options": 10000.0,
            }
        }
        provider = _make_metadata_provider({"TXO001": "option"})
        v = PriceBandValidator(cfg, price_scale_provider=provider)
        ok, _ = v.check(_intent("TXO001", price=8000 * 10000))
        assert ok

    def test_unknown_product_type_falls_back_to_global(self):
        """Unknown product type uses global cap."""
        cfg = {
            "global_defaults": {
                "max_price_cap": 5000.0,
                "max_price_cap_futures": 50000.0,
            }
        }
        provider = _make_metadata_provider({"UNKNOWN": ""})
        v = PriceBandValidator(cfg, price_scale_provider=provider)
        ok, reason = v.check(_intent("UNKNOWN", price=6000 * 10000))
        assert not ok
        assert "PRICE_EXCEEDS_CAP" in reason

    def test_cap_cached_per_symbol(self):
        """Second call for same symbol uses cache (no re-resolution)."""
        cfg = {
            "global_defaults": {
                "max_price_cap": 5000.0,
                "max_price_cap_futures": 50000.0,
            }
        }
        provider = _make_metadata_provider({"TMFD6": "future"})
        v = PriceBandValidator(cfg, price_scale_provider=provider)
        v.check(_intent("TMFD6", price=33000 * 10000))
        # Cache should have the entry now
        assert "TMFD6" in v._max_price_scaled_cache
        # Call again — should use cached value
        ok, _ = v.check(_intent("TMFD6", price=33000 * 10000))
        assert ok

    def test_reason_includes_symbol(self):
        """Rejection reason includes the symbol name for observability (S5c)."""
        cfg = {"global_defaults": {"max_price_cap": 5000.0}}
        provider = _make_metadata_provider({"TMFD6": "future"})
        v = PriceBandValidator(cfg, price_scale_provider=provider)
        ok, reason = v.check(_intent("TMFD6", price=33000 * 10000))
        assert not ok
        assert "TMFD6" in reason
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/unit/test_price_cap_product_type.py -v`
Expected: FAIL — `PriceBandValidator` has no product-type resolution

- [ ] **Step 3: Implement per-product-type caps in PriceBandValidator**

In `src/hft_platform/risk/validators.py`, replace the `PriceBandValidator.__init__` and `check` methods:

```python
class PriceBandValidator(RiskValidator):
    # Product type string -> config key mapping
    _PRODUCT_CAP_KEYS: dict[str, str] = {
        "future": "max_price_cap_futures",
        "option": "max_price_cap_options",
    }

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self._max_price_cap_raw = float(self.defaults.get("max_price_cap", 5000.0))  # precision-config
        self._tick_size_raw = float(self.defaults.get("tick_size", 0.01))  # precision-config
        self._product_caps_raw: Dict[str, float] = {}
        for ptype, key in self._PRODUCT_CAP_KEYS.items():
            val = self.defaults.get(key)
            if val is not None:
                self._product_caps_raw[ptype] = float(val)
        self._max_price_scaled_cache: Dict[str, int] = {}
        self._tick_size_scaled_cache: Dict[str, int] = {}
        self._band_ticks_cache: Dict[str, int] = {}

    def _resolve_cap_raw(self, symbol: str) -> float:
        """Resolve price cap: per-symbol > per-product-type > global."""
        sym_cap = self.defaults.get(f"max_price_cap_{symbol}")
        if sym_cap is not None:
            return float(sym_cap)
        metadata = getattr(getattr(self.price_codec, "provider", None), "metadata", None)
        if metadata is not None:
            ptype = metadata.product_type(symbol)
            if ptype in self._product_caps_raw:
                return self._product_caps_raw[ptype]
        return self._max_price_cap_raw

    def check(self, intent: OrderIntent) -> Tuple[bool, str]:
        if intent.intent_type == IntentType.CANCEL:
            return True, "OK"

        if intent.price <= 0:
            return False, "PRICE_ZERO_OR_NEG"

        # Fat Finger Protection: Absolute price cap (per-product-type aware)
        scale = self._scale_factor(intent.symbol)
        max_price_scaled = self._max_price_scaled_cache.get(intent.symbol)
        if max_price_scaled is None:
            cap_raw = self._resolve_cap_raw(intent.symbol)
            max_price_scaled = int(cap_raw * scale)
            self._max_price_scaled_cache[intent.symbol] = max_price_scaled

        if intent.price > max_price_scaled:
            return False, f"PRICE_EXCEEDS_CAP({intent.symbol}): {intent.price} > {max_price_scaled}"

        # LOB-relative price band validation (unchanged)
        if self.lob is not None:
            mid_price = self._get_mid_price(intent.symbol)
            if mid_price is not None and mid_price > 0:
                strat_cfg = self.strat_configs.get(intent.strategy_id, {})
                band_ticks = self._band_ticks_cache.get(intent.strategy_id)
                if band_ticks is None:
                    band_ticks = int(strat_cfg.get("price_band_ticks", self.defaults.get("price_band_ticks", 20)))
                    self._band_ticks_cache[intent.strategy_id] = band_ticks
                tick_size_scaled = self._tick_size_scaled_cache.get(intent.symbol)
                if tick_size_scaled is None:
                    tick_size_scaled = int(self._tick_size_raw * scale)
                    self._tick_size_scaled_cache[intent.symbol] = tick_size_scaled

                band_width = band_ticks * tick_size_scaled
                lower_bound = mid_price - band_width
                upper_bound = mid_price + band_width

                if intent.price < lower_bound or intent.price > upper_bound:
                    return False, (
                        f"PRICE_OUTSIDE_BAND: price={intent.price} mid={mid_price} band=[{lower_bound}, {upper_bound}]"
                    )

        return True, "OK"
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/unit/test_price_cap_product_type.py -v`
Expected: All 9 tests PASS

- [ ] **Step 5: Run existing validator tests for regressions**

Run: `uv run pytest tests/unit/test_risk_validators.py tests/unit/test_risk_validators_extended.py tests/unit/test_risk_validators_comprehensive.py -v`
Expected: All PASS. Note: existing `test_price_band_validator` uses no SymbolMetadata → hits global cap fallback → behavior unchanged.

- [ ] **Step 6: Add futures config to strategy_limits.yaml**

In `config/base/strategy_limits.yaml`, add two lines after `max_daily_loss` in `global_defaults`:

```yaml
global_defaults:
  max_notional: 500000000            # 50K NTD x10000
  per_symbol_max_notional: 5000000000 # Single symbol = full capital for futures
  max_position_lots: 3               # Max 3 lots (OpMM_TX + OpMM_TMF + CBS concurrent)
  max_daily_loss: 50000000           # 5,000 NTD x10000 (triggers rejection)
  max_price_cap: 5000.0              # Stocks: max 5,000 NTD per share
  max_price_cap_futures: 50000.0     # Futures: max 50,000 NTD (covers TMFD6/TXFD6/MXFD6)
  max_price_cap_options: 10000.0     # TXO options: max 10,000 NTD
```

- [ ] **Step 7: Commit**

```bash
git add src/hft_platform/risk/validators.py config/base/strategy_limits.yaml tests/unit/test_price_cap_product_type.py
git commit -m "feat(risk): per-product-type price caps in PriceBandValidator (S2)

Resolve price cap via SymbolMetadata.product_type(): per-symbol >
per-product-type > global fallback. Cached per-symbol, zero hot-path
alloc. Rejection reason now includes symbol name. Config: add
max_price_cap_futures=50000, max_price_cap_options=10000 to
strategy_limits.yaml global_defaults."
```

---

### Task 3: Raise FastGate + RustValidator Caps (S2 — coarse paths)

**Files:**
- Modify: `src/hft_platform/risk/engine.py:171-206` (_init_fast_gate), `:208-245` (_init_rust_validator)
- Test: `tests/unit/test_risk_coarse_gate_caps.py` (new)

- [ ] **Step 1: Write failing tests**

Create `tests/unit/test_risk_coarse_gate_caps.py`:

```python
"""Tests for FastGate + RustValidator using max(all configured caps) (S2)."""

from __future__ import annotations

import os
from unittest.mock import MagicMock, patch

from hft_platform.risk.engine import RiskEngine


def _make_config(global_defaults: dict | None = None) -> dict:
    base = {
        "global_defaults": {
            "max_price_cap": 5000.0,
            **(global_defaults or {}),
        },
        "strategies": {},
    }
    return base


def _write_config(tmp_path, config: dict) -> str:
    import yaml

    path = tmp_path / "strategy_limits.yaml"
    path.write_text(yaml.dump(config))
    return str(path)


class TestFastGateMaxCap:
    def test_fast_gate_uses_max_of_all_caps(self, tmp_path, monkeypatch):
        """FastGate should use max(5000, 50000, 10000) = 50000."""
        monkeypatch.setenv("HFT_RISK_FAST_GATE", "1")
        config = _make_config({
            "max_price_cap_futures": 50000.0,
            "max_price_cap_options": 10000.0,
        })
        config_path = _write_config(tmp_path, config)
        q1 = MagicMock()
        q2 = MagicMock()
        engine = RiskEngine(config_path, q1, q2)
        gate = engine._fast_gate
        assert gate is not None
        # max_price should be 50000 * 10000 = 500_000_000
        expected_cap = int(50000.0 * 10000)
        assert gate._max_price == expected_cap

    def test_fast_gate_without_product_caps_uses_global(self, tmp_path, monkeypatch):
        """FastGate with no product caps uses global 5000."""
        monkeypatch.setenv("HFT_RISK_FAST_GATE", "1")
        config = _make_config()
        config_path = _write_config(tmp_path, config)
        q1 = MagicMock()
        q2 = MagicMock()
        engine = RiskEngine(config_path, q1, q2)
        gate = engine._fast_gate
        assert gate is not None
        expected_cap = int(5000.0 * 10000)
        assert gate._max_price == expected_cap
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/unit/test_risk_coarse_gate_caps.py -v`
Expected: FAIL — FastGate cap is 5000 * 10000, not 50000 * 10000

- [ ] **Step 3: Implement max-of-all-caps in _init_fast_gate**

In `src/hft_platform/risk/engine.py`, modify `_init_fast_gate()` around line 186:

Replace:
```python
        max_price_cap = float(defaults.get("max_price_cap", 5000.0))  # precision-config
        max_price_scaled = int(max_price_cap * max(1, scale))
```

With:
```python
        max_price_cap = float(defaults.get("max_price_cap", 5000.0))  # precision-config
        all_caps = [max_price_cap]
        for key in ("max_price_cap_futures", "max_price_cap_options"):
            val = defaults.get(key)
            if val is not None:
                all_caps.append(float(val))
        coarse_cap = max(all_caps)
        max_price_scaled = int(coarse_cap * max(1, scale))
```

- [ ] **Step 4: Implement max-of-all-caps in _init_rust_validator**

In `src/hft_platform/risk/engine.py`, modify `_init_rust_validator()` around line 216:

Replace:
```python
            max_price_cap_raw = float(defaults.get("max_price_cap", 5000.0))  # precision-config
```

With:
```python
            max_price_cap_raw = float(defaults.get("max_price_cap", 5000.0))  # precision-config
            all_caps = [max_price_cap_raw]
            for key in ("max_price_cap_futures", "max_price_cap_options"):
                val = defaults.get(key)
                if val is not None:
                    all_caps.append(float(val))
            max_price_cap_raw = max(all_caps)
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `uv run pytest tests/unit/test_risk_coarse_gate_caps.py -v`
Expected: All PASS

- [ ] **Step 6: Run existing risk engine tests for regressions**

Run: `uv run pytest tests/unit/test_risk_engine.py tests/unit/test_risk_fast_gate.py tests/unit/test_risk_engine_behavior.py -v`
Expected: All PASS

- [ ] **Step 7: Commit**

```bash
git add src/hft_platform/risk/engine.py tests/unit/test_risk_coarse_gate_caps.py
git commit -m "feat(risk): FastGate + RustValidator use max(all caps) (S2)

Both coarse pre-filters now use the highest configured price cap
across all product types. Precise per-product-type enforcement
remains in the Python PriceBandValidator downstream."
```

---

### Task 4: Active-Reasons Model in PlatformDegradeController (S4 — part 1)

**Files:**
- Modify: `src/hft_platform/ops/platform_degrade.py:24-103` (constructor, enter, exit)
- Modify: `tests/unit/test_platform_degrade_shadow.py` (extend)

- [ ] **Step 1: Write failing tests for active-reasons tracking**

Append to `tests/unit/test_platform_degrade_shadow.py`:

```python
class TestActiveReasons:
    def test_reasons_accumulate_on_multiple_entries(self):
        ctrl = PlatformDegradeController()
        ctrl.enter_reduce_only(reason="feed_reconnect_unhealthy")
        ctrl.enter_reduce_only(reason="reconciliation_drift")
        assert ctrl._active_reasons == {"feed_reconnect_unhealthy", "reconciliation_drift"}

    def test_first_entry_activates_reduce_only(self):
        ctrl = PlatformDegradeController()
        ctrl.enter_reduce_only(reason="feed_reconnect_unhealthy")
        assert ctrl.reduce_only_active is True

    def test_second_entry_stays_active_adds_reason(self):
        ctrl = PlatformDegradeController()
        ctrl.enter_reduce_only(reason="feed_reconnect_unhealthy")
        ctrl.enter_reduce_only(reason="reconciliation_drift")
        assert ctrl.reduce_only_active is True
        assert len(ctrl._active_reasons) == 2

    def test_exit_clears_all_reasons(self):
        ctrl = PlatformDegradeController()
        ctrl.enter_reduce_only(reason="feed_reconnect_unhealthy")
        ctrl.enter_reduce_only(reason="reconciliation_drift")
        ctrl.exit_reduce_only(reason="operator_manual")
        assert ctrl._active_reasons == set()
        assert ctrl.reduce_only_active is False

    def test_duplicate_reason_not_double_counted(self):
        ctrl = PlatformDegradeController()
        ctrl.enter_reduce_only(reason="feed_reconnect_unhealthy")
        ctrl.enter_reduce_only(reason="feed_reconnect_unhealthy")
        assert ctrl._active_reasons == {"feed_reconnect_unhealthy"}
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/unit/test_platform_degrade_shadow.py::TestActiveReasons -v`
Expected: FAIL — `_active_reasons` attribute does not exist

- [ ] **Step 3: Add active-reasons set to PlatformDegradeController**

In `src/hft_platform/ops/platform_degrade.py`:

1. Add `_active_reasons` to constructor:

```python
    def __init__(self, *, metrics: Any | None = None, evidence_writer: Any | None = None,
                 shadow_mode: bool = False) -> None:
        self._shadow_mode = shadow_mode
        self.metrics = metrics or self._default_metrics()
        self.evidence_writer = evidence_writer or get_shared_autonomy_evidence_writer()
        self.reduce_only_active = False
        self.last_transition: AutonomyTransition | None = None
        self._reference_positions: dict[str, int] = {}
        self._reference_close_reservations: dict[str, int] = {}
        self._active_reasons: set[str] = set()
        self._sync_metrics()
```

2. Add reason tracking in `enter_reduce_only()`:

```python
    def enter_reduce_only(self, *, reason: str) -> AutonomyTransition:
        self._active_reasons.add(reason)
        if self.reduce_only_active and self.last_transition is not None:
            logger.info("platform_reduce_only_reason_added", reason=reason,
                        active_reasons=sorted(self._active_reasons))
            return self.last_transition

        transition = AutonomyTransition.enter_platform_reduce_only(
            reason,
            from_mode=AutonomyMode.NORMAL if not self.reduce_only_active else AutonomyMode.PLATFORM_REDUCE_ONLY,
        )
        self.reduce_only_active = True
        self.last_transition = transition
        self._sync_metrics()
        logger.warning(
            "platform_reduce_only_entered",
            reason=reason,
            from_mode=transition.from_mode.value,
            to_mode=transition.to_mode.value,
            manual_rearm_required=transition.manual_rearm_required,
            active_reasons=sorted(self._active_reasons),
        )
        if self.evidence_writer is not None:
            self.evidence_writer.record_transition(
                scope="platform",
                mode=transition.to_mode.value,
                reason=transition.reason,
                manual_rearm_required=transition.manual_rearm_required,
            )
        if self.metrics is not None:
            transition.record_transition(self.metrics)
        return transition
```

3. Clear reasons in `exit_reduce_only()`:

```python
    def exit_reduce_only(self, *, reason: str) -> AutonomyTransition:
        if not self.reduce_only_active:
            return AutonomyTransition.enter_platform_reduce_only(
                reason,
                from_mode=AutonomyMode.NORMAL,
            )

        transition = AutonomyTransition.exit_platform_reduce_only(
            reason,
            from_mode=AutonomyMode.PLATFORM_REDUCE_ONLY,
        )
        self.reduce_only_active = False
        self.last_transition = transition
        self._reference_positions = {}
        self._reference_close_reservations = {}
        self._active_reasons.clear()
        self._sync_metrics()
        logger.info(
            "platform_reduce_only_exited",
            reason=reason,
            from_mode=transition.from_mode.value,
            to_mode=transition.to_mode.value,
        )
        if self.evidence_writer is not None:
            self.evidence_writer.record_transition(
                scope="platform",
                mode=transition.to_mode.value,
                reason=transition.reason,
                manual_rearm_required=False,
            )
        if self.metrics is not None:
            transition.record_transition(self.metrics)
        return transition
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/unit/test_platform_degrade_shadow.py -v`
Expected: All tests PASS (both shadow + active-reasons)

- [ ] **Step 5: Run regression tests**

Run: `uv run pytest tests/unit/test_autonomy_monitor.py tests/unit/test_force_flat_intent.py -v`
Expected: All PASS

- [ ] **Step 6: Commit**

```bash
git add src/hft_platform/ops/platform_degrade.py tests/unit/test_platform_degrade_shadow.py
git commit -m "feat(ops): active-reasons set in PlatformDegradeController (S4 part 1)

enter_reduce_only() now accumulates reasons in _active_reasons set.
Second+ calls record the reason without re-triggering the transition.
exit_reduce_only() clears all reasons. Foundation for auto-recovery."
```

---

### Task 5: Auto-Recovery Logic (S4 — part 2)

**Files:**
- Modify: `src/hft_platform/ops/platform_degrade.py` (add check_auto_recovery, update factory)
- Modify: `src/hft_platform/services/system.py:408-414` (_update_platform_degrade_state)
- Modify: `tests/unit/test_platform_degrade_shadow.py` (extend)

- [ ] **Step 1: Write failing tests for auto-recovery**

Append to `tests/unit/test_platform_degrade_shadow.py`:

```python
from hft_platform.ops.platform_degrade import _AUTO_RECOVERABLE_REASONS


class TestAutoRecovery:
    def test_auto_recovery_after_all_reasons_cleared(self):
        ctrl = PlatformDegradeController(auto_recovery_enabled=True, auto_recovery_cooldown_s=60)
        ctrl.enter_reduce_only(reason="feed_reconnect_unhealthy")
        # Reasons cleared, start cooldown
        ctrl.check_auto_recovery(current_reasons=[], now_ns=1_000_000_000)
        assert ctrl.reduce_only_active is True  # still in cooldown
        # Cooldown elapses (60s = 60_000_000_000 ns)
        recovered = ctrl.check_auto_recovery(current_reasons=[], now_ns=61_000_000_001)
        assert recovered is True
        assert ctrl.reduce_only_active is False

    def test_auto_recovery_blocked_by_non_recoverable_reason(self):
        ctrl = PlatformDegradeController(auto_recovery_enabled=True, auto_recovery_cooldown_s=60)
        ctrl.enter_reduce_only(reason="feed_reconnect_unhealthy")
        ctrl.enter_reduce_only(reason="reconciliation_drift")
        # Feed clears but reconciliation_drift remains
        recovered = ctrl.check_auto_recovery(current_reasons=[], now_ns=1_000_000_000)
        assert recovered is False
        # Even after cooldown
        recovered = ctrl.check_auto_recovery(current_reasons=[], now_ns=61_000_000_001)
        assert recovered is False
        assert ctrl.reduce_only_active is True

    def test_auto_recovery_reset_on_retrigger(self):
        ctrl = PlatformDegradeController(auto_recovery_enabled=True, auto_recovery_cooldown_s=60)
        ctrl.enter_reduce_only(reason="feed_reconnect_unhealthy")
        # Reasons cleared, start cooldown at t=1s
        ctrl.check_auto_recovery(current_reasons=[], now_ns=1_000_000_000)
        # Reason re-appears at t=30s
        ctrl.check_auto_recovery(current_reasons=["feed_reconnect_unhealthy"], now_ns=30_000_000_000)
        # Clears again at t=50s
        ctrl.check_auto_recovery(current_reasons=[], now_ns=50_000_000_000)
        # 60s from t=50s would be t=110s — should NOT recover at t=70s
        recovered = ctrl.check_auto_recovery(current_reasons=[], now_ns=70_000_000_000)
        assert recovered is False
        # Should recover at t=111s (60s after re-clear)
        recovered = ctrl.check_auto_recovery(current_reasons=[], now_ns=111_000_000_000)
        assert recovered is True

    def test_auto_recovery_disabled(self):
        ctrl = PlatformDegradeController(auto_recovery_enabled=False, auto_recovery_cooldown_s=60)
        ctrl.enter_reduce_only(reason="feed_reconnect_unhealthy")
        recovered = ctrl.check_auto_recovery(current_reasons=[], now_ns=999_000_000_000)
        assert recovered is False
        assert ctrl.reduce_only_active is True

    def test_auto_recovery_clears_auto_recoverable_reasons_from_set(self):
        ctrl = PlatformDegradeController(auto_recovery_enabled=True, auto_recovery_cooldown_s=1)
        ctrl.enter_reduce_only(reason="feed_reconnect_unhealthy")
        # Input no longer reports feed_reconnect_unhealthy
        ctrl.check_auto_recovery(current_reasons=[], now_ns=1_000_000_000)
        assert "feed_reconnect_unhealthy" not in ctrl._active_reasons

    def test_auto_recovery_not_triggered_when_not_active(self):
        ctrl = PlatformDegradeController(auto_recovery_enabled=True, auto_recovery_cooldown_s=1)
        recovered = ctrl.check_auto_recovery(current_reasons=[], now_ns=999_000_000_000)
        assert recovered is False

    def test_auto_recoverable_reasons_are_feed_related(self):
        assert "feed_reconnect_unhealthy" in _AUTO_RECOVERABLE_REASONS
        assert "feed_gap_exceeded" in _AUTO_RECOVERABLE_REASONS
        assert "reconciliation_drift" not in _AUTO_RECOVERABLE_REASONS
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/unit/test_platform_degrade_shadow.py::TestAutoRecovery -v`
Expected: FAIL — `check_auto_recovery` method does not exist

- [ ] **Step 3: Implement auto-recovery**

In `src/hft_platform/ops/platform_degrade.py`:

1. Add module-level constant (after `_AUTONOMY_MODE_VALUES`):

```python
_AUTO_RECOVERABLE_REASONS: frozenset[str] = frozenset({
    "feed_reconnect_unhealthy",
    "feed_gap_exceeded",
})
```

2. Add auto-recovery params to constructor:

```python
    def __init__(self, *, metrics: Any | None = None, evidence_writer: Any | None = None,
                 shadow_mode: bool = False,
                 auto_recovery_enabled: bool = True,
                 auto_recovery_cooldown_s: int = 60) -> None:
        self._shadow_mode = shadow_mode
        self._auto_recovery_enabled = auto_recovery_enabled
        self._auto_recovery_cooldown_s = auto_recovery_cooldown_s
        self._auto_recovery_cooldown_ns = int(auto_recovery_cooldown_s * 1_000_000_000)
        self._recovery_started_ns: int = 0
        # ... rest of existing init
```

3. Add `check_auto_recovery()` method (after `exit_reduce_only`):

```python
    def check_auto_recovery(self, *, current_reasons: list[str], now_ns: int) -> bool:
        """Check if auto-recovery should trigger. Called from supervisor loop.

        Returns True if recovery was performed.
        """
        if not self.reduce_only_active or not self._auto_recovery_enabled:
            return False

        # Sync: remove auto-recoverable reasons that inputs no longer report
        input_reason_set = set(current_reasons)
        auto_recoverable_active = self._active_reasons & _AUTO_RECOVERABLE_REASONS
        cleared = auto_recoverable_active - input_reason_set
        if cleared:
            self._active_reasons -= cleared
            logger.info("auto_recovery_reasons_cleared", cleared=sorted(cleared),
                        remaining=sorted(self._active_reasons))

        # If ANY non-auto-recoverable reason remains, block auto-recovery
        non_recoverable = self._active_reasons - _AUTO_RECOVERABLE_REASONS
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

4. Update singleton factory to pass auto-recovery config:

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
            _auto_enabled = os.getenv("HFT_PLATFORM_AUTO_RECOVERY_ENABLED", "1") == "1"
            try:
                _cooldown = int(os.getenv("HFT_PLATFORM_AUTO_RECOVERY_COOLDOWN_S", "60"))
            except ValueError:
                _cooldown = 60
            _shared_controller = PlatformDegradeController(
                metrics=metrics, shadow_mode=_shadow,
                auto_recovery_enabled=_auto_enabled,
                auto_recovery_cooldown_s=_cooldown,
            )
        elif metrics is not None and _shared_controller.metrics is None:
            _shared_controller.metrics = metrics
            _shared_controller._sync_metrics()
        return _shared_controller
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/unit/test_platform_degrade_shadow.py -v`
Expected: All tests PASS

- [ ] **Step 5: Wire auto-recovery into supervisor loop**

In `src/hft_platform/services/system.py`, modify `_update_platform_degrade_state()` (line 408):

```python
    def _update_platform_degrade_state(self) -> None:
        controller = getattr(self, "platform_degrade_controller", None)
        inputs = getattr(self, "platform_degrade_inputs", None)
        if controller is None or inputs is None:
            return
        reasons = inputs.reduce_only_reasons()
        for reason in reasons:
            controller.enter_reduce_only(reason=reason)
        controller.check_auto_recovery(
            current_reasons=reasons, now_ns=timebase.now_ns(),
        )
```

- [ ] **Step 6: Run system lifecycle tests**

Run: `uv run pytest tests/unit/test_system_lifecycle.py tests/unit/test_autonomy_monitor.py -v`
Expected: All PASS

- [ ] **Step 7: Commit**

```bash
git add src/hft_platform/ops/platform_degrade.py src/hft_platform/services/system.py tests/unit/test_platform_degrade_shadow.py
git commit -m "feat(ops): auto-recovery from reduce-only with active-reasons safety (S4)

check_auto_recovery() only exits reduce-only when ALL reasons are
cleared for 60s (configurable). Non-auto-recoverable reasons (e.g.
reconciliation_drift) block auto-recovery permanently. Wired into
supervisor loop. Config: HFT_PLATFORM_AUTO_RECOVERY_ENABLED=1,
HFT_PLATFORM_AUTO_RECOVERY_COOLDOWN_S=60."
```

---

### Task 6: Observability — shadow_mode_active gauge + startup log (S5)

**Files:**
- Modify: `src/hft_platform/order/shadow.py:27-38` (ShadowOrderSink.__init__)
- Modify: `src/hft_platform/services/bootstrap.py:208-222` (after validate_shadow_lock)
- Modify: `tests/unit/test_shadow_order.py` (extend)

- [ ] **Step 1: Write failing test for shadow_mode_active metric**

Append to `tests/unit/test_shadow_order.py`:

```python
class TestShadowModeMetric:
    def test_enabled_sets_metric_to_1(self, monkeypatch):
        """shadow_mode_active gauge should be 1 when enabled."""
        mock_metrics = MagicMock()
        mock_gauge = MagicMock()
        mock_metrics.shadow_mode_active = mock_gauge
        monkeypatch.setattr(
            "hft_platform.order.shadow._get_metrics", lambda: mock_metrics
        )
        ShadowOrderSink(enabled=True)
        mock_gauge.set.assert_called_once_with(1)

    def test_disabled_sets_metric_to_0(self, monkeypatch):
        """shadow_mode_active gauge should be 0 when disabled."""
        mock_metrics = MagicMock()
        mock_gauge = MagicMock()
        mock_metrics.shadow_mode_active = mock_gauge
        monkeypatch.setattr(
            "hft_platform.order.shadow._get_metrics", lambda: mock_metrics
        )
        ShadowOrderSink(enabled=False)
        mock_gauge.set.assert_called_once_with(0)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/unit/test_shadow_order.py::TestShadowModeMetric -v`
Expected: FAIL — `set()` not called on gauge

- [ ] **Step 3: Implement metric initialization in ShadowOrderSink**

In `src/hft_platform/order/shadow.py`, modify `__init__`:

```python
class ShadowOrderSink:
    """Intercepts orders for shadow logging without broker execution."""

    __slots__ = ("_enabled", "_counter", "_writer")

    def __init__(self, enabled: bool | None = None, writer: ShadowOrderWriter | None = None):
        if enabled is not None:
            self._enabled = enabled
        else:
            self._enabled = os.getenv("HFT_ORDER_SHADOW_MODE", "0") == "1"
        self._counter = 0
        self._writer = writer
        # Set shadow_mode_active gauge
        metrics = _get_metrics()
        if metrics and hasattr(metrics, "shadow_mode_active"):
            metrics.shadow_mode_active.set(1 if self._enabled else 0)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/unit/test_shadow_order.py -v`
Expected: All PASS

- [ ] **Step 5: Add startup log in bootstrap**

In `src/hft_platform/services/bootstrap.py`, add after `validate_shadow_lock()` function (after line 222):

```python
def log_shadow_config_summary() -> None:
    """Log all shadow-relevant env vars at startup for debugging."""
    logger.info(
        "shadow_config_summary",
        shadow_mode=os.getenv("HFT_ORDER_SHADOW_MODE", "0"),
        gateway_enabled=os.getenv("HFT_GATEWAY_ENABLED", "0"),
        order_mode=os.getenv("HFT_ORDER_MODE", "sim"),
        auto_recovery_enabled=os.getenv("HFT_PLATFORM_AUTO_RECOVERY_ENABLED", "1"),
    )
```

Then call it from `SystemBootstrapper.build()` (wherever `validate_shadow_lock()` is called — find with grep and add `log_shadow_config_summary()` immediately after).

- [ ] **Step 6: Run existing tests**

Run: `uv run pytest tests/unit/test_shadow_order.py tests/unit/test_bootstrap_build_flow.py -v`
Expected: All PASS

- [ ] **Step 7: Commit**

```bash
git add src/hft_platform/order/shadow.py src/hft_platform/services/bootstrap.py tests/unit/test_shadow_order.py
git commit -m "feat(obs): set shadow_mode_active gauge + startup config log (S5)

ShadowOrderSink now sets shadow_mode_active gauge at init.
New log_shadow_config_summary() logs HFT_ORDER_SHADOW_MODE,
HFT_GATEWAY_ENABLED, HFT_ORDER_MODE at boot for faster debugging."
```

---

### Task 7: Final Validation

**Files:** None (verification only)

- [ ] **Step 1: Run full test suite**

Run: `uv run pytest tests/unit/ -x -q --timeout=120`
Expected: All PASS, no regressions

- [ ] **Step 2: Run lint + type check**

Run: `uv run ruff check src/hft_platform/ops/platform_degrade.py src/hft_platform/risk/validators.py src/hft_platform/risk/engine.py src/hft_platform/order/shadow.py src/hft_platform/services/system.py src/hft_platform/services/bootstrap.py`
Expected: Clean (no errors)

Run: `uv run mypy src/hft_platform/ops/platform_degrade.py src/hft_platform/risk/validators.py src/hft_platform/risk/engine.py src/hft_platform/order/shadow.py`
Expected: Clean or pre-existing errors only

- [ ] **Step 3: Verify config is valid YAML**

Run: `python -c "import yaml; yaml.safe_load(open('config/base/strategy_limits.yaml'))"`
Expected: No errors

- [ ] **Step 4: Review all changes**

Run: `git diff main --stat` to verify only expected files were touched:
- `src/hft_platform/ops/platform_degrade.py`
- `src/hft_platform/risk/validators.py`
- `src/hft_platform/risk/engine.py`
- `src/hft_platform/order/shadow.py`
- `src/hft_platform/services/system.py`
- `src/hft_platform/services/bootstrap.py`
- `config/base/strategy_limits.yaml`
- `tests/unit/test_platform_degrade_shadow.py` (new)
- `tests/unit/test_price_cap_product_type.py` (new)
- `tests/unit/test_risk_coarse_gate_caps.py` (new)
- `tests/unit/test_shadow_order.py` (modified)

---

### Task 8: Update Documentation (S5b references)

**Files:**
- Modify: `docs/operations/env-vars-reference.md`

- [ ] **Step 1: Add new env vars to reference**

Add to `docs/operations/env-vars-reference.md`:

```markdown
| `HFT_PLATFORM_AUTO_RECOVERY_ENABLED` | `1` | Enable auto-recovery from feed-related reduce-only |
| `HFT_PLATFORM_AUTO_RECOVERY_COOLDOWN_S` | `60` | Seconds of all-clear before auto-exit reduce-only |
```

- [ ] **Step 2: Commit**

```bash
git add docs/operations/env-vars-reference.md
git commit -m "docs: add auto-recovery env vars to reference"
```
