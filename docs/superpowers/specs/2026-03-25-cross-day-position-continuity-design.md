# Cross-Day Position State Continuity — Design Spec

**Date**: 2026-03-25
**Status**: Implemented
**Scope**: Sub-project B of Long-Term Operations Readiness

## Problem Statement

On restart, the platform's `PositionStore` initializes empty. If the broker holds existing positions (e.g., overnight futures), the first `ReconciliationService.sync_portfolio()` detects a discrepancy and may trigger HALT or reduce-only mode. This makes unattended autonomous operation impossible across restarts.

The infrastructure for position recovery already exists (`PositionCheckpointWriter`, `StartupPositionVerifier`) but is **not wired into the bootstrap sequence**.

## Goals

1. **Auto-warm PositionStore on restart** from dual sources (checkpoint file + broker API)
2. **Cross-validate** checkpoint vs broker positions before trading starts
3. **Graduated response** — small discrepancies auto-correct, large discrepancies HALT
4. **Trading-date awareness** — stale checkpoints (wrong trading date) trigger degraded recovery
5. **Observability** — Prometheus metrics + Telegram alerts for recovery status

## Non-Goals

- WAL cross-day continuity verification — separate project
- ClickHouse position restoration — checkpoint + broker is sufficient
- `RustPositionTracker` serialization — Python `PositionStore` is the source of truth
- Multi-account position merging — single account per broker

## Constraints

- `PositionCheckpointWriter` (`execution/checkpoint.py`) already implemented — reuse, don't rewrite
- `StartupPositionVerifier` (`execution/startup_recon.py`) already implemented — extend, don't rewrite
- Recovery must complete **before** `ReconciliationService` starts (otherwise recon sees empty PositionStore)
- Recovery must complete **before** `StrategyRunner` starts (no trading on stale positions)
- Broker `get_positions()` is synchronous and may take 1-3 seconds (Shioaji API call)

## Architecture

### Recovery Flow

```
HFTSystem.run()
  1. Start infrastructure (ClickHouse, Redis, Prometheus)
  2. Start RecorderService
  3. ★ StartupPositionVerifier.recover()          ← NEW, blocking
     a. Load checkpoint (.runtime/position_checkpoint.json)
     b. Check trading_date matches current trading_date
        - Match → checkpoint valid
        - Mismatch → checkpoint stale, degrade to broker-only + warn
        - Missing → first start, degrade to broker-only
     c. Query broker get_positions()
        - Success → continue
        - Failure + valid checkpoint → use checkpoint-only + warn
        - Failure + no checkpoint → HALT
     d. Cross-validate checkpoint vs broker
     e. Apply graduated response (auto-correct or HALT)
     f. Write merged positions into PositionStore
     g. Return RecoveryResult
  4. Start PositionCheckpointWriter (background)   ← NEW
  5. Start ReconciliationService
  6. Start StrategyRunner + MarketDataService
```

### Components

| Component | File | Change |
|-----------|------|--------|
| PositionCheckpointWriter | `src/hft_platform/execution/checkpoint.py` | Add `trading_date` to checkpoint format |
| StartupPositionVerifier | `src/hft_platform/execution/startup_recon.py` | Add `recover()` method with dual-source merge + graduated response |
| RecoveryResult | `src/hft_platform/execution/startup_recon.py` | New dataclass for recovery outcome |
| Bootstrap wiring | `src/hft_platform/services/bootstrap.py` | Wire checkpoint writer + startup verifier |
| System startup | `src/hft_platform/services/system.py` | Call `recover()` before trading loop |
| ServiceRegistry | `src/hft_platform/services/registry.py` | Register checkpoint_writer + startup_verifier |
| Notification extension | `src/hft_platform/notifications/dispatcher.py` | +2 methods |
| Template extension | `src/hft_platform/notifications/templates.py` | +2 render functions |
| Metrics extension | `src/hft_platform/observability/metrics.py` | +3 Gauge metrics |

## Detailed Design

### 1. Checkpoint Format Extension

Current format (from `PositionCheckpointWriter.write_checkpoint()`):

```json
{
  "timestamp_ns": 1711353600000000000,
  "positions": {"2330": {"symbol": "2330", "net_qty": 1000, "avg_price_scaled": 6500000, "realized_pnl_scaled": 0}},
  "sha256": "abc123..."
}
```

**Extended format** — add `trading_date`:

```json
{
  "trading_date": "20260325",
  "timestamp_ns": 1711353600000000000,
  "positions": {"2330": {"symbol": "2330", "net_qty": 1000, "avg_price_scaled": 6500000, "realized_pnl_scaled": 0}},
  "sha256": "abc123..."
}
```

Changes to `PositionCheckpointWriter`:
- `__slots__` extended with `"_trading_date_provider"`
- `__init__` accepts optional `trading_date_provider: Callable[[], str]` (defaults to `datetime.now(tz=ZoneInfo("Asia/Taipei")).strftime("%Y%m%d")`)
- `write_checkpoint()` adds `"trading_date"` to `body_obj` **before** the SHA-256 hash computation (so the hash covers the trading_date field)
- `load_checkpoint()` returns the dict with `trading_date` present if it was in the file. Callers must use `data.get("trading_date")` (not `data["trading_date"]`) for backward compatibility with old checkpoint files that lack the field

### 2. StartupPositionVerifier Extension

Add `recover()` method and `RecoveryResult` dataclass.

```python
@dataclass
class RecoveryResult:
    """Outcome of startup position recovery."""
    source: str              # "dual", "broker_only", "checkpoint_only", "empty"
    positions_loaded: int    # symbols written to PositionStore
    auto_corrected: int      # small discrepancies auto-corrected (broker wins)
    halted: bool             # True if HALT triggered
    mismatches: list[dict]   # [{symbol, checkpoint_qty, broker_qty, action}]
```

```python
class StartupPositionVerifier:
    # Extend __init__ signature with new keyword arguments:
    #   qty_threshold: int = 10       (from env HFT_STARTUP_RECON_QTY_THRESHOLD)
    #   futures_qty_threshold: int = 2 (from env HFT_STARTUP_RECON_FUTURES_QTY_THRESHOLD)
    #
    # The existing _load_checkpoint() module-level function in startup_recon.py
    # uses a simplified format ({symbol: qty}). The new recover() method uses
    # PositionCheckpointWriter.load_checkpoint() instead (SHA-256 verified,
    # richer format with avg_price_scaled etc). The old _load_checkpoint is
    # preserved for backward compatibility with verify() but NOT used by recover().

    async def recover(self) -> RecoveryResult:
        """Dual-source position recovery with graduated response.

        1. Load checkpoint + check trading_date
        2. Query broker
        3. Cross-validate
        4. Write to PositionStore
        5. Return RecoveryResult
        """
```

**Graduated response logic**:

```python
def _classify_discrepancy(self, symbol: str, ckpt_qty: int, broker_qty: int) -> str:
    """Returns 'match', 'minor', or 'critical'."""
    if ckpt_qty == broker_qty:
        return "match"
    diff = abs(ckpt_qty - broker_qty)
    # Side mismatch (one long, one short) → always critical
    if (ckpt_qty > 0 and broker_qty < 0) or (ckpt_qty < 0 and broker_qty > 0):
        return "critical"
    threshold = self._futures_qty_threshold if self._is_futures(symbol) else self._qty_threshold
    return "minor" if diff <= threshold else "critical"
```

**PositionStore write-back**:

`PositionStore.positions` uses a **composite key**: `f"{account_id}:{strategy_id}:{symbol}"`. For recovered positions, `account_id` comes from the broker config and `strategy_id` is `""` (recovered positions are not strategy-specific until a strategy claims them).

```python
def _write_to_store(self, positions: dict[str, dict], account_id: str) -> int:
    """Write recovered positions into PositionStore. Returns count.

    Key format: '{account_id}::{symbol}' (empty strategy_id).
    """
    from hft_platform.execution.positions import Position
    count = 0
    for symbol, data in positions.items():
        pos = Position(
            account_id=account_id,
            strategy_id="",
            symbol=symbol,
            net_qty=data["net_qty"],
            avg_price_scaled=data.get("avg_price_scaled", 0),
            realized_pnl_scaled=data.get("realized_pnl_scaled", 0),
        )
        key = f"{account_id}::{symbol}"
        self.store.positions[key] = pos
        count += 1
    return count
```

### 3. Trading Date Resolution

`recover()` needs the current trading_date. Primary method:

- `datetime.now(tz=ZoneInfo("Asia/Taipei")).strftime("%Y%m%d")` — used by default

**Note**: `SessionGovernor` is opt-in (`HFT_SESSION_GOVERNOR_ENABLED`, default `0`) and may not be instantiated during early bootstrap when `recover()` runs. Therefore, the calendar-date fallback is the **primary** path in most deployments.

**Cross-midnight limitation**: For futures night sessions (15:00-05:00), at 3am the calendar date will be the next day. A checkpoint written at 23:00 on 2026-03-25 will have `trading_date=20260325`, but the recover at 03:00 on 2026-03-26 will compare against `20260326`. This will mark the checkpoint as "stale" and trigger broker-only recovery. This is **acceptable** behavior — the broker always has the correct position state, and the checkpoint serves only as a cross-validation source. Future improvement: integrate `SessionGovernor.trading_date` when available.

### 4. Bootstrap Wiring

Changes to `SystemBootstrapper.build()`:

Both `checkpoint_writer` and `startup_verifier` are registered in `ServiceRegistry` and assigned as attributes on `HFTSystem` (same pattern as `recon_service`, `strategy_runner`, etc.).

```python
# After PositionStore is created:
checkpoint_writer = PositionCheckpointWriter(
    store=position_store,
    trading_date_provider=lambda: datetime.now(tz=ZoneInfo("Asia/Taipei")).strftime("%Y%m%d"),
)

startup_verifier = StartupPositionVerifier(
    client=broker_client,
    position_store=position_store,
    checkpoint_path=os.getenv("HFT_POSITION_CHECKPOINT_PATH", ".runtime/position_checkpoint.json"),
    qty_threshold=int(os.getenv("HFT_STARTUP_RECON_QTY_THRESHOLD", "10")),
    futures_qty_threshold=int(os.getenv("HFT_STARTUP_RECON_FUTURES_QTY_THRESHOLD", "2")),
)
```

Changes to `HFTSystem.run()`:

```python
# Before starting ReconciliationService:
if os.getenv("HFT_STARTUP_RECON_ENABLED", "1") == "1":
    recovery = await self.startup_verifier.recover()
    if recovery.halted:
        logger.critical("Position recovery HALT", result=recovery)
        # Send Telegram, set metrics, do NOT start trading
        return
    logger.info("Position recovery complete", result=recovery)

# Start checkpoint writer as background task
if os.getenv("HFT_CHECKPOINT_ENABLED", "1") == "1":
    self._start_service("checkpoint_writer", self.checkpoint_writer.run())
```

### 5. Degraded Recovery Scenarios

| Checkpoint | Broker | trading_date | Action |
|------------|--------|--------------|--------|
| Valid | Available | Matches | Dual-source validation → merge |
| Valid | Available | Stale | Broker-only + warn (checkpoint ignored) |
| Missing | Available | — | Broker-only + warn (first start) |
| Valid | Unavailable | Matches | Checkpoint-only + warn |
| Valid | Unavailable | Stale | HALT (no trustworthy source) |
| Missing | Unavailable | — | HALT (no source at all) → empty start only if `HFT_STARTUP_RECON_ENABLED=0` |

### 6. Notification Extension

Extend `NotificationDispatcher` with 2 methods:

| Method | Trigger | Critical |
|--------|---------|----------|
| `notify_position_recovery(source, loaded, corrected, mismatches)` | Recovery succeeded | No |
| `notify_position_recovery_failed(source, reason, mismatches)` | Recovery triggered HALT | Yes |

Template messages:

```
# Success
🟢 部位恢復完成
來源: {source} | 載入: {loaded} symbols | 修正: {corrected}
{mismatch_summary if any}

# Failure
🔴 部位恢復失敗 — HALT
來源: {source}
原因: {reason}
差異: {mismatch_details}
請手動確認部位後重啟
```

### 7. Prometheus Metrics

2 new Gauge metrics + semantic extension of existing metric:

| Metric | Type | Description |
|--------|------|-------------|
| `startup_recon_status` | Gauge | **Existing** (name without `hft_` prefix). Extend semantics: 0=not_run, 1=pass, 2=corrected (was: discrepancy), 3=halted (was: error), 4=error (new) |
| `startup_recon_positions_loaded` | Gauge | NEW: symbols loaded into PositionStore |
| `startup_recon_auto_corrected` | Gauge | NEW: discrepancies auto-corrected |

**Breaking change**: `startup_recon_status` value 2 changes from `discrepancy` to `corrected`, value 3 from `error` to `halted`, value 4 added as `error`. Any Grafana dashboards or alerts that fire on value 2 or 3 must be updated. Currently no Alertmanager rules reference this metric.

## Environment Variables

| Variable | Default | Purpose |
|----------|---------|---------|
| `HFT_CHECKPOINT_ENABLED` | `1` | Enable periodic checkpoint writing |
| `HFT_CHECKPOINT_INTERVAL_S` | `60` | Checkpoint write interval (already exists) |
| `HFT_POSITION_CHECKPOINT_PATH` | `.runtime/position_checkpoint.json` | Checkpoint file path (already exists) |
| `HFT_STARTUP_RECON_ENABLED` | `1` | Enable startup position recovery |
| `HFT_STARTUP_RECON_BLOCK` | `0` | Legacy: blocks startup in `verify()` mode. Superseded by `recover()` graduated response when `HFT_STARTUP_RECON_ENABLED=1`. Kept for backward compatibility. |
| `HFT_STARTUP_RECON_QTY_THRESHOLD` | `10` | Stock small discrepancy threshold (shares) |
| `HFT_STARTUP_RECON_FUTURES_QTY_THRESHOLD` | `2` | Futures small discrepancy threshold (contracts) |

## Testing Strategy

| Test | Type | What it validates |
|------|------|-------------------|
| Checkpoint with trading_date | Unit | trading_date included in write, parsed on load |
| Recovery dual-source match | Unit | Both sources agree → PositionStore populated |
| Recovery minor discrepancy | Unit | Small diff → auto-correct to broker, warn |
| Recovery critical discrepancy | Unit | Large diff or side mismatch → HALT |
| Recovery stale checkpoint | Unit | Wrong trading_date → broker-only |
| Recovery broker unavailable | Unit | Fallback to checkpoint-only + warn |
| Recovery both unavailable | Unit | HALT triggered |
| Checkpoint write-back to store | Unit | Positions correctly written to PositionStore |
| Notification templates | Unit | Template rendering for success/failure |
| Bootstrap wiring | Integration | System starts with recovery, checkpoint writer running |

## Risks and Mitigations

| Risk | Impact | Mitigation |
|------|--------|------------|
| Broker API slow on startup | Recovery delayed | Timeout (10s), fallback to checkpoint-only |
| Checkpoint corrupted | Invalid position data | SHA-256 verification (already implemented) |
| Stale checkpoint + broker down | Cannot recover | HALT + Telegram alert |
| Race: ReconciliationService starts before recovery | False discrepancy HALT | Enforce ordering in HFTSystem.run() |
| Night session checkpoint has "wrong" calendar date | Checkpoint rejected as stale | Use trading_date (not calendar date), matches SessionGovernor logic |

## File Inventory

| Action | File |
|--------|------|
| Modify | `src/hft_platform/execution/checkpoint.py` (add trading_date to format) |
| Modify | `src/hft_platform/execution/startup_recon.py` (add recover(), RecoveryResult, graduated response, write-back) |
| Modify | `src/hft_platform/services/bootstrap.py` (wire checkpoint writer + verifier) |
| Modify | `src/hft_platform/services/system.py` (call recover() before trading loop) |
| Modify | `src/hft_platform/notifications/dispatcher.py` (+2 methods) |
| Modify | `src/hft_platform/notifications/templates.py` (+2 functions) |
| Modify | `src/hft_platform/observability/metrics.py` (+2 Gauge metrics) |
| Modify | `src/hft_platform/services/registry.py` (register checkpoint_writer + startup_verifier) |
| Create | `tests/unit/test_position_recovery.py` |
| Create | `tests/unit/test_checkpoint_trading_date.py` |
| Modify | `.env.example` (+2 new env vars) |
| Modify | `CLAUDE.md` (env var table) |
