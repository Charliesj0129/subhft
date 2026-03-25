# Live Feasibility Validation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build 6 modules for micro-TAIEX (TMF) live feasibility validation — intraday PnL watermark, slippage tracking, daily report, TCA, liquidity gate, and statistical scorecard.

**Architecture:** Extend existing `DailyLossLimitValidator` for PnL watermark (no parallel authority). Add `decision_mid` field to `OrderIntent` for slippage correlation via `order_id_map`. New ClickHouse tables for slippage/reports/gate events. CLI tools for offline TCA and scorecard analysis.

**Tech Stack:** Python 3.12, ClickHouse, structlog, Prometheus, Telegram, scipy (offline only)

**Spec:** `docs/superpowers/specs/2026-03-24-live-feasibility-validation-design.md`

---

## File Map

### New Files

| File | Responsibility |
|------|---------------|
| `src/hft_platform/execution/slippage_tracker.py` | Per-fill slippage computation + ClickHouse persistence |
| `src/hft_platform/risk/liquidity_gate.py` | Spread-based order rejection validator |
| `src/hft_platform/ops/daily_pnl_report.py` | EOD PnL aggregation + Telegram daily summary |
| `src/hft_platform/analytics/__init__.py` | Analytics package init |
| `src/hft_platform/analytics/queries.py` | Shared ClickHouse aggregation queries |
| `src/hft_platform/cli/_tca.py` | `hft tca report` CLI command |
| `src/hft_platform/cli/_feasibility.py` | `hft feasibility report` CLI command |
| `src/hft_platform/migrations/clickhouse/20260325_001_add_slippage_records.sql` | Slippage table DDL |
| `src/hft_platform/migrations/clickhouse/20260325_002_add_daily_reports.sql` | Daily reports table DDL |
| `src/hft_platform/migrations/clickhouse/20260325_003_add_liquidity_gate_events.sql` | Liquidity gate events table DDL |
| `tests/unit/test_intraday_pnl_watermark.py` | Tests for Module 1 |
| `tests/unit/test_slippage_tracker.py` | Tests for Module 2 |
| `tests/unit/test_liquidity_gate.py` | Tests for Module 5 |
| `tests/unit/test_daily_pnl_report.py` | Tests for Module 3 |
| `tests/unit/test_tca_report.py` | Tests for Module 4 |
| `tests/unit/test_feasibility_scorecard.py` | Tests for Module 6 |

### Modified Files

| File | Change |
|------|--------|
| `src/hft_platform/contracts/strategy.py:33-57` | Add `decision_mid: int = 0` to `OrderIntent` |
| `src/hft_platform/risk/validators.py:175-290` | Extend `DailyLossLimitValidator` with watermark/soft-limit/drawdown |
| `src/hft_platform/risk/engine.py:91-130` | Add `lob_engine` parameter, wire `LiquidityGateValidator` |
| `src/hft_platform/order/adapter.py:87-125` | Add `_decision_mid_map` for slippage correlation |
| `src/hft_platform/ops/autonomy.py:7-23` | Add reason codes `pnl_soft_limit`, `pnl_peak_drawdown` |
| `src/hft_platform/services/bootstrap.py:860-870` | Pass `lob_engine` to `RiskEngine`, wire slippage tracker |
| `src/hft_platform/recorder/worker.py:265-310` | Add `slippage_records`, `daily_reports`, `liquidity_gate_events` batchers |
| `src/hft_platform/cli/_parser.py` | Register `tca` and `feasibility` subcommands |
| `config/base/strategy_limits.yaml` | Add `intraday_pnl` and `liquidity_gate` sections |

---

## Phase A: Pre-Launch (Modules 1 + 2)

### Task 1: Config — Add intraday_pnl thresholds to strategy_limits.yaml

**Files:**
- Modify: `config/base/strategy_limits.yaml`
- Test: manual — verify YAML loads

- [ ] **Step 1: Read current config**

Read `config/base/strategy_limits.yaml` to understand existing structure.

- [ ] **Step 2: Add intraday_pnl section**

Append to `config/base/strategy_limits.yaml`:

```yaml
intraday_pnl:
  soft_limit_ntd: 500
  hard_limit_ntd: 1000
  peak_drawdown_pct: 0.40
  soft_recovery_ntd: 300
  drawdown_recovery_pct: 0.20
  soft_limit_cooldown_s: 60
  peak_drawdown_min_peak_ntd: 200
```

- [ ] **Step 3: Verify YAML validity**

Run: `python -c "import yaml; yaml.safe_load(open('config/base/strategy_limits.yaml'))"`
Expected: No errors

- [ ] **Step 4: Commit**

```bash
git add config/base/strategy_limits.yaml
git commit -m "feat(risk): add intraday_pnl watermark config to strategy_limits"
```

---

### Task 2: Add autonomy reason codes

**Files:**
- Modify: `src/hft_platform/ops/autonomy.py:7-23`
- Test: `tests/unit/test_autonomy_state_machine.py` (existing, verify no regression)

- [ ] **Step 1: Read current reason codes**

Read `src/hft_platform/ops/autonomy.py` lines 1-30.

- [ ] **Step 2: Add new reason codes**

Add `"pnl_soft_limit"` and `"pnl_peak_drawdown"` to `_ALLOWED_REASON_CODES` frozenset.

- [ ] **Step 3: Run existing autonomy tests**

Run: `uv run pytest tests/unit/test_autonomy_state_machine.py -v`
Expected: All existing tests PASS

- [ ] **Step 4: Commit**

```bash
git add src/hft_platform/ops/autonomy.py
git commit -m "feat(ops): add pnl_soft_limit and pnl_peak_drawdown autonomy reason codes"
```

---

### Task 3: Module 1 — Extend DailyLossLimitValidator with watermark + soft limit

**Files:**
- Modify: `src/hft_platform/risk/validators.py:175-290`
- Test: `tests/unit/test_intraday_pnl_watermark.py`

- [ ] **Step 1: Write failing tests for soft limit**

Create `tests/unit/test_intraday_pnl_watermark.py`:

```python
"""Tests for DailyLossLimitValidator intraday watermark extensions."""
import pytest
from unittest.mock import patch
from hft_platform.risk.validators import DailyLossLimitValidator
from hft_platform.contracts.strategy import OrderIntent, Side, IntentType, TIF


def _make_intent(strategy_id="TEST", symbol="TMFD6", side=Side.BUY, price=200000000, qty=1):
    return OrderIntent(
        intent_id=1, strategy_id=strategy_id, symbol=symbol,
        intent_type=IntentType.NEW, side=side, price=price, qty=qty,
    )


def _make_validator(config=None):
    """Create validator with intraday_pnl config.

    Unit conversion: TMF price_scale=10000, point_value=10.
    1 NTD = price_scale / point_value = 1000 scaled units.
    So: 500 NTD = 500_000, 1000 NTD = 1_000_000 in scaled-int.
    """
    defaults = {
        "max_daily_loss": 1_000_000,  # 1000 NTD = 1000 * 1000 scaled units
    }
    intraday_pnl = {
        "soft_limit_ntd": 500,
        "hard_limit_ntd": 1000,
        "peak_drawdown_pct": 0.40,
        "soft_recovery_ntd": 300,
        "drawdown_recovery_pct": 0.20,
        "soft_limit_cooldown_s": 60,
        "peak_drawdown_min_peak_ntd": 200,
        "price_scale": 10000,
        "point_value": 10,
    }
    cfg = config or {}
    cfg.setdefault("global_defaults", defaults)
    cfg.setdefault("intraday_pnl", intraday_pnl)
    v = DailyLossLimitValidator(cfg, None)
    return v


class TestSoftLimit:
    def test_allows_order_above_soft_limit(self):
        v = _make_validator()
        # -400 NTD = -400_000 scaled, above soft limit -500 NTD = -500_000
        v.record_pnl("TEST", -400_000)
        ok, reason = v.check(_make_intent())
        assert ok is True

    def test_soft_limit_triggers_reduce_only_flag(self):
        v = _make_validator()
        # -550 NTD = -550_000 scaled, below soft limit -500_000
        v.record_pnl("TEST", -550_000)
        ok, reason = v.check(_make_intent())
        assert ok is False
        assert "SOFT_LIMIT" in reason
        assert v.soft_limit_active is True

    def test_soft_limit_allows_cancel(self):
        v = _make_validator()
        v.record_pnl("TEST", -550_000)
        cancel_intent = _make_intent()
        cancel_intent.intent_type = IntentType.CANCEL
        ok, reason = v.check(cancel_intent)
        assert ok is True

    def test_soft_limit_allows_force_flat(self):
        v = _make_validator()
        v.record_pnl("TEST", -550_000)
        flat_intent = _make_intent()
        flat_intent.intent_type = IntentType.FORCE_FLAT
        ok, reason = v.check(flat_intent)
        assert ok is True

    def test_soft_limit_recovery_blocked_by_cooldown(self):
        v = _make_validator()
        v.record_pnl("TEST", -550_000)
        v.check(_make_intent())  # triggers soft limit
        assert v.soft_limit_active is True
        # Simulate PnL recovery: record positive delta to bring total above recovery
        # Current: -550_000. Need above -300_000 (recovery). Delta: +350_000
        v.record_pnl("TEST", 350_000)  # accumulated now = -200_000 (-200 NTD)
        # But cooldown not elapsed yet — still blocked
        ok, _ = v.check(_make_intent())
        assert ok is False
        assert v.soft_limit_active is True

    def test_soft_limit_recovery_after_cooldown(self):
        v = _make_validator()
        v.record_pnl("TEST", -550_000)
        v.check(_make_intent())  # triggers soft limit
        # PnL recovers
        v.record_pnl("TEST", 350_000)  # accumulated = -200_000 (-200 NTD)
        v._soft_limit_cooldown_until_ns = 0  # force cooldown expired
        ok, _ = v.check(_make_intent())
        assert ok is True
        assert v.soft_limit_active is False

    def test_oscillation_resets_cooldown(self):
        """Re-entering soft limit after recovery resets the cooldown timer."""
        v = _make_validator()
        v.record_pnl("TEST", -550_000)
        v.check(_make_intent())  # triggers soft limit
        v.record_pnl("TEST", 350_000)  # recover to -200_000
        v._soft_limit_cooldown_until_ns = 0  # force cooldown expired
        v.check(_make_intent())  # recovers
        assert v.soft_limit_active is False
        # Drop again below soft limit
        v.record_pnl("TEST", -400_000)  # now at -600_000
        v.check(_make_intent())  # re-triggers soft limit
        assert v.soft_limit_active is True
        # Cooldown should be fresh (not zero)
        assert v._soft_limit_cooldown_until_ns > 0


class TestPeakDrawdown:
    def test_peak_drawdown_ignored_when_peak_below_minimum(self):
        v = _make_validator()
        # Record gain of +100 NTD = +100_000 scaled (below 200 NTD minimum)
        v.record_pnl("TEST", 100_000)
        v.check(_make_intent())  # check() calls _update_peak() internally
        # Drop: record -150_000 delta → total = -50_000 (-50 NTD)
        # Drawdown from peak = 150_000 (150%), but peak < 200_000 minimum
        v.record_pnl("TEST", -150_000)
        ok, _ = v.check(_make_intent())
        assert ok is True  # drawdown rule disabled because peak < 200 NTD

    def test_peak_drawdown_triggers_when_peak_above_minimum(self):
        v = _make_validator()
        # Record gain of +300 NTD = +300_000 scaled
        v.record_pnl("TEST", 300_000)
        v.check(_make_intent())  # updates peak to +300_000
        # Drawdown threshold: 300_000 * 0.4 = 120_000 (120 NTD)
        # Drop to +150 NTD: delta = -150_000 → total = 150_000
        # Drawdown = 300_000 - 150_000 = 150_000 > 120_000 → trigger
        v.record_pnl("TEST", -150_000)
        ok, reason = v.check(_make_intent())
        assert ok is False
        assert "PEAK_DRAWDOWN" in reason

    def test_peak_drawdown_allows_when_drawdown_small(self):
        v = _make_validator()
        v.record_pnl("TEST", 300_000)  # +300 NTD
        v.check(_make_intent())  # updates peak
        # Small drop: delta -50_000 → total = 250_000
        # Drawdown = 50_000 < 120_000 threshold → OK
        v.record_pnl("TEST", -50_000)
        ok, _ = v.check(_make_intent())
        assert ok is True


class TestHardLimit:
    def test_hard_limit_triggers_halt(self):
        v = _make_validator()
        # -1050 NTD = -1_050_000 scaled, beyond -1000 NTD hard limit
        v.record_pnl("TEST", -1_050_000)
        ok, reason = v.check(_make_intent())
        assert ok is False
        assert v.halt_triggered is True

    def test_hard_limit_not_recoverable(self):
        v = _make_validator()
        v.record_pnl("TEST", -1_050_000)
        v.check(_make_intent())
        # Even if PnL recovers, halt stays until daily reset
        v.record_pnl("TEST", 1_050_000)  # back to 0
        ok, _ = v.check(_make_intent())
        assert ok is False
        assert v.halt_triggered is True


class TestReset:
    def test_daily_reset_clears_watermark_state(self):
        v = _make_validator()
        v.record_pnl("TEST", -550_000)
        v.check(_make_intent())
        assert v.soft_limit_active is True
        v._force_reset()
        assert v.soft_limit_active is False
        assert v._peak_pnl_scaled == 0
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/unit/test_intraday_pnl_watermark.py -v`
Expected: FAIL — new attributes (`soft_limit_active`, `_peak_pnl_scaled`, `_update_peak`) don't exist

- [ ] **Step 3: Implement watermark extensions in DailyLossLimitValidator**

Modify `src/hft_platform/risk/validators.py`. Key changes to `DailyLossLimitValidator`:

1. Add new `__slots__` entries: `"_peak_pnl_scaled"`, `"soft_limit_active"`, `"_soft_limit_cooldown_until_ns"`, `"_soft_limit_threshold_scaled"`, `"_soft_recovery_threshold_scaled"`, `"_peak_drawdown_pct"`, `"_drawdown_recovery_pct"`, `"_soft_limit_cooldown_ns"`, `"_peak_drawdown_min_peak_scaled"`, `"_intraday_pnl_enabled"`

2. In `__init__`, read `intraday_pnl` section from config. Convert NTD thresholds to scaled-int using formula: `threshold_scaled = ntd * price_scale / point_value`. For TMF: `price_scale=10000`, `point_value=10`, multiplier = 1000. So 500 NTD = 500_000 scaled. Read `price_scale` and `point_value` from intraday_pnl config section. Initialize new fields to zero/False.

3. Add `_update_peak()` method: recalculate `_total_pnl_scaled` from `sum(_accumulated_loss.values()) + _unrealized_pnl`, update `_peak_pnl_scaled = max(_peak_pnl_scaled, _total_pnl_scaled)`.

4. **CRITICAL: Restructure `check()` method.** The existing flow has `if total_pnl >= 0: return True` at line 276 which would skip peak-drawdown checks when PnL is positive but has dropped from peak. New flow must be:

   ```
   a. Bypass CANCEL and FORCE_FLAT → return True
   b. _maybe_reset()
   c. Compute total_pnl = sum(accumulated) + unrealized
   d. _update_peak()  ← ALWAYS update peak, even when total_pnl >= 0
   e. If halt_triggered → return False (existing)
   f. If soft_limit_active:
      - Check recovery (PnL above recovery threshold + cooldown elapsed) → deactivate
      - If still active → reject NEW with "SOFT_LIMIT" (allow CANCEL, FORCE_FLAT)
   g. Peak drawdown check (BEFORE the total_pnl >= 0 guard):
      - If peak > minimum AND drawdown > threshold → reject with "PEAK_DRAWDOWN"
   h. If total_pnl >= 0 → return True (existing early return, now AFTER peak check)
   i. Soft limit trigger check: if total_pnl < -soft_threshold → activate soft limit
   j. Hard limit check (existing): if loss_magnitude >= max_daily_loss → halt
   ```

5. Extend `_force_reset()` and `_maybe_reset()`: clear `_peak_pnl_scaled`, `soft_limit_active`, `_soft_limit_cooldown_until_ns`.

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/unit/test_intraday_pnl_watermark.py -v`
Expected: All PASS

- [ ] **Step 5: Run existing validator tests for regression**

Run: `uv run pytest tests/unit/ -k "validator or daily_loss" -v`
Expected: All existing tests PASS

- [ ] **Step 6: Commit**

```bash
git add src/hft_platform/risk/validators.py tests/unit/test_intraday_pnl_watermark.py
git commit -m "feat(risk): extend DailyLossLimitValidator with intraday watermark + soft limit + peak drawdown"
```

---

### Task 4: Add `decision_mid` field to OrderIntent

**Files:**
- Modify: `src/hft_platform/contracts/strategy.py:33-57`
- Test: existing contract tests (regression)

- [ ] **Step 1: Add field to OrderIntent**

Add after `ttl_ns` field (line 57):

```python
    # Module 2: Slippage tracking — decision-time mid-price (scaled int x10000)
    decision_mid: int = 0
```

- [ ] **Step 2: Run existing tests**

Run: `uv run pytest tests/ -k "OrderIntent or strategy_contract or intent" -v --timeout=30`
Expected: All PASS (field has default, backward compatible)

- [ ] **Step 3: Populate decision_mid in StrategyRunner**

Read `src/hft_platform/strategy/runner.py` to find where `OrderIntent` is created. In the method that produces intents (after calling `strategy.handle_event()`), add:

```python
# Before appending intent to output list:
if hasattr(self._lob_engine, 'last_stats') and self._lob_engine.last_stats is not None:
    intent.decision_mid = self._lob_engine.last_stats.mid_price_x2 // 2
```

This sets `decision_mid` from the LOB mid-price at decision time. `_lob_engine` is already available on StrategyRunner (injected via constructor, `bootstrap.py:903`).

- [ ] **Step 4: Run existing tests**

Run: `uv run pytest tests/ -k "OrderIntent or strategy_contract or intent or runner" -v --timeout=30`
Expected: All PASS (field has default, backward compatible)

- [ ] **Step 5: Commit**

```bash
git add src/hft_platform/contracts/strategy.py src/hft_platform/strategy/runner.py
git commit -m "feat(contracts): add decision_mid field to OrderIntent, populate in StrategyRunner"
```

---

### Task 5: ClickHouse migration — slippage_records table

**Files:**
- Create: `src/hft_platform/migrations/clickhouse/20260325_001_add_slippage_records.sql`

- [ ] **Step 1: Create migration file**

```sql
CREATE TABLE IF NOT EXISTS hft.slippage_records (
    order_id      String,
    symbol        String,
    side          UInt8,
    decision_mid  Int64,
    fill_price    Int64,
    slippage_ticks Int32,
    slippage_ntd  Int32,
    latency_ns    Int64,
    ts            Int64
) ENGINE = MergeTree()
ORDER BY (symbol, ts)
TTL toDateTime(ts / 1000000000) + INTERVAL 90 DAY;
```

- [ ] **Step 2: Commit**

```bash
git add src/hft_platform/migrations/clickhouse/20260325_001_add_slippage_records.sql
git commit -m "feat(db): add hft.slippage_records ClickHouse migration"
```

---

### Task 6: Module 2 — SlippageTracker + OrderAdapter correlation

**Files:**
- Create: `src/hft_platform/execution/slippage_tracker.py`
- Modify: `src/hft_platform/order/adapter.py:87-125`
- Test: `tests/unit/test_slippage_tracker.py`

- [ ] **Step 1: Write failing tests**

Create `tests/unit/test_slippage_tracker.py`:

```python
"""Tests for SlippageTracker per-fill slippage computation."""
import pytest
from hft_platform.execution.slippage_tracker import SlippageTracker, SlippageRecord
from hft_platform.contracts.strategy import Side


class TestSlippageComputation:
    def test_buy_adverse_slippage(self):
        """Buy at 20500 when mid was 20498 → 2 ticks adverse slippage."""
        record = SlippageTracker.compute_slippage(
            order_id="test_1",
            symbol="TMFD6",
            side=Side.BUY,
            decision_mid=204980000,   # 20498 x10000
            fill_price=205000000,     # 20500 x10000
            order_ts_ns=1000,
            fill_ts_ns=2000,
            tick_size_scaled=10000,
            point_value=10,
        )
        assert record.slippage_ticks == 2    # adverse: bought higher than mid
        assert record.slippage_ntd == 20     # 2 ticks * 10 NTD
        assert record.latency_ns == 1000

    def test_sell_adverse_slippage(self):
        """Sell at 20496 when mid was 20498 → 2 ticks adverse slippage."""
        record = SlippageTracker.compute_slippage(
            order_id="test_2",
            symbol="TMFD6",
            side=Side.SELL,
            decision_mid=204980000,
            fill_price=204960000,
            order_ts_ns=1000,
            fill_ts_ns=2000,
            tick_size_scaled=10000,
            point_value=10,
        )
        assert record.slippage_ticks == 2
        assert record.slippage_ntd == 20

    def test_favorable_slippage_negative(self):
        """Buy at 20497 when mid was 20498 → -1 tick (favorable)."""
        record = SlippageTracker.compute_slippage(
            order_id="test_3",
            symbol="TMFD6",
            side=Side.BUY,
            decision_mid=204980000,
            fill_price=204970000,
            order_ts_ns=1000,
            fill_ts_ns=2000,
            tick_size_scaled=10000,
            point_value=10,
        )
        assert record.slippage_ticks == -1   # favorable
        assert record.slippage_ntd == -10

    def test_zero_slippage(self):
        record = SlippageTracker.compute_slippage(
            order_id="test_4",
            symbol="TMFD6",
            side=Side.BUY,
            decision_mid=205000000,
            fill_price=205000000,
            order_ts_ns=1000,
            fill_ts_ns=2000,
            tick_size_scaled=10000,
            point_value=10,
        )
        assert record.slippage_ticks == 0
        assert record.slippage_ntd == 0

    def test_skips_when_decision_mid_is_zero(self):
        """If decision_mid was not captured, return None."""
        record = SlippageTracker.compute_slippage(
            order_id="test_5",
            symbol="TMFD6",
            side=Side.BUY,
            decision_mid=0,
            fill_price=205000000,
            order_ts_ns=1000,
            fill_ts_ns=2000,
            tick_size_scaled=10000,
            point_value=10,
        )
        assert record is None
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/unit/test_slippage_tracker.py -v`
Expected: FAIL — `slippage_tracker` module doesn't exist

- [ ] **Step 3: Implement SlippageTracker**

Create `src/hft_platform/execution/slippage_tracker.py`:

```python
"""Per-fill slippage tracking: captures decision-time mid-price vs fill price."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from structlog import get_logger

from hft_platform.contracts.strategy import Side

logger = get_logger("execution.slippage_tracker")


@dataclass(slots=True)
class SlippageRecord:
    order_id: str
    symbol: str
    side: Side
    decision_mid: int       # x10000
    fill_price: int         # x10000
    slippage_ticks: int     # positive = adverse
    slippage_ntd: int       # ticks * point_value
    latency_ns: int
    ts: int                 # fill timestamp ns


class SlippageTracker:
    """Computes and collects slippage records for ClickHouse persistence."""

    __slots__ = ("_records",)

    def __init__(self) -> None:
        self._records: list[SlippageRecord] = []

    @staticmethod
    def compute_slippage(
        *,
        order_id: str,
        symbol: str,
        side: Side,
        decision_mid: int,
        fill_price: int,
        order_ts_ns: int,
        fill_ts_ns: int,
        tick_size_scaled: int,
        point_value: int,
    ) -> Optional[SlippageRecord]:
        if decision_mid == 0:
            return None

        side_sign = 1 if side == Side.BUY else -1
        raw_diff = (fill_price - decision_mid) * side_sign
        slippage_ticks = raw_diff // tick_size_scaled
        slippage_ntd = slippage_ticks * point_value

        return SlippageRecord(
            order_id=order_id,
            symbol=symbol,
            side=side,
            decision_mid=decision_mid,
            fill_price=fill_price,
            slippage_ticks=slippage_ticks,
            slippage_ntd=slippage_ntd,
            latency_ns=fill_ts_ns - order_ts_ns,
            ts=fill_ts_ns,
        )

    def to_row_dict(self, record: SlippageRecord) -> dict:
        return {
            "order_id": record.order_id,
            "symbol": record.symbol,
            "side": int(record.side),
            "decision_mid": record.decision_mid,
            "fill_price": record.fill_price,
            "slippage_ticks": record.slippage_ticks,
            "slippage_ntd": record.slippage_ntd,
            "latency_ns": record.latency_ns,
            "ts": record.ts,
        }
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/unit/test_slippage_tracker.py -v`
Expected: All PASS

- [ ] **Step 5: Add `_decision_mid_map` to OrderAdapter**

Modify `src/hft_platform/order/adapter.py`:

1. In `__init__` (after line 101), add:
   ```python
   self._decision_mid_map: Dict[str, int] = {}
   ```

2. In `execute()` method, after storing the order in `live_orders`, add:
   ```python
   order_key = f"{cmd.intent.strategy_id}:{cmd.intent.intent_id}"
   if cmd.intent.decision_mid != 0:
       self._decision_mid_map[order_key] = cmd.intent.decision_mid
   ```

3. In `on_terminal_state()` (around line 235), add cleanup **BEFORE** `del self.live_orders[order_key]` (inside the async lock, because `order_key` resolution depends on `live_orders`):
   ```python
   # Must come BEFORE del self.live_orders[order_key]
   self._decision_mid_map.pop(order_key, None)
   ```

- [ ] **Step 6: Add slippage_records batcher to RecorderService**

Modify `src/hft_platform/recorder/worker.py`: Add a new batcher entry in `self.batchers` dict (after existing entries):

```python
"slippage_records": Batcher(
    "hft.slippage_records",
    writer=self.writer,
    memory_guard=self.memory_guard,
    health_tracker=self.health_tracker,
),
```

- [ ] **Step 7: Run full test suite for regression**

Run: `uv run pytest tests/unit/ -x --timeout=30 -q`
Expected: All PASS

- [ ] **Step 8: Commit**

```bash
git add src/hft_platform/execution/slippage_tracker.py \
        src/hft_platform/order/adapter.py \
        src/hft_platform/recorder/worker.py \
        tests/unit/test_slippage_tracker.py
git commit -m "feat(execution): add SlippageTracker with decision_mid correlation via OrderAdapter"
```

---

## Phase B: Launch Day (Module 3)

### Task 7: ClickHouse migration — daily_reports table

**Files:**
- Create: `src/hft_platform/migrations/clickhouse/20260325_002_add_daily_reports.sql`

- [ ] **Step 1: Create migration file**

```sql
CREATE TABLE IF NOT EXISTS hft.daily_reports (
    report_date   Date,
    strategy_id   String,
    symbol        String,
    realized_pnl_ntd  Int32,
    unrealized_pnl_ntd Int32,
    net_pnl_ntd   Int32,
    fees_ntd      Int32,
    tax_ntd       Int32,
    orders_sent   UInt32,
    orders_filled UInt32,
    orders_cancelled UInt32,
    avg_slippage_ticks Float32,
    slippage_cost_ntd Int32,
    peak_pnl_ntd  Int32,
    max_drawdown_ntd Int32,
    soft_limit_triggers UInt32,
    hard_limit_triggers UInt32,
    autonomy_transitions UInt32,
    win_count     UInt32,
    loss_count    UInt32,
    profit_factor Float32,
    ts            Int64
) ENGINE = MergeTree()
ORDER BY (report_date, strategy_id)
TTL report_date + INTERVAL 365 DAY;
```

- [ ] **Step 2: Commit**

```bash
git add src/hft_platform/migrations/clickhouse/20260325_002_add_daily_reports.sql
git commit -m "feat(db): add hft.daily_reports ClickHouse migration"
```

---

### Task 8: Shared analytics queries module

**Files:**
- Create: `src/hft_platform/analytics/__init__.py`
- Create: `src/hft_platform/analytics/queries.py`

- [ ] **Step 1: Create analytics package**

Create `src/hft_platform/analytics/__init__.py` (empty).

Create `src/hft_platform/analytics/queries.py`:

```python
"""Shared ClickHouse aggregation queries for daily reports, TCA, and scorecard."""
from __future__ import annotations

# Query: Daily fills summary for a given date range
DAILY_FILLS_SUMMARY = """
SELECT
    toDate(ts / 1000000000) AS trade_date,
    strategy_id,
    symbol,
    count() AS fill_count,
    sum(CASE WHEN realized_pnl != 0 THEN 1 ELSE 0 END) AS pnl_fills,
    sum(realized_pnl) AS total_realized_pnl,
    sum(fee) AS total_fees,
    sum(tax) AS total_tax
FROM hft.trades
WHERE ts >= {start_ns:Int64} AND ts < {end_ns:Int64}
GROUP BY trade_date, strategy_id, symbol
ORDER BY trade_date
"""

# Query: Daily slippage summary
DAILY_SLIPPAGE_SUMMARY = """
SELECT
    toDate(ts / 1000000000) AS trade_date,
    count() AS slip_count,
    avg(slippage_ticks) AS avg_slippage_ticks,
    sum(slippage_ntd) AS total_slippage_ntd,
    max(slippage_ticks) AS max_adverse_ticks
FROM hft.slippage_records
WHERE ts >= {start_ns:Int64} AND ts < {end_ns:Int64}
GROUP BY trade_date
ORDER BY trade_date
"""

# Query: Daily orders summary
DAILY_ORDERS_SUMMARY = """
SELECT
    toDate(ts / 1000000000) AS trade_date,
    count() AS total_orders,
    countIf(status = 'FILLED') AS filled,
    countIf(status = 'CANCELLED') AS cancelled
FROM hft.orders
WHERE ts >= {start_ns:Int64} AND ts < {end_ns:Int64}
GROUP BY trade_date
ORDER BY trade_date
"""

# Query: Cumulative daily reports lookback
CUMULATIVE_REPORTS = """
SELECT
    report_date,
    net_pnl_ntd,
    win_count,
    loss_count,
    profit_factor,
    avg_slippage_ticks,
    soft_limit_triggers,
    hard_limit_triggers
FROM hft.daily_reports
WHERE strategy_id = {strategy_id:String}
ORDER BY report_date
"""

# Query: Per-fill slippage detail for TCA
TCA_FILL_DETAIL = """
SELECT
    s.order_id,
    s.symbol,
    s.side,
    s.decision_mid,
    s.fill_price,
    s.slippage_ticks,
    s.slippage_ntd,
    s.latency_ns,
    s.ts,
    toHour(toDateTime(s.ts / 1000000000)) AS hour_of_day
FROM hft.slippage_records s
WHERE s.ts >= {start_ns:Int64} AND s.ts < {end_ns:Int64}
ORDER BY s.ts
"""
```

- [ ] **Step 2: Commit**

```bash
git add src/hft_platform/analytics/__init__.py src/hft_platform/analytics/queries.py
git commit -m "feat(analytics): add shared ClickHouse query module for reports/TCA/scorecard"
```

---

### Task 9: Module 3 — Daily PnL Report + Telegram integration

**Files:**
- Create: `src/hft_platform/ops/daily_pnl_report.py`
- Test: `tests/unit/test_daily_pnl_report.py`

- [ ] **Step 1: Write failing tests**

Create `tests/unit/test_daily_pnl_report.py`:

```python
"""Tests for daily PnL report generation."""
import pytest
from hft_platform.ops.daily_pnl_report import DailyPnLReport, DailyReportData


class TestDailyReportData:
    def test_net_pnl_calculation(self):
        data = DailyReportData(
            realized_pnl_ntd=300,
            fees_ntd=46,
            tax_ntd=20,
        )
        assert data.net_pnl_ntd == 234  # 300 - 46 - 20

    def test_win_rate(self):
        data = DailyReportData(win_count=7, loss_count=5)
        assert data.win_rate == pytest.approx(0.583, abs=0.01)

    def test_win_rate_no_trades(self):
        data = DailyReportData(win_count=0, loss_count=0)
        assert data.win_rate == 0.0

    def test_profit_factor(self):
        data = DailyReportData(gross_profit_ntd=500, gross_loss_ntd=200)
        assert data.profit_factor == pytest.approx(2.5)

    def test_profit_factor_no_losses(self):
        data = DailyReportData(gross_profit_ntd=500, gross_loss_ntd=0)
        assert data.profit_factor == float("inf")


class TestTelegramFormat:
    def test_format_daily_summary(self):
        data = DailyReportData(
            report_date="2026-03-24",
            realized_pnl_ntd=254,
            unrealized_pnl_ntd=0,
            fees_ntd=46,
            tax_ntd=20,
            orders_sent=12,
            orders_filled=12,
            orders_cancelled=0,
            avg_slippage_ticks=-0.8,
            slippage_cost_ntd=-96,
            peak_pnl_ntd=480,
            max_drawdown_ntd=-160,
            soft_limit_triggers=0,
            hard_limit_triggers=0,
            autonomy_transitions=0,
            win_count=7,
            loss_count=5,
        )
        msg = DailyPnLReport.format_telegram(data)
        assert "Daily Summary" in msg
        assert "+254 NTD" in msg or "254" in msg
        assert "12 sent" in msg
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/unit/test_daily_pnl_report.py -v`
Expected: FAIL — module doesn't exist

- [ ] **Step 3: Implement DailyPnLReport**

Create `src/hft_platform/ops/daily_pnl_report.py`. Key components:

1. `DailyReportData` dataclass with all report fields and computed properties (`net_pnl_ntd`, `win_rate`, `profit_factor`)
2. `DailyPnLReport.format_telegram(data) -> str` — formats the Telegram message string matching the spec template
3. `DailyPnLReport.aggregate_from_clickhouse(client, date, strategy_id) -> DailyReportData` — runs shared queries from `analytics/queries.py`, returns populated dataclass
4. `DailyPnLReport.persist_to_clickhouse(client, data)` — inserts row into `hft.daily_reports`
5. `DailyPnLReport.generate_evidence_pack(data, output_dir)` — writes JSON + CSV to `outputs/production_rollout/daily/YYYYMMDD/`

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/unit/test_daily_pnl_report.py -v`
Expected: All PASS

- [ ] **Step 5: Add daily_reports batcher to RecorderService**

Modify `src/hft_platform/recorder/worker.py`: Add batcher:

```python
"daily_reports": Batcher(
    "hft.daily_reports",
    writer=self.writer,
    memory_guard=self.memory_guard,
    health_tracker=self.health_tracker,
),
```

- [ ] **Step 6: Commit**

```bash
git add src/hft_platform/ops/daily_pnl_report.py \
        tests/unit/test_daily_pnl_report.py \
        src/hft_platform/recorder/worker.py
git commit -m "feat(ops): add daily PnL report with Telegram integration and evidence pack"
```

---

## Phase C: Post-Launch (Modules 4 + 5)

### Task 10: Module 4 — TCA CLI command

**Files:**
- Create: `src/hft_platform/cli/_tca.py`
- Modify: `src/hft_platform/cli/_parser.py`
- Test: `tests/unit/test_tca_report.py`

- [ ] **Step 1: Write failing tests**

Create `tests/unit/test_tca_report.py`:

```python
"""Tests for TCA attribution engine."""
import pytest
from hft_platform.cli._tca import TCAEngine, TradeAttribution


class TestTradeAttribution:
    def test_gross_alpha_computation(self):
        """Gross alpha = what you'd have earned with zero slippage."""
        attr = TradeAttribution(
            fill_pnl_ntd=80,
            slippage_ntd=20,
            fees_ntd=12,
        )
        assert attr.gross_alpha_ntd == 100  # 80 + 20
        assert attr.net_alpha_ntd == 68     # 100 - 20 - 12

    def test_retention_rate(self):
        attr = TradeAttribution(
            fill_pnl_ntd=80,
            slippage_ntd=20,
            fees_ntd=12,
        )
        assert attr.retention_rate == pytest.approx(0.68)

    def test_retention_rate_zero_gross(self):
        attr = TradeAttribution(fill_pnl_ntd=-20, slippage_ntd=20, fees_ntd=0)
        assert attr.retention_rate == 0.0  # gross alpha = 0


class TestTCAEngine:
    def test_aggregate_by_hour(self):
        records = [
            {"hour_of_day": 9, "slippage_ticks": 2},
            {"hour_of_day": 9, "slippage_ticks": 1},
            {"hour_of_day": 10, "slippage_ticks": 3},
        ]
        by_hour = TCAEngine.aggregate_by_dimension(records, "hour_of_day", "slippage_ticks")
        assert by_hour[9] == pytest.approx(1.5)
        assert by_hour[10] == pytest.approx(3.0)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/unit/test_tca_report.py -v`
Expected: FAIL

- [ ] **Step 3: Implement TCA engine + CLI**

Create `src/hft_platform/cli/_tca.py`:

1. `TradeAttribution` dataclass: `fill_pnl_ntd`, `slippage_ntd`, `fees_ntd` → computed `gross_alpha_ntd`, `net_alpha_ntd`, `retention_rate`
2. `TCAEngine.aggregate_by_dimension(records, group_key, value_key) -> dict` — groups and averages
3. `TCAEngine.run(ch_client, days, strategy_id) -> TCAReport` — queries via `analytics/queries.py`, computes per-trade attribution, aggregates by hour/direction/trend
4. `cmd_tca_report(args)` — CLI entry point, connects to ClickHouse, runs engine, prints table + CSV

- [ ] **Step 4: Register CLI command**

Modify `src/hft_platform/cli/_parser.py`: Add after existing subcommands:

```python
tca = sub.add_parser("tca", help="Transaction cost analysis")
tca_sub = tca.add_subparsers(dest="tca_cmd")
tca_report = tca_sub.add_parser("report", help="Generate TCA attribution report")
tca_report.add_argument("--days", type=int, default=5, help="Lookback days")
tca_report.add_argument("--strategy", type=str, default="", help="Strategy ID filter")
tca_report.set_defaults(func=cmd_tca_report)
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `uv run pytest tests/unit/test_tca_report.py -v`
Expected: All PASS

- [ ] **Step 6: Commit**

```bash
git add src/hft_platform/cli/_tca.py \
        src/hft_platform/cli/_parser.py \
        tests/unit/test_tca_report.py
git commit -m "feat(cli): add hft tca report command for transaction cost attribution"
```

---

### Task 11: Config — Add liquidity_gate thresholds

**Files:**
- Modify: `config/base/strategy_limits.yaml`

- [ ] **Step 1: Add liquidity_gate section**

Append to `config/base/strategy_limits.yaml`:

```yaml
liquidity_gate:
  spread_reject_ticks: 3
  spread_warn_ticks: 2
  cooldown_s: 5
  gate_start_offset_s: 60
```

- [ ] **Step 2: Commit**

```bash
git add config/base/strategy_limits.yaml
git commit -m "feat(risk): add liquidity_gate config to strategy_limits"
```

---

### Task 12: ClickHouse migration — liquidity_gate_events table

**Files:**
- Create: `src/hft_platform/migrations/clickhouse/20260325_003_add_liquidity_gate_events.sql`

- [ ] **Step 1: Create migration file**

```sql
CREATE TABLE IF NOT EXISTS hft.liquidity_gate_events (
    symbol        String,
    spread_scaled Int64,
    threshold_scaled Int64,
    action        String,
    ts            Int64
) ENGINE = MergeTree()
ORDER BY (symbol, ts)
TTL toDateTime(ts / 1000000000) + INTERVAL 30 DAY;
```

- [ ] **Step 2: Commit**

```bash
git add src/hft_platform/migrations/clickhouse/20260325_003_add_liquidity_gate_events.sql
git commit -m "feat(db): add hft.liquidity_gate_events ClickHouse migration"
```

---

### Task 13: Module 5 — LiquidityGateValidator + RiskEngine wiring

**Files:**
- Create: `src/hft_platform/risk/liquidity_gate.py`
- Modify: `src/hft_platform/risk/engine.py:91-130`
- Modify: `src/hft_platform/services/bootstrap.py:860-870`
- Test: `tests/unit/test_liquidity_gate.py`

- [ ] **Step 1: Write failing tests**

Create `tests/unit/test_liquidity_gate.py`:

```python
"""Tests for LiquidityGateValidator."""
import pytest
from unittest.mock import MagicMock
from hft_platform.risk.liquidity_gate import LiquidityGateValidator
from hft_platform.contracts.strategy import OrderIntent, Side, IntentType, TIF


def _make_intent(intent_type=IntentType.NEW):
    return OrderIntent(
        intent_id=1, strategy_id="TEST", symbol="TMFD6",
        intent_type=intent_type, side=Side.BUY, price=200000000, qty=1,
    )


def _make_lob(spread_scaled=10000):
    lob = MagicMock()
    stats = MagicMock()
    stats.spread_scaled = spread_scaled
    lob.last_stats = stats
    return lob


def _make_validator(lob=None, reject_ticks=3, tick_size_scaled=10000):
    cfg = {
        "liquidity_gate": {
            "spread_reject_ticks": reject_ticks,
            "spread_warn_ticks": 2,
            "cooldown_s": 5,
            "gate_start_offset_s": 60,
        },
    }
    v = LiquidityGateValidator(cfg, None, lob=lob, tick_size_scaled=tick_size_scaled)
    v._gate_active = True  # bypass time-of-day offset for testing
    return v


class TestLiquidityGate:
    def test_allows_normal_spread(self):
        lob = _make_lob(spread_scaled=10000)  # 1 tick
        v = _make_validator(lob=lob)
        ok, reason = v.check(_make_intent())
        assert ok is True

    def test_rejects_wide_spread(self):
        lob = _make_lob(spread_scaled=40000)  # 4 ticks > 3 threshold
        v = _make_validator(lob=lob)
        ok, reason = v.check(_make_intent())
        assert ok is False
        assert "SPREAD_TOO_WIDE" in reason

    def test_allows_cancel_during_wide_spread(self):
        lob = _make_lob(spread_scaled=40000)
        v = _make_validator(lob=lob)
        ok, _ = v.check(_make_intent(intent_type=IntentType.CANCEL))
        assert ok is True

    def test_allows_force_flat_during_wide_spread(self):
        lob = _make_lob(spread_scaled=40000)
        v = _make_validator(lob=lob)
        ok, _ = v.check(_make_intent(intent_type=IntentType.FORCE_FLAT))
        assert ok is True

    def test_allows_when_no_lob_data(self):
        """If LOB hasn't published stats yet, don't block."""
        lob = MagicMock()
        lob.last_stats = None
        v = _make_validator(lob=lob)
        ok, _ = v.check(_make_intent())
        assert ok is True

    def test_exact_threshold_allowed(self):
        lob = _make_lob(spread_scaled=30000)  # exactly 3 ticks
        v = _make_validator(lob=lob)
        ok, _ = v.check(_make_intent())
        assert ok is True  # reject only when strictly greater
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/unit/test_liquidity_gate.py -v`
Expected: FAIL — module doesn't exist

- [ ] **Step 3: Implement LiquidityGateValidator**

Create `src/hft_platform/risk/liquidity_gate.py`:

```python
"""Liquidity gate validator: rejects new orders when spread is abnormally wide."""
from __future__ import annotations

from typing import Any, Tuple

from structlog import get_logger

from hft_platform.contracts.strategy import IntentType, OrderIntent
from hft_platform.risk.validators import RiskValidator
from hft_platform import timebase

logger = get_logger("risk.liquidity_gate")


class LiquidityGateValidator(RiskValidator):
    __slots__ = (
        "_spread_reject_scaled",
        "_spread_warn_scaled",
        "_cooldown_ns",
        "_gate_start_offset_ns",
        "_lob",
        "_last_reject_ns",
        "_gate_active",
    )

    def __init__(self, config: dict, price_scale_provider: Any,
                 lob: Any = None, tick_size_scaled: int = 10000) -> None:
        super().__init__(config, price_scale_provider)
        gate_cfg = config.get("liquidity_gate", {})
        reject_ticks = int(gate_cfg.get("spread_reject_ticks", 3))
        warn_ticks = int(gate_cfg.get("spread_warn_ticks", 2))
        self._spread_reject_scaled = reject_ticks * tick_size_scaled
        self._spread_warn_scaled = warn_ticks * tick_size_scaled
        self._cooldown_ns = int(gate_cfg.get("cooldown_s", 5)) * 1_000_000_000
        self._gate_start_offset_ns = int(gate_cfg.get("gate_start_offset_s", 60)) * 1_000_000_000
        self._lob = lob
        self._last_reject_ns: int = 0
        self._gate_active: bool = False

    def check(self, intent: OrderIntent) -> Tuple[bool, str]:
        # Always allow cancel and force-flat
        if intent.intent_type in (IntentType.CANCEL, IntentType.FORCE_FLAT):
            return True, "OK"

        if self._lob is None:
            return True, "OK"

        stats = getattr(self._lob, "last_stats", None)
        if stats is None:
            return True, "OK"

        if not self._gate_active:
            return True, "OK"

        spread = getattr(stats, "spread_scaled", 0)

        if spread > self._spread_reject_scaled:
            now_ns = timebase.now_ns()
            # Cooldown: don't spam rejects
            if now_ns - self._last_reject_ns < self._cooldown_ns:
                return False, "SPREAD_TOO_WIDE_COOLDOWN"
            self._last_reject_ns = now_ns
            logger.warning(
                "Liquidity gate: spread too wide",
                symbol=intent.symbol,
                spread_scaled=spread,
                threshold=self._spread_reject_scaled,
            )
            return False, f"SPREAD_TOO_WIDE: {spread} > {self._spread_reject_scaled}"

        if spread > self._spread_warn_scaled:
            logger.info(
                "Liquidity gate: spread warning",
                symbol=intent.symbol,
                spread_scaled=spread,
            )

        return True, "OK"
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/unit/test_liquidity_gate.py -v`
Expected: All PASS

- [ ] **Step 5: Wire LOB into RiskEngine + bootstrap**

Modify `src/hft_platform/risk/engine.py`:

1. Add `lob_engine` parameter to `__init__`:
   ```python
   def __init__(self, config_path, intent_queue, order_queue,
                price_scale_provider=None, storm_guard=None,
                notification_dispatcher=None, lob_engine=None):
   ```

2. After existing validators list, conditionally add LiquidityGateValidator:
   ```python
   if self.config.get("liquidity_gate"):
       from hft_platform.risk.liquidity_gate import LiquidityGateValidator
       self.validators.append(
           LiquidityGateValidator(self.config, price_scale_provider, lob=lob_engine)
       )
   ```

Modify `src/hft_platform/services/bootstrap.py:864`:
```python
risk_engine = RiskEngine(
    risk_path, risk_queue, order_queue, price_scale_provider,
    lob_engine=md_service.lob,
)
```

- [ ] **Step 6: Add liquidity_gate_events batcher**

Modify `src/hft_platform/recorder/worker.py`:

```python
"liquidity_gate_events": Batcher(
    "hft.liquidity_gate_events",
    writer=self.writer,
    memory_guard=self.memory_guard,
    health_tracker=self.health_tracker,
),
```

- [ ] **Step 7: Run full test suite**

Run: `uv run pytest tests/unit/ -x --timeout=30 -q`
Expected: All PASS

- [ ] **Step 8: Commit**

```bash
git add src/hft_platform/risk/liquidity_gate.py \
        src/hft_platform/risk/engine.py \
        src/hft_platform/services/bootstrap.py \
        src/hft_platform/recorder/worker.py \
        tests/unit/test_liquidity_gate.py
git commit -m "feat(risk): add LiquidityGateValidator with LOB wiring through RiskEngine + bootstrap"
```

---

### Task 13b: Register Prometheus metrics for Modules 1 + 5

**Files:**
- Modify: `src/hft_platform/observability/metrics.py` (or wherever `MetricsRegistry` is defined)

- [ ] **Step 1: Find MetricsRegistry**

Run: `grep -rn "class MetricsRegistry" src/hft_platform/` to locate the metrics file.

- [ ] **Step 2: Add new metrics**

Add to the registry:

```python
# Module 1: Intraday PnL watermark
pnl_soft_limit_active = Gauge("hft_pnl_soft_limit_active", "Whether soft limit reduce-only is active")
pnl_hard_limit_triggered_total = Counter("hft_pnl_hard_limit_triggered_total", "Hard limit HALT triggers")
pnl_peak_drawdown_triggered_total = Counter("hft_pnl_peak_drawdown_triggered_total", "Peak drawdown reduce-only triggers")

# Module 5: Liquidity gate
liquidity_gate_rejections_total = Counter("hft_liquidity_gate_rejections_total", "Orders rejected due to wide spread", ["symbol"])
```

- [ ] **Step 3: Wire metrics into validators**

In `DailyLossLimitValidator.check()`: when soft limit activates, set `metrics.pnl_soft_limit_active.set(1)`. On recovery, `.set(0)`. On hard limit, increment `pnl_hard_limit_triggered_total`. On peak drawdown, increment `pnl_peak_drawdown_triggered_total`.

In `LiquidityGateValidator.check()`: on rejection, increment `liquidity_gate_rejections_total.labels(symbol=intent.symbol)`.

- [ ] **Step 4: Run tests**

Run: `uv run pytest tests/unit/test_intraday_pnl_watermark.py tests/unit/test_liquidity_gate.py -v`
Expected: All PASS

- [ ] **Step 5: Commit**

```bash
git add src/hft_platform/observability/metrics.py \
        src/hft_platform/risk/validators.py \
        src/hft_platform/risk/liquidity_gate.py
git commit -m "feat(obs): add Prometheus metrics for PnL watermark and liquidity gate"
```

---

## Phase D: Scorecard (Module 6)

### Task 14: Module 6 — Feasibility Scorecard CLI

**Files:**
- Create: `src/hft_platform/cli/_feasibility.py`
- Modify: `src/hft_platform/cli/_parser.py`
- Test: `tests/unit/test_feasibility_scorecard.py`

- [ ] **Step 1: Write failing tests**

Create `tests/unit/test_feasibility_scorecard.py`:

```python
"""Tests for feasibility validation scorecard."""
import pytest
from hft_platform.cli._feasibility import FeasibilityScorecard, Verdict


class TestVerdict:
    def test_pass_when_all_criteria_met(self):
        sc = FeasibilityScorecard(
            cumulative_net_pnl_ntd=1820,
            daily_pnl_values=[200, 300, -100, 250, 400, -50, 320, 500],
            net_alpha_retention_rate=0.73,
            hard_limit_triggers=0,
            max_consecutive_loss_days=1,
        )
        assert sc.verdict == Verdict.PASS

    def test_fail_when_cumulative_loss(self):
        sc = FeasibilityScorecard(
            cumulative_net_pnl_ntd=-500,
            daily_pnl_values=[-100, -200, 50, -250],
            net_alpha_retention_rate=0.60,
            hard_limit_triggers=0,
            max_consecutive_loss_days=2,
        )
        assert sc.verdict == Verdict.FAIL

    def test_inconclusive_when_profitable_but_not_significant(self):
        sc = FeasibilityScorecard(
            cumulative_net_pnl_ntd=100,
            daily_pnl_values=[50, -30, 40, 20, 10, 5, 5],  # small effect, high p
            net_alpha_retention_rate=0.60,
            hard_limit_triggers=0,
            max_consecutive_loss_days=1,
        )
        # With such small/noisy values, t-test likely p > 0.10
        assert sc.verdict in (Verdict.INCONCLUSIVE, Verdict.PASS)

    def test_fail_when_retention_too_low(self):
        sc = FeasibilityScorecard(
            cumulative_net_pnl_ntd=500,
            daily_pnl_values=[100, 100, 100, 100, 100],
            net_alpha_retention_rate=0.30,  # below 50%
            hard_limit_triggers=0,
            max_consecutive_loss_days=0,
        )
        assert sc.verdict == Verdict.FAIL

    def test_fail_when_too_many_hard_limits(self):
        sc = FeasibilityScorecard(
            cumulative_net_pnl_ntd=500,
            daily_pnl_values=[100, 100, 100, 100, 100],
            net_alpha_retention_rate=0.70,
            hard_limit_triggers=3,  # > 1
            max_consecutive_loss_days=0,
        )
        assert sc.verdict == Verdict.FAIL


class TestTTest:
    def test_significant_positive_returns(self):
        sc = FeasibilityScorecard(
            cumulative_net_pnl_ntd=2000,
            daily_pnl_values=[200, 300, 250, 200, 350, 300, 400, 250, 300, 200],
            net_alpha_retention_rate=0.70,
            hard_limit_triggers=0,
            max_consecutive_loss_days=0,
        )
        assert sc.t_test_p_value < 0.05  # strongly significant
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/unit/test_feasibility_scorecard.py -v`
Expected: FAIL

- [ ] **Step 3: Implement FeasibilityScorecard**

Create `src/hft_platform/cli/_feasibility.py`:

```python
"""Feasibility validation scorecard — statistical pass/fail for strategy viability."""
from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Sequence

from scipy.stats import ttest_1samp


class Verdict(StrEnum):
    PASS = "PASS"
    INCONCLUSIVE = "INCONCLUSIVE"
    FAIL = "FAIL"


@dataclass
class FeasibilityScorecard:
    cumulative_net_pnl_ntd: int
    daily_pnl_values: Sequence[float]
    net_alpha_retention_rate: float
    hard_limit_triggers: int
    max_consecutive_loss_days: int
    p_threshold: float = 0.10

    @property
    def t_test_p_value(self) -> float:
        if len(self.daily_pnl_values) < 2:
            return 1.0
        _, p = ttest_1samp(self.daily_pnl_values, 0)
        return float(p)

    @property
    def verdict(self) -> Verdict:
        # Hard fail conditions
        if self.cumulative_net_pnl_ntd <= 0:
            return Verdict.FAIL
        if self.net_alpha_retention_rate < 0.50:
            return Verdict.FAIL
        if self.hard_limit_triggers > 1:
            return Verdict.FAIL
        if self.max_consecutive_loss_days > 3:
            return Verdict.FAIL
        # Statistical check
        if self.t_test_p_value > self.p_threshold:
            return Verdict.INCONCLUSIVE
        return Verdict.PASS
```

Add `cmd_feasibility_report(args)` function that connects to ClickHouse, queries `hft.daily_reports` via `analytics/queries.py`, builds `FeasibilityScorecard`, prints terminal report, writes JSON.

- [ ] **Step 4: Register CLI command**

Modify `src/hft_platform/cli/_parser.py`: Add:

```python
feasibility = sub.add_parser("feasibility", help="Feasibility validation")
feas_sub = feasibility.add_subparsers(dest="feasibility_cmd")
feas_report = feas_sub.add_parser("report", help="Generate feasibility scorecard")
feas_report.add_argument("--min-days", type=int, default=5)
feas_report.add_argument("--strategy", type=str, required=True)
feas_report.set_defaults(func=cmd_feasibility_report)
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `uv run pytest tests/unit/test_feasibility_scorecard.py -v`
Expected: All PASS

- [ ] **Step 6: Run full test suite**

Run: `uv run pytest tests/unit/ -x --timeout=30 -q`
Expected: All PASS

- [ ] **Step 7: Commit**

```bash
git add src/hft_platform/cli/_feasibility.py \
        src/hft_platform/cli/_parser.py \
        tests/unit/test_feasibility_scorecard.py
git commit -m "feat(cli): add hft feasibility report command with statistical pass/fail scorecard"
```

---

## Final: Integration Verification

### Task 15: Lint + typecheck + full test sweep

- [ ] **Step 1: Lint**

Run: `uv run ruff check src/hft_platform/execution/slippage_tracker.py src/hft_platform/risk/liquidity_gate.py src/hft_platform/ops/daily_pnl_report.py src/hft_platform/analytics/ src/hft_platform/cli/_tca.py src/hft_platform/cli/_feasibility.py`

Fix any issues.

- [ ] **Step 2: Type check new files**

Run: `uv run mypy src/hft_platform/execution/slippage_tracker.py src/hft_platform/risk/liquidity_gate.py src/hft_platform/ops/daily_pnl_report.py src/hft_platform/analytics/queries.py src/hft_platform/cli/_tca.py src/hft_platform/cli/_feasibility.py`

Fix any issues.

- [ ] **Step 3: Full test run**

Run: `uv run pytest tests/unit/ -x --timeout=60 -q`
Expected: All PASS, no regressions

- [ ] **Step 4: Commit any fixes**

```bash
git add -u
git commit -m "chore: fix lint and type issues in feasibility validation modules"
```
