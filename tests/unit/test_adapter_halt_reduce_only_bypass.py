"""Regression test for Bug 24 (2026-04-17 R47 OrderAdapter HALT reduce-only gap).

Symptom (downstream of Bug 21/22): even with Gateway reduce-only bypass fixed,
OrderAdapter re-checks StormGuard HALT at dispatch time (TOCTOU closure, M1 gap).
The adapter's ``_halt_exempt`` clause at ``adapter.py:1055`` only allows
CANCEL / FORCE_FLAT / halt_exempt strategies — a covering NEW BUY against a short
position still hits ``_is_halt and not _halt_exempt`` and gets dropped into DLQ.

Scenario (race that triggers it):
  1. Gateway passes cover BUY (HALT_REDUCE_ONLY, Bug 22 fix).
  2. RiskEngine passes (validators + StormGuard reduce bypass, Bug 21 fix).
  3. Between risk-approve and adapter-dispatch, StormGuard transitions to HALT
     (feed gap or margin breach).
  4. Adapter sees live ``_storm_guard.state == HALT`` but intent is NEW BUY.
  5. Adapter blocks → DLQ → position stays stuck → deadlock returns.

Fix: Add ``_intent_reduces_position(intent)`` helper delegating to StormGuard's
existing predicate and include it in ``_halt_exempt`` so reducing orders flow
through at the adapter layer as well.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from hft_platform.contracts.execution import Side as ExecSide  # noqa: F401 — side-effect import
from hft_platform.contracts.strategy import (
    IntentType,
    OrderCommand,
    OrderIntent,
    Side,
    StormGuardState,
)
from hft_platform.order.adapter import OrderAdapter


def _make_intent(
    intent_type=IntentType.NEW,
    side=Side.BUY,
    qty=1,
    symbol="TMFE6",
    strategy_id="R47_MAKER_TMF",
    intent_id=1,
):
    return OrderIntent(
        intent_id=intent_id,
        strategy_id=strategy_id,
        symbol=symbol,
        intent_type=intent_type,
        side=side,
        price=377_000_000,
        qty=qty,
    )


def _make_cmd(intent, sg_state=StormGuardState.NORMAL, cmd_id=1):
    return OrderCommand(
        intent=intent,
        cmd_id=cmd_id,
        deadline_ns=0,
        storm_guard_state=sg_state,
        created_ns=0,
    )


def _make_adapter_in_halt(*, short_position: int = -1) -> OrderAdapter:
    """Build a bare OrderAdapter with StormGuard in HALT and position wired."""
    adapter = OrderAdapter.__new__(OrderAdapter)
    adapter._storm_guard = MagicMock()
    adapter._storm_guard.state = StormGuardState.HALT
    adapter._storm_guard._halt_exempt_strategies = frozenset()
    adapter._storm_guard.is_halt_exempt = lambda sid: False

    def _pos(symbol: str, sid: str) -> int:
        return short_position if (symbol, sid) == ("TMFE6", "R47_MAKER_TMF") else 0

    def _reduces(intent):
        cur = _pos(intent.symbol, intent.strategy_id)
        if cur == 0:
            return False
        signed = intent.qty if intent.side == Side.BUY else -intent.qty
        return abs(cur + signed) < abs(cur)

    adapter._storm_guard._intent_reduces_position = _reduces

    adapter._add_to_dlq = AsyncMock()
    adapter.metrics = MagicMock()
    return adapter


class TestAdapterHaltReduceOnlyBypass:
    """Bug 24: adapter must allow reducing cover orders through HALT."""

    def test_helper_returns_true_for_cover_intent(self):
        """``_intent_reduces_position`` is True for NEW BUY covering a short."""
        adapter = _make_adapter_in_halt(short_position=-1)
        cover = _make_intent(intent_type=IntentType.NEW, side=Side.BUY, qty=1)
        assert adapter._intent_reduces_position(cover) is True

    def test_helper_returns_false_for_opening_intent(self):
        """Opening SELL from flat does NOT reduce position."""
        adapter = _make_adapter_in_halt(short_position=0)
        opener = _make_intent(intent_type=IntentType.NEW, side=Side.SELL, qty=1)
        assert adapter._intent_reduces_position(opener) is False

    def test_helper_returns_false_when_storm_guard_missing(self):
        """Defensive: adapter must never raise if StormGuard not wired."""
        adapter = OrderAdapter.__new__(OrderAdapter)
        adapter._storm_guard = None
        cover = _make_intent(intent_type=IntentType.NEW, side=Side.BUY, qty=1)
        assert adapter._intent_reduces_position(cover) is False

    @pytest.mark.asyncio
    async def test_cover_order_bypasses_halt_gate(self):
        """End-to-end: NEW BUY covering short passes adapter HALT gate.

        Before fix: ``_halt_exempt`` is False → adapter._add_to_dlq is called.
        After fix: ``_halt_exempt`` includes ``_intent_reduces_position`` →
        the code past line 1067 runs (rate limits, dedup, dispatch).
        """
        adapter = _make_adapter_in_halt(short_position=-1)

        cover_intent = _make_intent(intent_type=IntentType.NEW, side=Side.BUY, qty=1)
        cmd = _make_cmd(cover_intent, sg_state=StormGuardState.HALT)

        # Short-circuit the rest of execute() — we only need to test the HALT branch.
        # The simplest black-box is to check _add_to_dlq was NOT called with HALT reason.
        # We stub downstream paths so execute() returns cleanly.
        adapter._dedup_store = None
        adapter.per_symbol_rate_limiter = MagicMock()
        adapter.per_symbol_rate_limiter.check = MagicMock(
            return_value=MagicMock(name="OK")
        )
        # Force a subsequent bail-out after HALT check, so we don't drag the full pipeline in.
        # We patch _platform_degrade_allows to return False to stop execution cleanly.
        adapter._platform_degrade_allows = MagicMock(return_value=False)
        adapter.strategy_cb_mgr = MagicMock()
        adapter.strategy_cb_mgr.is_open = MagicMock(return_value=False)
        adapter.circuit_breaker = MagicMock()
        adapter.circuit_breaker.is_open = MagicMock(return_value=False)
        adapter.check_rate_limit = MagicMock(return_value=True)
        adapter._emit_trace = MagicMock()
        adapter._is_strategy_halt_exempt = lambda sid: False

        await adapter.execute(cmd)

        # Assert: none of the _add_to_dlq calls used the "StormGuard HALT" reason.
        halt_rejects = [
            c for c in adapter._add_to_dlq.await_args_list
            if len(c.args) >= 3 and c.args[2] == "StormGuard HALT"
        ]
        assert not halt_rejects, (
            f"Adapter still blocks cover order under HALT (Bug 24 not fixed): "
            f"{adapter._add_to_dlq.await_args_list}"
        )

    @pytest.mark.asyncio
    async def test_opening_order_still_blocked_under_halt(self):
        """Negative control: opening SELL from flat STILL blocks under HALT."""
        adapter = _make_adapter_in_halt(short_position=0)

        opener = _make_intent(intent_type=IntentType.NEW, side=Side.SELL, qty=1)
        cmd = _make_cmd(opener, sg_state=StormGuardState.HALT)
        adapter._is_strategy_halt_exempt = lambda sid: False

        await adapter.execute(cmd)

        # Opening orders should still hit DLQ with "StormGuard HALT"
        halt_rejects = [
            c for c in adapter._add_to_dlq.await_args_list
            if len(c.args) >= 3 and c.args[2] == "StormGuard HALT"
        ]
        assert halt_rejects, (
            "Opening order must still be blocked under HALT — fix over-reached"
        )
