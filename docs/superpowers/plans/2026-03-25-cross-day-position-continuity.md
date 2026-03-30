# Cross-Day Position State Continuity — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Auto-warm PositionStore on restart from dual sources (checkpoint + broker), with graduated discrepancy handling and full observability.

**Architecture:** Extend existing `PositionCheckpointWriter` (add `trading_date` to checkpoint format) and `StartupPositionVerifier` (add `recover()` method with dual-source merge + graduated response). Wire both into `SystemBootstrapper.build()` and `HFTSystem.run()` so recovery runs before any trading services start.

**Tech Stack:** Existing `checkpoint.py` + `startup_recon.py`, `PositionStore`, broker `get_positions()` API, Prometheus Gauge, Telegram notifications.

**Spec:** `docs/superpowers/specs/2026-03-25-cross-day-position-continuity-design.md`

---

## File Structure

| Action | File | Responsibility |
|--------|------|----------------|
| Modify | `src/hft_platform/execution/checkpoint.py:49-68,94-117` | Add `_trading_date_provider` slot + `trading_date` in checkpoint format |
| Modify | `src/hft_platform/execution/startup_recon.py:26-82` | Add `RecoveryResult`, extend `__init__`, add `recover()` + helpers |
| Modify | `src/hft_platform/services/registry.py:64` | Add `checkpoint_writer` + `startup_verifier` optional fields |
| Modify | `src/hft_platform/services/bootstrap.py:708` | Wire checkpoint writer + startup verifier |
| Modify | `src/hft_platform/services/system.py:211-214` | Call `recover()` before recon/strategy start |
| Modify | `src/hft_platform/notifications/templates.py` | +2 render functions |
| Modify | `src/hft_platform/notifications/dispatcher.py` | +2 notify methods |
| — | `src/hft_platform/observability/metrics.py` | NOT modified — Gauges are module-level in `startup_recon.py` to avoid duplicate registration |
| Create | `tests/unit/test_checkpoint_trading_date.py` | Checkpoint format extension tests |
| Create | `tests/unit/test_position_recovery.py` | Recovery flow tests (all scenarios) |
| Create | `tests/unit/test_recovery_notifications.py` | Notification template + dispatcher tests |
| Modify | `.env.example` | +2 new env vars |
| Modify | `CLAUDE.md` | Env var table update |

---

### Task 1: Checkpoint Format Extension — trading_date

**Files:**
- Modify: `src/hft_platform/execution/checkpoint.py`
- Create: `tests/unit/test_checkpoint_trading_date.py`

- [ ] **Step 1: Write failing tests**

Create `tests/unit/test_checkpoint_trading_date.py`:

```python
"""Tests for checkpoint trading_date extension."""

from __future__ import annotations

from unittest.mock import MagicMock


def _make_store(positions=None):
    """Create a mock PositionStore."""
    store = MagicMock()
    store.positions = positions or {}
    return store


def test_checkpoint_includes_trading_date(tmp_path):
    from hft_platform.execution.checkpoint import PositionCheckpointWriter

    path = str(tmp_path / "ckpt.json")
    store = _make_store()
    writer = PositionCheckpointWriter(
        store=store,
        path=path,
        trading_date_provider=lambda: "20260325",
    )
    writer.write_checkpoint()

    data = PositionCheckpointWriter.load_checkpoint(path)
    assert data is not None
    assert data.get("trading_date") == "20260325"


def test_checkpoint_trading_date_covered_by_sha256(tmp_path):
    """Changing trading_date should invalidate the SHA-256 hash."""
    import json

    from hft_platform.execution.checkpoint import PositionCheckpointWriter

    path = str(tmp_path / "ckpt.json")
    store = _make_store()
    writer = PositionCheckpointWriter(
        store=store,
        path=path,
        trading_date_provider=lambda: "20260325",
    )
    writer.write_checkpoint()

    # Tamper with trading_date
    with open(path, "rb") as f:
        raw = json.loads(f.read())
    raw["trading_date"] = "20260326"  # tamper
    with open(path, "w") as f:
        json.dump(raw, f)

    # Should fail verification
    data = PositionCheckpointWriter.load_checkpoint(path)
    assert data is None, "Tampered trading_date should fail SHA-256 check"


def test_checkpoint_backward_compat_no_trading_date(tmp_path):
    """Old checkpoint without trading_date should still load."""
    import hashlib
    import json

    path = str(tmp_path / "ckpt.json")

    # Write old-format checkpoint (no trading_date)
    body = {"timestamp_ns": 123456, "positions": {}}
    body_bytes = json.dumps(body, separators=(",", ":")).encode()
    sha = hashlib.sha256(body_bytes).hexdigest()
    body["sha256"] = sha
    with open(path, "w") as f:
        json.dump(body, f)

    from hft_platform.execution.checkpoint import PositionCheckpointWriter

    data = PositionCheckpointWriter.load_checkpoint(path)
    assert data is not None
    assert data.get("trading_date") is None  # missing field → None


def test_checkpoint_default_trading_date_provider(tmp_path):
    """Without explicit provider, uses current date."""
    from hft_platform.execution.checkpoint import PositionCheckpointWriter

    path = str(tmp_path / "ckpt.json")
    store = _make_store()
    writer = PositionCheckpointWriter(store=store, path=path)
    writer.write_checkpoint()

    data = PositionCheckpointWriter.load_checkpoint(path)
    assert data is not None
    # trading_date should be today's date (YYYYMMDD format)
    td = data.get("trading_date")
    assert td is not None
    assert len(td) == 8
    assert td.isdigit()
```

- [ ] **Step 2: Run tests — they should FAIL**

```bash
uv run pytest tests/unit/test_checkpoint_trading_date.py -v --no-cov
```

Expected: FAIL (TypeError: `__init__()` got unexpected keyword argument `trading_date_provider`)

- [ ] **Step 3: Implement checkpoint extension**

Modify `src/hft_platform/execution/checkpoint.py`:

**a)** Add import at top (after existing imports):

```python
from datetime import datetime
from typing import Callable
from zoneinfo import ZoneInfo
```

**b)** Extend `__slots__` (line 49):

```python
    __slots__ = (
        "_store",
        "_path",
        "_interval_s",
        "_trading_date_provider",
        "running",
    )
```

**c)** Extend `__init__` (line 56-68) — add `trading_date_provider` parameter:

After `self.running = False`, add:

```python
        self._trading_date_provider: Callable[[], str] = trading_date_provider or (
            lambda: datetime.now(tz=ZoneInfo("Asia/Taipei")).strftime("%Y%m%d")
        )
```

And update the signature to:

```python
    def __init__(
        self,
        store: PositionStore,
        path: Optional[str] = None,
        interval_s: Optional[float] = None,
        trading_date_provider: Optional[Callable[[], str]] = None,
    ) -> None:
```

**d)** In `write_checkpoint()` (line 108-111), add `trading_date` to `body_obj` BEFORE the SHA-256 computation:

```python
        body_obj = {
            "trading_date": self._trading_date_provider(),
            "timestamp_ns": now_ns(),
            "positions": positions_payload,
        }
```

- [ ] **Step 4: Run tests — they should PASS**

```bash
uv run pytest tests/unit/test_checkpoint_trading_date.py -v --no-cov
```

Expected: 4 passed

- [ ] **Step 5: Commit**

```bash
git add src/hft_platform/execution/checkpoint.py tests/unit/test_checkpoint_trading_date.py
git commit -m "feat(checkpoint): add trading_date to checkpoint format with SHA-256 coverage"
```

---

### Task 2: RecoveryResult dataclass + StartupPositionVerifier.__init__ extension

**Files:**
- Modify: `src/hft_platform/execution/startup_recon.py`
- Create: `tests/unit/test_position_recovery.py`

- [ ] **Step 1: Write failing tests**

Create `tests/unit/test_position_recovery.py`:

```python
"""Tests for startup position recovery flow."""

from __future__ import annotations

import os
from dataclasses import fields
from unittest.mock import MagicMock


def _make_store():
    store = MagicMock()
    store.positions = {}
    return store


def _make_client(positions=None):
    client = MagicMock()
    client.get_positions.return_value = positions or []
    return client


def test_recovery_result_dataclass():
    from hft_platform.execution.startup_recon import RecoveryResult

    r = RecoveryResult(
        source="dual",
        positions_loaded=3,
        auto_corrected=1,
        halted=False,
        mismatches=[{"symbol": "2330", "action": "corrected"}],
    )
    assert r.source == "dual"
    assert r.positions_loaded == 3
    assert r.halted is False
    field_names = {f.name for f in fields(r)}
    assert field_names == {"source", "positions_loaded", "auto_corrected", "halted", "mismatches"}


def test_verifier_accepts_threshold_params():
    from hft_platform.execution.startup_recon import StartupPositionVerifier

    v = StartupPositionVerifier(
        client=_make_client(),
        position_store=_make_store(),
        qty_threshold=20,
        futures_qty_threshold=5,
    )
    assert v._qty_threshold == 20
    assert v._futures_qty_threshold == 5


def test_verifier_threshold_defaults_from_env():
    from hft_platform.execution.startup_recon import StartupPositionVerifier

    with MagicMock() as _:
        os.environ["HFT_STARTUP_RECON_QTY_THRESHOLD"] = "15"
        os.environ["HFT_STARTUP_RECON_FUTURES_QTY_THRESHOLD"] = "3"
        try:
            v = StartupPositionVerifier(
                client=_make_client(),
                position_store=_make_store(),
            )
            assert v._qty_threshold == 15
            assert v._futures_qty_threshold == 3
        finally:
            os.environ.pop("HFT_STARTUP_RECON_QTY_THRESHOLD", None)
            os.environ.pop("HFT_STARTUP_RECON_FUTURES_QTY_THRESHOLD", None)
```

- [ ] **Step 2: Run tests — they should FAIL**

```bash
uv run pytest tests/unit/test_position_recovery.py -v --no-cov
```

Expected: FAIL (ImportError: cannot import name 'RecoveryResult')

- [ ] **Step 3: Implement RecoveryResult + __init__ extension**

Modify `src/hft_platform/execution/startup_recon.py`:

**a)** Add import at top:

```python
from dataclasses import dataclass, field
```

**b)** Add `RecoveryResult` after the module-level `_BLOCK_ENV` / `_CHECKPOINT_PATH_ENV` constants (line 34):

```python
@dataclass
class RecoveryResult:
    """Outcome of startup position recovery."""

    source: str  # "dual", "broker_only", "checkpoint_only", "empty"
    positions_loaded: int = 0
    auto_corrected: int = 0
    halted: bool = False
    mismatches: list[dict] = field(default_factory=list)
```

**c)** Add 2 new module-level Gauges after the existing `startup_recon_status` (line 31):

```python
startup_recon_positions_loaded = Gauge(
    "startup_recon_positions_loaded",
    "Number of symbols loaded into PositionStore at startup",
)
startup_recon_auto_corrected = Gauge(
    "startup_recon_auto_corrected",
    "Number of position discrepancies auto-corrected at startup",
)
```

**d)** Extend `StartupPositionVerifier.__init__` (line 61-82) — add threshold parameters:

```python
    def __init__(
        self,
        client: Any,
        position_store: PositionStore,
        *,
        blocking: bool | None = None,
        checkpoint_path: str | None = None,
        qty_threshold: int | None = None,
        futures_qty_threshold: int | None = None,
    ) -> None:
        self.client = client
        self.store = position_store

        if blocking is not None:
            self.blocking = blocking
        else:
            self.blocking = os.environ.get(_BLOCK_ENV, "0") == "1"

        self.checkpoint_path = checkpoint_path or os.environ.get(_CHECKPOINT_PATH_ENV)

        self._qty_threshold = qty_threshold if qty_threshold is not None else int(
            os.environ.get("HFT_STARTUP_RECON_QTY_THRESHOLD", "10")
        )
        self._futures_qty_threshold = futures_qty_threshold if futures_qty_threshold is not None else int(
            os.environ.get("HFT_STARTUP_RECON_FUTURES_QTY_THRESHOLD", "2")
        )

        self.discrepancies: List[PositionDiscrepancy] = []
        self.status: int = 0
```

- [ ] **Step 4: Run tests — they should PASS**

```bash
uv run pytest tests/unit/test_position_recovery.py -v --no-cov
```

Expected: 3 passed

- [ ] **Step 5: Commit**

```bash
git add src/hft_platform/execution/startup_recon.py tests/unit/test_position_recovery.py
git commit -m "feat(startup_recon): add RecoveryResult dataclass and threshold params"
```

---

### Task 3: recover() method — dual-source merge + graduated response

**Files:**
- Modify: `src/hft_platform/execution/startup_recon.py`
- Modify: `tests/unit/test_position_recovery.py`

- [ ] **Step 1: Write failing tests**

Append to `tests/unit/test_position_recovery.py`:

```python
import asyncio
from pathlib import Path


def _write_checkpoint(path, trading_date, positions):
    """Write a valid checkpoint file for testing."""
    from hft_platform.execution.checkpoint import PositionCheckpointWriter

    store = MagicMock()
    from hft_platform.execution.positions import Position

    store.positions = {}
    for sym, data in positions.items():
        pos = Position(
            account_id="test", strategy_id="", symbol=sym,
            net_qty=data["net_qty"],
            avg_price_scaled=data.get("avg_price_scaled", 0),
            realized_pnl_scaled=data.get("realized_pnl_scaled", 0),
        )
        store.positions[f"test::{sym}"] = pos

    writer = PositionCheckpointWriter(
        store=store, path=str(path),
        trading_date_provider=lambda: trading_date,
    )
    writer.write_checkpoint()


def test_recover_dual_source_match(tmp_path):
    """Both sources agree → PositionStore populated, no corrections."""
    from hft_platform.execution.startup_recon import StartupPositionVerifier

    ckpt_path = str(tmp_path / "ckpt.json")
    _write_checkpoint(ckpt_path, "20260325", {"2330": {"net_qty": 1000}})

    broker_positions = [{"code": "2330", "quantity": 1000}]
    store = _make_store()
    verifier = StartupPositionVerifier(
        client=_make_client(broker_positions),
        position_store=store,
        checkpoint_path=ckpt_path,
    )

    result = asyncio.run(verifier.recover(trading_date="20260325", account_id="test"))
    assert result.source == "dual"
    assert result.positions_loaded == 1
    assert result.auto_corrected == 0
    assert result.halted is False
    assert len(store.positions) == 1


def test_recover_minor_discrepancy_auto_corrects(tmp_path):
    """Small qty difference → auto-correct to broker, warn."""
    from hft_platform.execution.startup_recon import StartupPositionVerifier

    ckpt_path = str(tmp_path / "ckpt.json")
    _write_checkpoint(ckpt_path, "20260325", {"2330": {"net_qty": 1000}})

    broker_positions = [{"code": "2330", "quantity": 1005}]  # diff = 5, threshold = 10
    store = _make_store()
    verifier = StartupPositionVerifier(
        client=_make_client(broker_positions),
        position_store=store,
        checkpoint_path=ckpt_path,
        qty_threshold=10,
    )

    result = asyncio.run(verifier.recover(trading_date="20260325", account_id="test"))
    assert result.source == "dual"
    assert result.auto_corrected == 1
    assert result.halted is False
    # Broker qty should win
    key = "test::2330"
    assert store.positions[key].net_qty == 1005


def test_recover_critical_discrepancy_halts(tmp_path):
    """Large qty difference → HALT."""
    from hft_platform.execution.startup_recon import StartupPositionVerifier

    ckpt_path = str(tmp_path / "ckpt.json")
    _write_checkpoint(ckpt_path, "20260325", {"2330": {"net_qty": 1000}})

    broker_positions = [{"code": "2330", "quantity": 100}]  # diff = 900 >> 10
    store = _make_store()
    verifier = StartupPositionVerifier(
        client=_make_client(broker_positions),
        position_store=store,
        checkpoint_path=ckpt_path,
        qty_threshold=10,
    )

    result = asyncio.run(verifier.recover(trading_date="20260325", account_id="test"))
    assert result.halted is True
    assert len(store.positions) == 0  # nothing written on HALT


def test_recover_side_mismatch_halts(tmp_path):
    """Checkpoint long, broker short → always HALT."""
    from hft_platform.execution.startup_recon import StartupPositionVerifier

    ckpt_path = str(tmp_path / "ckpt.json")
    _write_checkpoint(ckpt_path, "20260325", {"2330": {"net_qty": 100}})

    broker_positions = [{"code": "2330", "quantity": -50, "direction": "Action.Sell"}]
    store = _make_store()
    verifier = StartupPositionVerifier(
        client=_make_client(broker_positions),
        position_store=store,
        checkpoint_path=ckpt_path,
    )

    result = asyncio.run(verifier.recover(trading_date="20260325", account_id="test"))
    assert result.halted is True


def test_recover_stale_checkpoint_broker_only(tmp_path):
    """Checkpoint from wrong trading date → use broker only."""
    from hft_platform.execution.startup_recon import StartupPositionVerifier

    ckpt_path = str(tmp_path / "ckpt.json")
    _write_checkpoint(ckpt_path, "20260324", {"2330": {"net_qty": 500}})  # stale

    broker_positions = [{"code": "2330", "quantity": 1000}]
    store = _make_store()
    verifier = StartupPositionVerifier(
        client=_make_client(broker_positions),
        position_store=store,
        checkpoint_path=ckpt_path,
    )

    result = asyncio.run(verifier.recover(trading_date="20260325", account_id="test"))
    assert result.source == "broker_only"
    assert result.positions_loaded == 1
    assert result.halted is False
    assert store.positions["test::2330"].net_qty == 1000


def test_recover_broker_unavailable_checkpoint_only(tmp_path):
    """Broker fails, valid checkpoint → use checkpoint only."""
    from hft_platform.execution.startup_recon import StartupPositionVerifier

    ckpt_path = str(tmp_path / "ckpt.json")
    _write_checkpoint(ckpt_path, "20260325", {"2330": {"net_qty": 500, "avg_price_scaled": 6500000}})

    client = _make_client()
    client.get_positions.side_effect = Exception("broker down")
    store = _make_store()
    verifier = StartupPositionVerifier(
        client=client,
        position_store=store,
        checkpoint_path=ckpt_path,
    )

    result = asyncio.run(verifier.recover(trading_date="20260325", account_id="test"))
    assert result.source == "checkpoint_only"
    assert result.positions_loaded == 1
    assert result.halted is False


def test_recover_both_unavailable_halts(tmp_path):
    """No checkpoint + broker fails → HALT."""
    from hft_platform.execution.startup_recon import StartupPositionVerifier

    client = _make_client()
    client.get_positions.side_effect = Exception("broker down")
    store = _make_store()
    verifier = StartupPositionVerifier(
        client=client,
        position_store=store,
        checkpoint_path=str(tmp_path / "nonexistent.json"),
    )

    result = asyncio.run(verifier.recover(trading_date="20260325", account_id="test"))
    assert result.halted is True
    assert result.source == "empty"


def test_recover_no_checkpoint_broker_only(tmp_path):
    """No checkpoint file, broker available → broker only."""
    from hft_platform.execution.startup_recon import StartupPositionVerifier

    broker_positions = [{"code": "TXFD6", "quantity": 2}]
    store = _make_store()
    verifier = StartupPositionVerifier(
        client=_make_client(broker_positions),
        position_store=store,
        checkpoint_path=str(tmp_path / "nonexistent.json"),
    )

    result = asyncio.run(verifier.recover(trading_date="20260325", account_id="test"))
    assert result.source == "broker_only"
    assert result.positions_loaded == 1
    assert result.halted is False
```

- [ ] **Step 2: Run tests — they should FAIL**

```bash
uv run pytest tests/unit/test_position_recovery.py -v --no-cov
```

Expected: FAIL (AttributeError: 'StartupPositionVerifier' object has no attribute 'recover')

- [ ] **Step 3: Implement recover() and helpers**

Add to `StartupPositionVerifier` class in `src/hft_platform/execution/startup_recon.py`, after `_build_local_map`:

```python
    # ------------------------------------------------------------------
    # Position Recovery (dual-source merge + graduated response)
    # ------------------------------------------------------------------

    async def recover(
        self,
        *,
        trading_date: str | None = None,
        account_id: str = "default",
    ) -> RecoveryResult:
        """Dual-source position recovery with graduated response.

        Args:
            trading_date: Current trading date (YYYYMMDD). Defaults to today in Asia/Taipei.
            account_id: Broker account ID for PositionStore key construction.
        """
        from datetime import datetime
        from zoneinfo import ZoneInfo

        from hft_platform.execution.checkpoint import PositionCheckpointWriter

        if trading_date is None:
            trading_date = datetime.now(tz=ZoneInfo("Asia/Taipei")).strftime("%Y%m%d")

        logger.info("position_recovery: starting", trading_date=trading_date)

        # 1. Load checkpoint
        ckpt_data = None
        ckpt_positions: Dict[str, Dict[str, Any]] = {}
        ckpt_valid = False

        if self.checkpoint_path:
            ckpt_data = PositionCheckpointWriter.load_checkpoint(self.checkpoint_path)
            if ckpt_data is not None:
                ckpt_td = ckpt_data.get("trading_date")
                if ckpt_td == trading_date:
                    ckpt_valid = True
                    ckpt_positions = ckpt_data.get("positions", {})
                    logger.info("position_recovery: checkpoint valid", symbols=len(ckpt_positions))
                else:
                    logger.warning(
                        "position_recovery: checkpoint stale",
                        checkpoint_date=ckpt_td,
                        current_date=trading_date,
                    )
            else:
                logger.info("position_recovery: no checkpoint found")

        # 2. Query broker
        broker_map: Dict[str, int] = {}
        broker_available = False
        try:
            broker_map = await self._fetch_broker_positions()
            broker_available = True
            logger.info("position_recovery: broker positions fetched", symbols=len(broker_map))
        except Exception as exc:
            logger.warning("position_recovery: broker unavailable", error=str(exc))

        # 3. Determine source and act
        if ckpt_valid and broker_available:
            return self._recover_dual(ckpt_positions, broker_map, account_id)
        elif broker_available:
            return self._recover_broker_only(broker_map, account_id)
        elif ckpt_valid:
            return self._recover_checkpoint_only(ckpt_positions, account_id)
        else:
            # Both unavailable
            startup_recon_status.set(3)  # halted
            return RecoveryResult(source="empty", halted=True)

    def _recover_dual(
        self,
        ckpt_positions: Dict[str, Dict[str, Any]],
        broker_map: Dict[str, int],
        account_id: str,
    ) -> RecoveryResult:
        """Cross-validate checkpoint vs broker, apply graduated response."""
        all_symbols = set(broker_map.keys())
        for pos_data in ckpt_positions.values():
            sym = pos_data.get("symbol", "")
            if sym:
                all_symbols.add(sym)

        # Build checkpoint maps keyed by SYMBOL (not composite key)
        # ckpt_positions uses composite keys (e.g. "account::2330"), so we
        # re-index by pos_data["symbol"] to match broker_map's symbol keys.
        ckpt_qty_map: Dict[str, int] = {}
        ckpt_by_symbol: Dict[str, Dict[str, Any]] = {}
        for _key, pos_data in ckpt_positions.items():
            sym = pos_data.get("symbol", _key)
            ckpt_qty_map[sym] = pos_data.get("net_qty", 0)
            ckpt_by_symbol[sym] = pos_data

        mismatches: list[dict] = []
        has_critical = False
        auto_corrected = 0
        merged: Dict[str, Dict[str, Any]] = {}

        for symbol in all_symbols:
            ckpt_qty = ckpt_qty_map.get(symbol, 0)
            broker_qty = broker_map.get(symbol, 0)
            classification = self._classify_discrepancy(symbol, ckpt_qty, broker_qty)

            if classification == "critical":
                has_critical = True
                mismatches.append({
                    "symbol": symbol,
                    "checkpoint_qty": ckpt_qty,
                    "broker_qty": broker_qty,
                    "action": "halt",
                })
            elif classification == "minor":
                auto_corrected += 1
                mismatches.append({
                    "symbol": symbol,
                    "checkpoint_qty": ckpt_qty,
                    "broker_qty": broker_qty,
                    "action": "corrected",
                })
                # Broker wins — use broker qty with checkpoint's avg_price
                ckpt_entry = ckpt_by_symbol.get(symbol, {})
                merged[symbol] = {
                    "net_qty": broker_qty,
                    "avg_price_scaled": ckpt_entry.get("avg_price_scaled", 0),
                    "realized_pnl_scaled": ckpt_entry.get("realized_pnl_scaled", 0),
                }
            else:
                # Match — use checkpoint data (richer, has avg_price)
                ckpt_entry = ckpt_by_symbol.get(symbol, {})
                if broker_qty != 0:
                    merged[symbol] = {
                        "net_qty": broker_qty,
                        "avg_price_scaled": ckpt_entry.get("avg_price_scaled", 0),
                        "realized_pnl_scaled": ckpt_entry.get("realized_pnl_scaled", 0),
                    }

        if has_critical:
            startup_recon_status.set(3)  # halted
            return RecoveryResult(
                source="dual", halted=True, mismatches=mismatches,
            )

        loaded = self._write_to_store(merged, account_id)
        status_val = 2 if auto_corrected > 0 else 1
        startup_recon_status.set(status_val)
        startup_recon_positions_loaded.set(loaded)
        startup_recon_auto_corrected.set(auto_corrected)

        return RecoveryResult(
            source="dual",
            positions_loaded=loaded,
            auto_corrected=auto_corrected,
            mismatches=mismatches,
        )

    def _recover_broker_only(
        self, broker_map: Dict[str, int], account_id: str,
    ) -> RecoveryResult:
        """Use broker positions only (no valid checkpoint)."""
        merged: Dict[str, Dict[str, Any]] = {}
        for symbol, qty in broker_map.items():
            if qty != 0:
                merged[symbol] = {"net_qty": qty, "avg_price_scaled": 0, "realized_pnl_scaled": 0}

        loaded = self._write_to_store(merged, account_id)
        startup_recon_status.set(1)
        startup_recon_positions_loaded.set(loaded)
        return RecoveryResult(source="broker_only", positions_loaded=loaded)

    def _recover_checkpoint_only(
        self, ckpt_positions: Dict[str, Dict[str, Any]], account_id: str,
    ) -> RecoveryResult:
        """Use checkpoint positions only (broker unavailable)."""
        merged: Dict[str, Dict[str, Any]] = {}
        for _key, pos_data in ckpt_positions.items():
            sym = pos_data.get("symbol", _key)
            qty = pos_data.get("net_qty", 0)
            if qty != 0:
                merged[sym] = {
                    "net_qty": qty,
                    "avg_price_scaled": pos_data.get("avg_price_scaled", 0),
                    "realized_pnl_scaled": pos_data.get("realized_pnl_scaled", 0),
                }

        loaded = self._write_to_store(merged, account_id)
        startup_recon_status.set(1)
        startup_recon_positions_loaded.set(loaded)
        return RecoveryResult(source="checkpoint_only", positions_loaded=loaded)

    def _classify_discrepancy(self, symbol: str, ckpt_qty: int, broker_qty: int) -> str:
        """Returns 'match', 'minor', or 'critical'."""
        if ckpt_qty == broker_qty:
            return "match"
        diff = abs(ckpt_qty - broker_qty)
        # Side mismatch → always critical
        if (ckpt_qty > 0 and broker_qty < 0) or (ckpt_qty < 0 and broker_qty > 0):
            return "critical"
        threshold = self._futures_qty_threshold if self._is_futures(symbol) else self._qty_threshold
        return "minor" if diff <= threshold else "critical"

    @staticmethod
    def _is_futures(symbol: str) -> bool:
        """Heuristic: futures symbols contain 'F' or 'O' and end with digits."""
        return any(c in symbol.upper() for c in ("FD", "FX", "TX", "MX", "TE", "TF"))

    def _write_to_store(self, positions: Dict[str, Dict[str, Any]], account_id: str) -> int:
        """Write recovered positions into PositionStore. Returns count."""
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

- [ ] **Step 4: Run ALL tests**

```bash
uv run pytest tests/unit/test_position_recovery.py -v --no-cov
```

Expected: 11 passed

- [ ] **Step 5: Commit**

```bash
git add src/hft_platform/execution/startup_recon.py tests/unit/test_position_recovery.py
git commit -m "feat(startup_recon): add recover() with dual-source merge and graduated response"
```

---

### Task 4: Notification Templates + Dispatcher

**Files:**
- Modify: `src/hft_platform/notifications/templates.py`
- Modify: `src/hft_platform/notifications/dispatcher.py`
- Create: `tests/unit/test_recovery_notifications.py`

- [ ] **Step 1: Write failing tests**

Create `tests/unit/test_recovery_notifications.py`:

```python
"""Tests for position recovery notification templates and dispatcher."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock


def test_render_position_recovery():
    from hft_platform.notifications.templates import render_position_recovery

    msg = render_position_recovery(
        source="dual", loaded=5, corrected=1,
        mismatches=[{"symbol": "2330", "action": "corrected"}],
    )
    assert "dual" in msg
    assert "5" in msg
    assert "2330" in msg


def test_render_position_recovery_failed():
    from hft_platform.notifications.templates import render_position_recovery_failed

    msg = render_position_recovery_failed(
        source="dual", reason="Side mismatch on 2330",
        mismatches=[{"symbol": "2330", "checkpoint_qty": 100, "broker_qty": -50}],
    )
    assert "HALT" in msg or "失敗" in msg
    assert "2330" in msg


def test_notify_position_recovery_sends_non_critical():
    from hft_platform.notifications.dispatcher import NotificationDispatcher

    sender = MagicMock()
    sender.send = AsyncMock()
    d = NotificationDispatcher(sender=sender)
    asyncio.run(d.notify_position_recovery(source="dual", loaded=3, corrected=0, mismatches=[]))
    sender.send.assert_called_once()
    assert sender.send.call_args.kwargs.get("critical") is False


def test_notify_position_recovery_failed_sends_critical():
    from hft_platform.notifications.dispatcher import NotificationDispatcher

    sender = MagicMock()
    sender.send = AsyncMock()
    d = NotificationDispatcher(sender=sender)
    asyncio.run(d.notify_position_recovery_failed(source="dual", reason="test", mismatches=[]))
    sender.send.assert_called_once()
    assert sender.send.call_args.kwargs.get("critical") is True
```

- [ ] **Step 2: Run tests — they should FAIL**

```bash
uv run pytest tests/unit/test_recovery_notifications.py -v --no-cov
```

- [ ] **Step 3: Add templates**

Append to `src/hft_platform/notifications/templates.py`:

```python


def render_position_recovery(
    *,
    source: str,
    loaded: int,
    corrected: int,
    mismatches: list[dict],
) -> str:
    """Startup position recovery succeeded.

    Args:
        source: Recovery source ("dual", "broker_only", "checkpoint_only").
        loaded: Number of symbols loaded into PositionStore.
        corrected: Number of auto-corrected discrepancies.
        mismatches: List of discrepancy dicts.

    Returns:
        Formatted recovery success notification string.
    """
    lines = [
        f"🟢 部位恢復完成",
        f"來源: {source} | 載入: {loaded} symbols | 修正: {corrected}",
    ]
    for m in mismatches[:5]:
        lines.append(f"  {m.get('symbol', '?')}: ckpt={m.get('checkpoint_qty', '?')} broker={m.get('broker_qty', '?')} → {m.get('action', '?')}")
    return "\n".join(lines)


def render_position_recovery_failed(
    *,
    source: str,
    reason: str,
    mismatches: list[dict],
) -> str:
    """Startup position recovery failed — HALT triggered.

    Args:
        source: Recovery source attempted.
        reason: Failure reason.
        mismatches: List of discrepancy dicts.

    Returns:
        Formatted recovery failure notification string.
    """
    lines = [
        f"🔴 部位恢復失敗 — HALT",
        f"來源: {source}",
        f"原因: {reason}",
    ]
    for m in mismatches[:5]:
        lines.append(f"  {m.get('symbol', '?')}: ckpt={m.get('checkpoint_qty', '?')} broker={m.get('broker_qty', '?')}")
    lines.append("請手動確認部位後重啟")
    return "\n".join(lines)
```

- [ ] **Step 4: Add dispatcher methods**

Append to `NotificationDispatcher` class in `src/hft_platform/notifications/dispatcher.py`:

```python
    async def notify_position_recovery(
        self,
        *,
        source: str,
        loaded: int,
        corrected: int,
        mismatches: list[dict],
    ) -> None:
        """Notify operator of successful position recovery."""
        msg = templates.render_position_recovery(
            source=source, loaded=loaded, corrected=corrected, mismatches=mismatches,
        )
        logger.info("dispatcher.notify_position_recovery", source=source, loaded=loaded)
        await self._sender.send(msg, critical=False)

    async def notify_position_recovery_failed(
        self,
        *,
        source: str,
        reason: str,
        mismatches: list[dict],
    ) -> None:
        """Notify operator of failed position recovery (HALT)."""
        msg = templates.render_position_recovery_failed(
            source=source, reason=reason, mismatches=mismatches,
        )
        logger.warning("dispatcher.notify_position_recovery_failed", source=source, reason=reason)
        await self._sender.send(msg, critical=True)
```

- [ ] **Step 5: Run tests — they should PASS**

```bash
uv run pytest tests/unit/test_recovery_notifications.py -v --no-cov
```

Expected: 4 passed

- [ ] **Step 6: Commit**

```bash
git add src/hft_platform/notifications/templates.py src/hft_platform/notifications/dispatcher.py tests/unit/test_recovery_notifications.py
git commit -m "feat(notifications): add position recovery success/failure templates and dispatcher"
```

---

### Task 5: Prometheus Metrics — Module-Level Only (No MetricsRegistry)

**No files to modify.** The 2 new Gauges (`startup_recon_positions_loaded`, `startup_recon_auto_corrected`) and the existing `startup_recon_status` are all defined as **module-level Gauges** in `startup_recon.py` (added in Task 2). They are NOT added to `MetricsRegistry` to avoid duplicate registration crashes from `prometheus_client`. This matches the existing pattern where `startup_recon_status` was already module-level.

- [ ] **Step 1: Verify all 3 gauges are accessible**

```bash
uv run python -c "
from hft_platform.execution.startup_recon import startup_recon_status, startup_recon_positions_loaded, startup_recon_auto_corrected
print('startup_recon_status:', startup_recon_status._value.get())
print('startup_recon_positions_loaded:', startup_recon_positions_loaded._value.get())
print('startup_recon_auto_corrected:', startup_recon_auto_corrected._value.get())
print('All 3 startup recovery gauges accessible')
"
```

- [ ] **Step 2: No commit needed** (metrics were already added in Task 2)

---

### Task 6: ServiceRegistry + Bootstrap Wiring

**Files:**
- Modify: `src/hft_platform/services/registry.py:64`
- Modify: `src/hft_platform/services/bootstrap.py:708`

- [ ] **Step 1: Add fields to ServiceRegistry**

In `src/hft_platform/services/registry.py`, after `autonomy_monitor` field (end of class), add:

```python
    checkpoint_writer: Optional[Any] = field(default=None)
    startup_verifier: Optional[Any] = field(default=None)
```

- [ ] **Step 2: Wire in bootstrap.py**

In `src/hft_platform/services/bootstrap.py`, after `position_store = PositionStore()` (around line 708), add:

```python
        # Position checkpoint writer (periodic serialization)
        from hft_platform.execution.checkpoint import PositionCheckpointWriter
        checkpoint_writer = PositionCheckpointWriter(store=position_store)

        # Startup position verifier (dual-source recovery)
        from hft_platform.execution.startup_recon import StartupPositionVerifier
        startup_verifier = StartupPositionVerifier(
            client=client,
            position_store=position_store,
            checkpoint_path=os.getenv("HFT_POSITION_CHECKPOINT_PATH", ".runtime/position_checkpoint.json"),
        )
```

Then find where the `ServiceRegistry(...)` constructor is called and add:

```python
            checkpoint_writer=checkpoint_writer,
            startup_verifier=startup_verifier,
```

- [ ] **Step 3: Commit**

```bash
git add src/hft_platform/services/registry.py src/hft_platform/services/bootstrap.py
git commit -m "feat(bootstrap): wire PositionCheckpointWriter and StartupPositionVerifier"
```

---

### Task 7: HFTSystem.run() — Call recover() Before Trading

**Files:**
- Modify: `src/hft_platform/services/system.py:95-215`

- [ ] **Step 1: Add attributes to HFTSystem.__init__**

In `HFTSystem.__init__`, after the existing registry attribute assignments (around line 104), add:

```python
        self.checkpoint_writer = self.registry.checkpoint_writer
        self.startup_verifier = self.registry.startup_verifier
```

- [ ] **Step 2: Add recovery call in run()**

In `HFTSystem.run()`, BEFORE the line `self._start_service("recon", self.recon_service.run())` (line 214), add:

```python
            # ── Position Recovery (must complete before recon + strategy) ──
            if os.getenv("HFT_STARTUP_RECON_ENABLED", "1") == "1" and self.startup_verifier:
                try:
                    recovery = await self.startup_verifier.recover(
                        account_id=self.registry.broker_id,
                    )
                    if recovery.halted:
                        logger.critical(
                            "Position recovery HALT — refusing to start trading",
                            source=recovery.source,
                            mismatches=recovery.mismatches,
                        )
                        return
                    logger.info(
                        "Position recovery complete",
                        source=recovery.source,
                        loaded=recovery.positions_loaded,
                        corrected=recovery.auto_corrected,
                    )
                except Exception as exc:
                    logger.critical("Position recovery failed", error=str(exc))
                    return

            # ── Checkpoint Writer (after recovery, before trading) ──
            if os.getenv("HFT_CHECKPOINT_ENABLED", "1") == "1" and self.checkpoint_writer:
                self._start_service("checkpoint_writer", self.checkpoint_writer.run())
```

- [ ] **Step 3: Commit**

```bash
git add src/hft_platform/services/system.py
git commit -m "feat(system): call position recovery before trading loop, start checkpoint writer"
```

---

### Task 8: Lint + CI Verification

- [ ] **Step 1: Run ruff on all changed files**

```bash
uv run ruff check src/hft_platform/execution/checkpoint.py src/hft_platform/execution/startup_recon.py src/hft_platform/services/bootstrap.py src/hft_platform/services/system.py src/hft_platform/notifications/templates.py src/hft_platform/notifications/dispatcher.py
```

- [ ] **Step 2: Run all new tests**

```bash
uv run pytest tests/unit/test_checkpoint_trading_date.py tests/unit/test_position_recovery.py tests/unit/test_recovery_notifications.py -v --no-cov
```

Expected: all pass

- [ ] **Step 3: Run mypy on key files**

```bash
uv run mypy src/hft_platform/execution/checkpoint.py src/hft_platform/execution/startup_recon.py --ignore-missing-imports
```

- [ ] **Step 4: Fix any issues and commit**

```bash
git add -u
git commit -m "fix: lint/typecheck fixes for position recovery"
```

---

### Task 9: Documentation Update

**Files:**
- Modify: `.env.example`
- Modify: `CLAUDE.md`
- Modify: `docs/superpowers/specs/2026-03-25-cross-day-position-continuity-design.md`

- [ ] **Step 1: Update .env.example**

Append:

```bash
# ─── Position Recovery ──────────────────────────────────────
# HFT_STARTUP_RECON_ENABLED=1             # Enable startup position recovery (default: 1)
# HFT_STARTUP_RECON_QTY_THRESHOLD=10      # Stock discrepancy threshold (default: 10)
# HFT_STARTUP_RECON_FUTURES_QTY_THRESHOLD=2  # Futures discrepancy threshold (default: 2)
# HFT_CHECKPOINT_ENABLED=1                # Enable periodic position checkpoint (default: 1)
```

- [ ] **Step 2: Update CLAUDE.md env var table**

Add after the backup env vars:

```markdown
| `HFT_STARTUP_RECON_ENABLED`              | `1`   | Enable startup position recovery            |
| `HFT_STARTUP_RECON_QTY_THRESHOLD`        | `10`  | Stock discrepancy auto-correct threshold    |
| `HFT_STARTUP_RECON_FUTURES_QTY_THRESHOLD`| `2`   | Futures discrepancy auto-correct threshold  |
| `HFT_CHECKPOINT_ENABLED`                 | `1`   | Enable periodic position checkpoint writing |
```

- [ ] **Step 3: Update spec status**

Change line 4 of the spec to: `**Status**: Implemented`

- [ ] **Step 4: Commit**

```bash
git add .env.example CLAUDE.md docs/superpowers/specs/2026-03-25-cross-day-position-continuity-design.md
git commit -m "docs: mark position continuity spec as implemented, update env var docs"
```
