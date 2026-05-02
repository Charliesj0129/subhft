# Multi-Strategy Position Isolation & Manual Order Coexistence

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the `strategy_id="*"` wildcard with an explicit `MANUAL` strategy constant, fix recovery position leaking into per-strategy queries, and add a `/pos` Telegram command for per-strategy position visibility.

**Architecture:** Three layers of change: (1) A `MANUAL_STRATEGY_ID` constant replaces all `"*"` usage — orphaned/manual positions get a well-defined identity instead of a magic string. (2) `net_qty_for_symbol()` is fixed to respect `strategy_id` filtering for recovery positions, stopping cross-strategy contamination. (3) A `/pos` bot command gives the operator per-strategy position breakdown on demand.

**Tech Stack:** Python 3.12, pytest, structlog, python-telegram-bot, PositionStore (Python + Rust tracker)

---

## File Structure

| Action | File | Responsibility |
|--------|------|----------------|
| Create | `src/hft_platform/contracts/constants.py` | Platform-wide constants (`MANUAL_STRATEGY_ID`) |
| Modify | `src/hft_platform/execution/positions.py:226-246` | Fix `net_qty_for_symbol` recovery filter |
| Modify | `src/hft_platform/execution/reconciliation.py:617-628` | Use `MANUAL_STRATEGY_ID` in auto-correct |
| Modify | `src/hft_platform/execution/startup_recon.py:467-494` | Use `MANUAL_STRATEGY_ID` in broker-only recovery |
| Modify | `src/hft_platform/bot/handlers.py` | Add `cmd_pos` handler |
| Modify | `src/hft_platform/bot/app.py:109-141` | Register `/pos` command, add position_store ref |
| Create | `tests/unit/test_manual_strategy_constant.py` | Tests for constant usage and recovery filter fix |
| Modify | `tests/unit/test_reconciliation_auto_correct.py` | Update `"*"` → `MANUAL_STRATEGY_ID` in assertions |
| Modify | `tests/unit/test_position_recovery.py` | Update `"*"` → `MANUAL_STRATEGY_ID` in assertions |

---

### Task 1: Define MANUAL_STRATEGY_ID constant

**Files:**
- Create: `src/hft_platform/contracts/constants.py`
- Test: `tests/unit/test_manual_strategy_constant.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/unit/test_manual_strategy_constant.py
"""Tests for MANUAL_STRATEGY_ID constant and its usage contract."""


def test_manual_strategy_id_is_string():
    from hft_platform.contracts.constants import MANUAL_STRATEGY_ID

    assert isinstance(MANUAL_STRATEGY_ID, str)
    assert len(MANUAL_STRATEGY_ID) > 0


def test_manual_strategy_id_is_not_wildcard():
    """MANUAL must NOT be '*' — wildcard matching is the bug we're fixing."""
    from hft_platform.contracts.constants import MANUAL_STRATEGY_ID

    assert MANUAL_STRATEGY_ID != "*"


def test_manual_strategy_id_is_uppercase():
    """Convention: special strategy IDs are uppercase for visibility in logs."""
    from hft_platform.contracts.constants import MANUAL_STRATEGY_ID

    assert MANUAL_STRATEGY_ID == MANUAL_STRATEGY_ID.upper()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/unit/test_manual_strategy_constant.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'hft_platform.contracts.constants'`

- [ ] **Step 3: Write minimal implementation**

```python
# src/hft_platform/contracts/constants.py
"""Platform-wide constants for strategy identification and position attribution."""

# Strategy ID assigned to positions that originate from manual broker operations,
# reconciliation auto-corrections, or broker-only recovery (no checkpoint).
# Replaces the former "*" wildcard which caused recovery positions to leak
# into per-strategy queries via net_qty_for_symbol().
MANUAL_STRATEGY_ID: str = "MANUAL"
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/unit/test_manual_strategy_constant.py -v`
Expected: 3 passed

- [ ] **Step 5: Commit**

```bash
git add src/hft_platform/contracts/constants.py tests/unit/test_manual_strategy_constant.py
git commit -m "feat(contracts): add MANUAL_STRATEGY_ID constant for position attribution"
```

---

### Task 2: Fix net_qty_for_symbol recovery position filter

**Files:**
- Modify: `src/hft_platform/execution/positions.py:226-246`
- Test: `tests/unit/test_manual_strategy_constant.py` (append)

- [ ] **Step 1: Write the failing test**

Append to `tests/unit/test_manual_strategy_constant.py`:

```python
from unittest.mock import MagicMock, patch


def _make_position_store():
    """Create a PositionStore with metrics and metadata mocked."""
    with patch("hft_platform.execution.positions.MetricsRegistry.get", return_value=MagicMock()):
        from hft_platform.execution.positions import Position, PositionStore

        store = PositionStore()
        # Seed two strategy positions for same symbol
        store.positions["acc:alpha:TXFD6"] = Position(
            account_id="acc",
            strategy_id="alpha",
            symbol="TXFD6",
            net_qty=2,
        )
        store.positions["acc:beta:TXFD6"] = Position(
            account_id="acc",
            strategy_id="beta",
            symbol="TXFD6",
            net_qty=-1,
        )
        return store


def test_net_qty_for_symbol_without_filter_includes_recovery():
    """Without strategy_id filter, recovery positions ARE included."""
    store = _make_position_store()
    # Add a MANUAL recovery position
    store._recovery_positions["acc:MANUAL:TXFD6"] = {
        "symbol": "TXFD6",
        "net_qty": 1,
        "strategy_id": "MANUAL",
    }
    # No filter: alpha(+2) + beta(-1) + recovery(+1) = +2
    assert store.net_qty_for_symbol("TXFD6") == 2


def test_net_qty_for_symbol_with_filter_excludes_other_strategy_recovery():
    """With strategy_id='alpha', MANUAL recovery must NOT leak in."""
    store = _make_position_store()
    store._recovery_positions["acc:MANUAL:TXFD6"] = {
        "symbol": "TXFD6",
        "net_qty": 1,
        "strategy_id": "MANUAL",
    }
    # Filter alpha: only alpha's +2
    assert store.net_qty_for_symbol("TXFD6", strategy_id="alpha") == 2


def test_net_qty_for_symbol_manual_filter_returns_only_manual():
    """Querying strategy_id='MANUAL' returns only MANUAL recovery positions."""
    store = _make_position_store()
    store._recovery_positions["acc:MANUAL:TXFD6"] = {
        "symbol": "TXFD6",
        "net_qty": 1,
        "strategy_id": "MANUAL",
    }
    # Filter MANUAL: only the recovery +1
    assert store.net_qty_for_symbol("TXFD6", strategy_id="MANUAL") == 1


def test_net_qty_for_symbol_legacy_no_strategy_recovery_included_when_no_filter():
    """Legacy recovery without strategy_id still included when no filter."""
    store = _make_position_store()
    store._recovery_positions["acc:TXFD6"] = {
        "symbol": "TXFD6",
        "net_qty": 3,
    }
    # No filter: alpha(+2) + beta(-1) + legacy(+3) = +4
    assert store.net_qty_for_symbol("TXFD6") == 4


def test_net_qty_for_symbol_legacy_no_strategy_recovery_excluded_with_filter():
    """Legacy recovery without strategy_id excluded when filtering specific strategy."""
    store = _make_position_store()
    store._recovery_positions["acc:TXFD6"] = {
        "symbol": "TXFD6",
        "net_qty": 3,
        # No strategy_id key
    }
    # Filter alpha: only alpha's +2 (legacy recovery excluded)
    assert store.net_qty_for_symbol("TXFD6", strategy_id="alpha") == 2
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/unit/test_manual_strategy_constant.py::test_net_qty_for_symbol_with_filter_excludes_other_strategy_recovery -v`
Expected: FAIL — currently returns 3 (2 + 1 recovery leak) instead of 2

- [ ] **Step 3: Modify net_qty_for_symbol**

In `src/hft_platform/execution/positions.py`, replace lines 226-246:

Old code:
```python
    def net_qty_for_symbol(self, symbol: str, strategy_id: str | None = None) -> int:
        """Return aggregate net_qty for *symbol*, including pending recovery positions.

        When *strategy_id* is provided, only that strategy's entries in
        ``self.positions`` are summed.  Recovery entries (which lack a
        strategy_id) are always included because they represent a broker-
        confirmed position that has not yet received its first fill.
        """
        total = 0
        for _key, pos in self.positions.items():
            if getattr(pos, "symbol", None) != symbol:
                continue
            if strategy_id is not None and getattr(pos, "strategy_id", None) != strategy_id:
                continue
            total += int(getattr(pos, "net_qty", 0) or 0)
        # Include pending recovery (keyed by account:symbol, no strategy_id)
        for rkey, rdata in self._recovery_positions.items():
            rsym = rdata.get("symbol", rkey.rsplit(":", 1)[-1]) if isinstance(rdata, dict) else ""
            if rsym == symbol:
                total += int(rdata.get("net_qty", 0))
        return total
```

New code:
```python
    def net_qty_for_symbol(self, symbol: str, strategy_id: str | None = None) -> int:
        """Return aggregate net_qty for *symbol*, optionally filtered by strategy.

        When *strategy_id* is ``None``, all strategies AND recovery positions
        are included (aggregate view).  When *strategy_id* is provided, only
        positions belonging to that strategy are summed — recovery positions
        are included only if their ``strategy_id`` matches the filter (or if
        they have no ``strategy_id`` and no filter is applied).
        """
        total = 0
        for _key, pos in self.positions.items():
            if getattr(pos, "symbol", None) != symbol:
                continue
            if strategy_id is not None and getattr(pos, "strategy_id", None) != strategy_id:
                continue
            total += int(getattr(pos, "net_qty", 0) or 0)
        for rkey, rdata in self._recovery_positions.items():
            if not isinstance(rdata, dict):
                continue
            rsym = rdata.get("symbol", rkey.rsplit(":", 1)[-1])
            if rsym != symbol:
                continue
            rstrat = rdata.get("strategy_id", "")
            if strategy_id is not None and rstrat and rstrat != strategy_id:
                continue
            if strategy_id is not None and not rstrat:
                # Legacy recovery with no strategy_id: exclude from filtered queries
                continue
            total += int(rdata.get("net_qty", 0))
        return total
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/unit/test_manual_strategy_constant.py -v`
Expected: All 9 tests pass

- [ ] **Step 5: Run existing position tests for regression**

Run: `uv run pytest tests/unit/test_positions.py tests/unit/test_position_store.py -v --timeout=30`
Expected: All existing tests still pass

- [ ] **Step 6: Commit**

```bash
git add src/hft_platform/execution/positions.py tests/unit/test_manual_strategy_constant.py
git commit -m "fix(positions): respect strategy_id filter for recovery positions in net_qty_for_symbol"
```

---

### Task 3: Replace `"*"` with MANUAL_STRATEGY_ID in reconciliation auto-correct

**Files:**
- Modify: `src/hft_platform/execution/reconciliation.py:617-628`
- Modify: `tests/unit/test_reconciliation_auto_correct.py`

- [ ] **Step 1: Write the failing test**

Add a new test to `tests/unit/test_reconciliation_auto_correct.py`:

```python
def test_auto_correct_uses_manual_strategy_id(service):
    """Auto-corrected positions must use MANUAL_STRATEGY_ID, not '*'."""
    from hft_platform.contracts.constants import MANUAL_STRATEGY_ID

    svc = service
    # Drive enough sync cycles to trigger auto-correct (streak >= 3)
    import asyncio

    for _ in range(4):
        asyncio.get_event_loop().run_until_complete(svc._sync_once())

    # Check that load_recovery was called with MANUAL_STRATEGY_ID
    recovery = svc.store._recovery_positions
    for _key, data in recovery.items():
        assert data["strategy_id"] == MANUAL_STRATEGY_ID, (
            f"Expected '{MANUAL_STRATEGY_ID}', got '{data.get('strategy_id')}'"
        )
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/unit/test_reconciliation_auto_correct.py::test_auto_correct_uses_manual_strategy_id -v`
Expected: FAIL — `strategy_id` is `"*"` not `"MANUAL"`

- [ ] **Step 3: Modify reconciliation auto-correct**

In `src/hft_platform/execution/reconciliation.py`, add the import near the top (after existing imports):

```python
from hft_platform.contracts.constants import MANUAL_STRATEGY_ID
```

Then replace line 627 (`strategy_id="*"`) with:

```python
                    strategy_id=MANUAL_STRATEGY_ID,
```

And update the comment on line 619:

Old: `# with strategy_id="*" (unknown ownership — matches startup_recon pattern).`
New: `# with MANUAL_STRATEGY_ID (explicit manual/orphan ownership).`

- [ ] **Step 4: Update existing test assertions**

In `tests/unit/test_reconciliation_auto_correct.py`, update three occurrences of `strategy_id="*"` in assertion `assert_called` blocks:

Find each `strategy_id="*"` in the test file and replace with:

```python
from hft_platform.contracts.constants import MANUAL_STRATEGY_ID
# ... in each assert_called_once_with / assert_any_call:
strategy_id=MANUAL_STRATEGY_ID,
```

There are 3 occurrences at approximately lines 238, 282, 348. Add the import at the top of the file:

```python
from hft_platform.contracts.constants import MANUAL_STRATEGY_ID
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `uv run pytest tests/unit/test_reconciliation_auto_correct.py -v`
Expected: All tests pass

- [ ] **Step 6: Commit**

```bash
git add src/hft_platform/execution/reconciliation.py tests/unit/test_reconciliation_auto_correct.py
git commit -m "fix(reconciliation): use MANUAL_STRATEGY_ID instead of wildcard '*' in auto-correct"
```

---

### Task 4: Replace `"*"` with MANUAL_STRATEGY_ID in startup_recon broker-only recovery

**Files:**
- Modify: `src/hft_platform/execution/startup_recon.py:467-494`
- Modify: `tests/unit/test_startup_recon.py`
- Modify: `tests/unit/test_position_recovery.py`

- [ ] **Step 1: Write the failing test**

Add to `tests/unit/test_startup_recon.py`:

```python
def test_broker_only_recovery_uses_manual_strategy_id():
    """Broker-only recovery must use MANUAL_STRATEGY_ID, not '*'."""
    from hft_platform.contracts.constants import MANUAL_STRATEGY_ID
    from hft_platform.execution.startup_recon import StartupPositionVerifier

    broker_positions = [SimpleNamespace(code="TXFD6", quantity=2, direction="Long")]
    store = _make_store()
    verifier = StartupPositionVerifier(
        client=_make_client(broker_positions),
        position_store=store,
    )
    result = asyncio.run(verifier.recover(trading_date="20260415", account_id="test"))
    assert result.source == "broker_only"

    recovery = store._recovery_positions
    for _key, data in recovery.items():
        assert data["strategy_id"] == MANUAL_STRATEGY_ID, (
            f"Expected '{MANUAL_STRATEGY_ID}', got '{data.get('strategy_id')}'"
        )
        # Key should contain MANUAL, not *
        assert MANUAL_STRATEGY_ID in _key
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/unit/test_startup_recon.py::test_broker_only_recovery_uses_manual_strategy_id -v`
Expected: FAIL — `strategy_id` is `"*"` not `"MANUAL"`

- [ ] **Step 3: Modify startup_recon**

In `src/hft_platform/execution/startup_recon.py`, add the import near the top:

```python
from hft_platform.contracts.constants import MANUAL_STRATEGY_ID
```

In `_recover_broker_only` (around line 481), replace:

Old:
```python
                key = f"{account_id}:*:{symbol}"
                merged[key] = {
                    "symbol": symbol,
                    "net_qty": qty,
                    "avg_price_scaled": -1,
                    "realized_pnl_scaled": 0,
                    "fees_scaled": 0,
                    "account_id": account_id,
                    "strategy_id": "*",
                }
```

New:
```python
                key = f"{account_id}:{MANUAL_STRATEGY_ID}:{symbol}"
                merged[key] = {
                    "symbol": symbol,
                    "net_qty": qty,
                    "avg_price_scaled": -1,
                    "realized_pnl_scaled": 0,
                    "fees_scaled": 0,
                    "account_id": account_id,
                    "strategy_id": MANUAL_STRATEGY_ID,
                }
```

Also update the docstring on line 471:
Old: `Uses ``strategy_id="*"`` (wildcard) to explicitly mark unknown ownership.`
New: `Uses ``MANUAL_STRATEGY_ID`` to explicitly mark manual/orphan ownership.`

And line 472:
Old: `StrategyRunner dispatches ``"*"`` positions to matching strategies on first`
New: `StrategyRunner dispatches ``MANUAL`` positions to matching strategies on first`

And line 473:
Old: `fill via the wildcard lookup in ``positions_by_strategy.get("*")``.`
New: `fill via the lookup in ``positions_by_strategy.get(MANUAL_STRATEGY_ID)``.`

- [ ] **Step 4: Update existing test assertions in test_startup_recon.py**

In `tests/unit/test_startup_recon.py`, add the import:

```python
from hft_platform.contracts.constants import MANUAL_STRATEGY_ID
```

Update the 4 occurrences of `"*"` in assertions (approximately lines 222, 230, 298, 300, 340, 344):

- Line 222: `# strategy_id="*" marks wildcard` → `# strategy_id=MANUAL_STRATEGY_ID marks manual`
- Line 230: `strategy_id="*"` → `strategy_id=MANUAL_STRATEGY_ID`
- Line 298: `assert data["strategy_id"] == "*"` → `assert data["strategy_id"] == MANUAL_STRATEGY_ID`
- Line 300: `assert "*" in key` → `assert MANUAL_STRATEGY_ID in key`
- Line 340: `# MXFJ6 should have strategy_id="*"` → `# MXFJ6 should have MANUAL_STRATEGY_ID`
- Line 344: `assert mxfj_data["strategy_id"] == "*"` → `assert mxfj_data["strategy_id"] == MANUAL_STRATEGY_ID`

- [ ] **Step 5: Update test_position_recovery.py**

In `tests/unit/test_position_recovery.py`, add the import:

```python
from hft_platform.contracts.constants import MANUAL_STRATEGY_ID
```

Update the assertion at line 230:

Old: `strategy_id="*",`
New: `strategy_id=MANUAL_STRATEGY_ID,`

- [ ] **Step 6: Check for any other `"*"` references in router**

In `src/hft_platform/execution/router.py:621`, there's a check: `if strategy_id and strategy_id != "*":`. This should also be updated:

```python
from hft_platform.contracts.constants import MANUAL_STRATEGY_ID
```

Replace line 621:
Old: `if strategy_id and strategy_id != "*":`
New: `if strategy_id and strategy_id != MANUAL_STRATEGY_ID:`

- [ ] **Step 7: Run all affected tests**

Run: `uv run pytest tests/unit/test_startup_recon.py tests/unit/test_position_recovery.py tests/unit/test_reconciliation_auto_correct.py tests/unit/test_manual_strategy_constant.py -v`
Expected: All pass

- [ ] **Step 8: Commit**

```bash
git add src/hft_platform/execution/startup_recon.py src/hft_platform/execution/router.py tests/unit/test_startup_recon.py tests/unit/test_position_recovery.py
git commit -m "fix(startup_recon): use MANUAL_STRATEGY_ID instead of wildcard '*' in broker-only recovery"
```

---

### Task 5: Add /pos Telegram bot command

**Files:**
- Modify: `src/hft_platform/bot/handlers.py`
- Modify: `src/hft_platform/bot/app.py:109-141`
- Test: `tests/unit/test_bot_pos_command.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/unit/test_bot_pos_command.py
"""Tests for /pos Telegram bot command."""
from __future__ import annotations

from dataclasses import dataclass
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


@dataclass(slots=True)
class FakePosition:
    account_id: str
    strategy_id: str
    symbol: str
    net_qty: int
    avg_price_scaled: int = 0
    realized_pnl_scaled: int = 0
    fees_scaled: int = 0
    last_update_ts: int = 0


def _make_fake_store(positions: dict):
    store = MagicMock()
    store.positions = positions
    store._recovery_positions = {}
    return store


@pytest.mark.asyncio
async def test_pos_command_shows_all_strategies():
    """'/pos' with no args shows all strategies grouped."""
    from hft_platform.bot.handlers import cmd_pos

    positions = {
        "acc:r47_maker:TXFD6": FakePosition("acc", "r47_maker", "TXFD6", 2, 200000000),
        "acc:r47_maker:TMFD6": FakePosition("acc", "r47_maker", "TMFD6", 1, 190000000),
        "acc:MANUAL:TXFD6": FakePosition("acc", "MANUAL", "TXFD6", 1, 205000000),
    }

    with patch("hft_platform.bot.handlers._get_position_store", return_value=_make_fake_store(positions)):
        update = MagicMock()
        update.message.reply_text = AsyncMock()
        context = MagicMock()
        context.args = []

        await cmd_pos(update, context)

        reply = update.message.reply_text.call_args[0][0]
        assert "r47_maker" in reply
        assert "MANUAL" in reply
        assert "TXFD6" in reply
        assert "TMFD6" in reply


@pytest.mark.asyncio
async def test_pos_command_filters_by_strategy():
    """'/pos r47_maker' shows only that strategy's positions."""
    from hft_platform.bot.handlers import cmd_pos

    positions = {
        "acc:r47_maker:TXFD6": FakePosition("acc", "r47_maker", "TXFD6", 2),
        "acc:MANUAL:TXFD6": FakePosition("acc", "MANUAL", "TXFD6", 1),
    }

    with patch("hft_platform.bot.handlers._get_position_store", return_value=_make_fake_store(positions)):
        update = MagicMock()
        update.message.reply_text = AsyncMock()
        context = MagicMock()
        context.args = ["r47_maker"]

        await cmd_pos(update, context)

        reply = update.message.reply_text.call_args[0][0]
        assert "r47_maker" in reply
        assert "MANUAL" not in reply


@pytest.mark.asyncio
async def test_pos_command_empty_positions():
    """'/pos' with no open positions shows empty message."""
    from hft_platform.bot.handlers import cmd_pos

    with patch("hft_platform.bot.handlers._get_position_store", return_value=_make_fake_store({})):
        update = MagicMock()
        update.message.reply_text = AsyncMock()
        context = MagicMock()
        context.args = []

        await cmd_pos(update, context)

        reply = update.message.reply_text.call_args[0][0]
        assert "無持倉" in reply or "empty" in reply.lower() or "0" in reply
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/unit/test_bot_pos_command.py -v`
Expected: FAIL with `ImportError: cannot import name 'cmd_pos' from 'hft_platform.bot.handlers'`

- [ ] **Step 3: Add position store accessor to bot handlers**

In `src/hft_platform/bot/handlers.py`, add near the top after existing imports:

```python
from typing import Any

# Position store reference — set by bootstrap or bot app setup
_position_store_ref: Any = None


def set_position_store(store: Any) -> None:
    """Set the PositionStore reference for bot commands."""
    global _position_store_ref  # noqa: PLW0603
    _position_store_ref = store


def _get_position_store() -> Any:
    """Return the current PositionStore reference (or None)."""
    return _position_store_ref
```

- [ ] **Step 4: Implement cmd_pos handler**

In `src/hft_platform/bot/handlers.py`, add before the final line:

```python
@owner_only
async def cmd_pos(update: Any, context: Any) -> None:
    """Handle /pos [strategy_id] command — show per-strategy position breakdown."""
    store = _get_position_store()
    if store is None:
        await update.message.reply_text("Position store 未連接")
        return

    args = context.args or []
    filter_strategy = args[0] if args else None

    positions = getattr(store, "positions", {})
    if not positions:
        await update.message.reply_text("無持倉")
        return

    # Group by strategy_id
    by_strategy: dict[str, list[tuple[str, int]]] = {}
    for _key, pos in positions.items():
        if pos.net_qty == 0:
            continue
        strat = getattr(pos, "strategy_id", "?")
        if filter_strategy and strat != filter_strategy:
            continue
        by_strategy.setdefault(strat, []).append((pos.symbol, pos.net_qty))

    # Include recovery positions
    recovery = getattr(store, "_recovery_positions", {})
    for _rkey, rdata in recovery.items():
        if not isinstance(rdata, dict):
            continue
        qty = int(rdata.get("net_qty", 0))
        if qty == 0:
            continue
        strat = rdata.get("strategy_id", "?")
        if filter_strategy and strat != filter_strategy:
            continue
        by_strategy.setdefault(strat, []).append((rdata.get("symbol", "?"), qty))

    if not by_strategy:
        msg = f"策略 {filter_strategy} 無持倉" if filter_strategy else "無持倉"
        await update.message.reply_text(msg)
        return

    lines: list[str] = ["倉位明細\n"]
    total_by_symbol: dict[str, int] = {}
    for strat in sorted(by_strategy):
        lines.append(f"[{strat}]")
        for symbol, qty in sorted(by_strategy[strat]):
            sign = "+" if qty > 0 else ""
            lines.append(f"  {symbol}: {sign}{qty}")
            total_by_symbol[symbol] = total_by_symbol.get(symbol, 0) + qty
        lines.append("")

    # Aggregate footer
    if len(by_strategy) > 1:
        lines.append("[合計]")
        for symbol in sorted(total_by_symbol):
            qty = total_by_symbol[symbol]
            sign = "+" if qty > 0 else ""
            lines.append(f"  {symbol}: {sign}{qty}")

    await update.message.reply_text("\n".join(lines))
```

- [ ] **Step 5: Register /pos command in app.py**

In `src/hft_platform/bot/app.py`, add `cmd_pos` to the import block (line 113-121):

```python
    from hft_platform.bot.handlers import (
        cmd_ask,
        cmd_flow,
        cmd_levels,
        cmd_pos,
        cmd_report,
        cmd_report_rule,
        cmd_start,
        cmd_status,
    )
```

After line 136 (`app.add_handler(CommandHandler("status", cmd_status))`), add:

```python
    app.add_handler(CommandHandler("pos", cmd_pos))
```

- [ ] **Step 6: Run tests to verify they pass**

Run: `uv run pytest tests/unit/test_bot_pos_command.py -v`
Expected: 3 passed

- [ ] **Step 7: Commit**

```bash
git add src/hft_platform/bot/handlers.py src/hft_platform/bot/app.py tests/unit/test_bot_pos_command.py
git commit -m "feat(bot): add /pos command for per-strategy position breakdown"
```

---

### Task 6: Full regression test

**Files:**
- No new files

- [ ] **Step 1: Run lint**

Run: `uv run ruff check src/hft_platform/contracts/constants.py src/hft_platform/execution/positions.py src/hft_platform/execution/reconciliation.py src/hft_platform/execution/startup_recon.py src/hft_platform/execution/router.py src/hft_platform/bot/handlers.py src/hft_platform/bot/app.py`
Expected: No errors

- [ ] **Step 2: Run type check on modified files**

Run: `uv run mypy src/hft_platform/contracts/constants.py src/hft_platform/execution/positions.py src/hft_platform/execution/reconciliation.py src/hft_platform/execution/startup_recon.py`
Expected: No new errors

- [ ] **Step 3: Run full unit test suite for affected modules**

Run: `uv run pytest tests/unit/test_manual_strategy_constant.py tests/unit/test_bot_pos_command.py tests/unit/test_reconciliation_auto_correct.py tests/unit/test_startup_recon.py tests/unit/test_position_recovery.py tests/unit/test_positions.py -v --timeout=60`
Expected: All pass

- [ ] **Step 4: Run broader regression**

Run: `uv run pytest tests/unit/ -x --timeout=120 -q`
Expected: No regressions

- [ ] **Step 5: Verify grep for remaining '*' wildcards**

Run: `grep -rn 'strategy_id.*=.*"\*"' src/hft_platform/`
Expected: No results (all `"*"` replaced with `MANUAL_STRATEGY_ID`)

---

## Summary of Behavioral Changes

| Scenario | Before (with `"*"`) | After (with `MANUAL`) |
|----------|---------------------|-----------------------|
| `net_qty_for_symbol("TXFD6", "alpha")` with orphan recovery | Recovery QTY **leaks in** | Recovery QTY **excluded** |
| `net_qty_for_symbol("TXFD6")` without filter | Includes all + recovery | Same (unchanged) |
| `net_qty_for_symbol("TXFD6", "MANUAL")` | N/A (nobody queries `"*"`) | Returns only manual positions |
| Reconciliation auto-correct | `strategy_id="*"` | `strategy_id="MANUAL"` |
| Broker-only recovery | `strategy_id="*"` key | `strategy_id="MANUAL"` key |
| Checkpoint persistence | `"*"` in key | `"MANUAL"` in key |
| Telegram /pos command | N/A | Per-strategy breakdown |
