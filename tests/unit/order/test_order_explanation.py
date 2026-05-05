"""Unit tests for ``OrderExplanationAssembler`` (loop_v1 step L8).

Covers the four lifecycle shapes the assembler must handle:
  * 1-intent → 1-fill → terminal "filled"
  * 1-intent → cancel only (no fill) → terminal "canceled"
  * 1-intent → 3 partial fills + 1 cancel → 1 explanation row carrying all fills
  * 1-intent → TTL exceeded → "incomplete" via sweep

Plus the guards:
  * Empty trace_id => no registration (phantom-DLQ replay protection)
  * Re-registration during reconnect preserves accumulated fills/cancels
  * Sink exception is swallowed (assembler never crashes the order path)
  * Capacity eviction emits the oldest entry as "incomplete"
"""

from __future__ import annotations

from typing import Any

import pytest

from hft_platform.order.explanation import (
    OrderExplanation,
    OrderExplanationAssembler,
)


def _make_assembler(
    *,
    sink: Any = None,
    ttl_s: float = 300.0,
    max_in_flight: int = 10000,
) -> OrderExplanationAssembler:
    return OrderExplanationAssembler(
        loop_id="r47_tmf_v1",
        strategy_version="1823be17",
        config_hash="deadbeefcafe0001",
        git_sha="1823be17",
        data_session_id="sim-2026-05-05",
        sink=sink,
        ttl_s=ttl_s,
        max_in_flight=max_in_flight,
    )


class TestSingleFillTerminal:
    def test_one_intent_one_fill_emits_one_explanation(self) -> None:
        emitted: list[OrderExplanation] = []
        asm = _make_assembler(sink=emitted.append)

        asm.on_intent(
            client_order_id="R47:42",
            trace_id="trace-abc",
            strategy_id="R47_MAKER_TMF",
            symbol="TMFR1",
            intent_payload={"reason": "spread>=threshold", "side": "BUY", "qty": 1},
        )
        asm.on_command(
            client_order_id="R47:42",
            command_payload={"price_scaled": 171960000, "qty": 1, "side": "BUY"},
        )
        asm.on_fill(
            client_order_id="R47:42",
            fill_payload={"fill_id": "dl-1", "qty": 1, "price_scaled": 171960000},
        )
        result = asm.on_terminal(
            client_order_id="R47:42",
            lifecycle_status="filled",
            ts_emit_ns=1_700_000_000_000_000_000,
        )

        assert result is not None
        assert isinstance(result, OrderExplanation)
        assert len(emitted) == 1
        e = emitted[0]
        assert e.trace_id == "trace-abc"
        assert e.client_order_id == "R47:42"
        assert e.loop_id == "r47_tmf_v1"
        assert e.strategy_id == "R47_MAKER_TMF"
        assert e.symbol == "TMFR1"
        assert e.lifecycle_status == "filled"
        assert e.git_sha == "1823be17"
        assert e.config_hash == "deadbeefcafe0001"
        assert len(e.fills) == 1
        assert e.fills[0]["fill_id"] == "dl-1"
        assert e.cancels == []
        assert asm.in_flight == 0


class TestCancelOnly:
    def test_intent_then_cancel_no_fill(self) -> None:
        emitted: list[OrderExplanation] = []
        asm = _make_assembler(sink=emitted.append)

        asm.on_intent(
            client_order_id="R47:43",
            trace_id="trace-cancel",
            strategy_id="R47_MAKER_TMF",
            symbol="TMFR1",
        )
        asm.on_cancel(
            client_order_id="R47:43",
            cancel_payload={"event": "dispatched", "intent_type": "CANCEL"},
        )
        asm.on_terminal(
            client_order_id="R47:43",
            lifecycle_status="canceled",
            ts_emit_ns=1_700_000_000_000_000_001,
        )

        assert len(emitted) == 1
        e = emitted[0]
        assert e.lifecycle_status == "canceled"
        assert e.fills == []
        assert len(e.cancels) == 1


class TestFanIn:
    def test_three_fills_one_cancel_one_explanation(self) -> None:
        emitted: list[OrderExplanation] = []
        asm = _make_assembler(sink=emitted.append)

        asm.on_intent(
            client_order_id="R47:44",
            trace_id="trace-fanin",
            strategy_id="R47_MAKER_TMF",
            symbol="TMFR1",
        )
        for i in range(3):
            asm.on_fill(
                client_order_id="R47:44",
                fill_payload={"fill_id": f"f-{i}", "qty": 1, "price_scaled": 171960000 + i},
            )
        asm.on_cancel(
            client_order_id="R47:44",
            cancel_payload={"event": "dispatched", "intent_type": "CANCEL", "remaining": 0},
        )
        asm.on_terminal(
            client_order_id="R47:44",
            lifecycle_status="filled",
            ts_emit_ns=1_700_000_000_000_000_002,
        )

        assert len(emitted) == 1
        e = emitted[0]
        assert len(e.fills) == 3
        assert [f["fill_id"] for f in e.fills] == ["f-0", "f-1", "f-2"]
        assert len(e.cancels) == 1


class TestTTLSweep:
    def test_stale_entry_emits_incomplete(self, monkeypatch: pytest.MonkeyPatch) -> None:
        emitted: list[OrderExplanation] = []
        asm = _make_assembler(sink=emitted.append, ttl_s=10.0)

        # Register an intent at "t=100"
        import hft_platform.order.explanation as expl_mod

        monkeypatch.setattr(expl_mod.time, "monotonic", lambda: 100.0)
        asm.on_intent(
            client_order_id="R47:45",
            trace_id="trace-stale",
            strategy_id="R47_MAKER_TMF",
            symbol="TMFR1",
        )

        # Sweep at "t=200" — well past TTL=10s
        monkeypatch.setattr(expl_mod.time, "monotonic", lambda: 200.0)
        swept = asm.sweep_stale(now_mono=200.0, now_ns=1_700_000_000_000_000_003)

        assert swept == 1
        assert len(emitted) == 1
        assert emitted[0].lifecycle_status == "incomplete"
        assert asm.in_flight == 0

    def test_sweep_rate_limited_to_60s(self, monkeypatch: pytest.MonkeyPatch) -> None:
        emitted: list[OrderExplanation] = []
        asm = _make_assembler(sink=emitted.append, ttl_s=1.0)

        # Pin monotonic so insert and sweep share the same logical timeline.
        import hft_platform.order.explanation as expl_mod

        monkeypatch.setattr(expl_mod.time, "monotonic", lambda: 100.0)
        asm.on_intent(
            client_order_id="R47:46",
            trace_id="trace-rate",
            strategy_id="R47_MAKER_TMF",
            symbol="TMFR1",
        )
        # First sweep at t=1000 runs (cutoff = 999, entry at 100 → stale).
        first = asm.sweep_stale(now_mono=1000.0, now_ns=0)
        # Second sweep within 60s of the first is rate-limited even with
        # additional stale entries; returns 0.
        second = asm.sweep_stale(now_mono=1010.0, now_ns=0)
        assert first == 1
        assert second == 0


class TestGuards:
    def test_empty_trace_id_drops_registration(self) -> None:
        emitted: list[OrderExplanation] = []
        asm = _make_assembler(sink=emitted.append)
        asm.on_intent(
            client_order_id="R47:47",
            trace_id="",  # phantom-DLQ replay shape
            strategy_id="R47_MAKER_TMF",
            symbol="TMFR1",
        )
        asm.on_fill(client_order_id="R47:47", fill_payload={"fill_id": "x"})
        result = asm.on_terminal(
            client_order_id="R47:47",
            lifecycle_status="filled",
            ts_emit_ns=0,
        )
        assert result is None
        assert emitted == []
        assert asm.in_flight == 0

    def test_empty_client_order_id_drops_registration(self) -> None:
        asm = _make_assembler()
        asm.on_intent(
            client_order_id="",
            trace_id="trace-x",
            strategy_id="R47_MAKER_TMF",
            symbol="TMFR1",
        )
        assert asm.in_flight == 0

    def test_reregistration_preserves_existing_fills(self) -> None:
        emitted: list[OrderExplanation] = []
        asm = _make_assembler(sink=emitted.append)
        asm.on_intent(
            client_order_id="R47:48",
            trace_id="trace-reconnect",
            strategy_id="R47_MAKER_TMF",
            symbol="TMFR1",
        )
        asm.on_fill(client_order_id="R47:48", fill_payload={"fill_id": "f-pre-reconnect"})
        # Re-register (e.g. broker reconnect) — must NOT clobber the fill.
        asm.on_intent(
            client_order_id="R47:48",
            trace_id="trace-reconnect",
            strategy_id="R47_MAKER_TMF",
            symbol="TMFR1",
        )
        asm.on_terminal(
            client_order_id="R47:48",
            lifecycle_status="filled",
            ts_emit_ns=1,
        )
        assert len(emitted) == 1
        assert emitted[0].fills[0]["fill_id"] == "f-pre-reconnect"

    def test_sink_exception_swallowed(self) -> None:
        def _bad(_e: OrderExplanation) -> None:
            raise RuntimeError("sink down")

        asm = _make_assembler(sink=_bad)
        asm.on_intent(
            client_order_id="R47:49",
            trace_id="trace-sink",
            strategy_id="R47_MAKER_TMF",
            symbol="TMFR1",
        )
        # Must not raise even though sink raises.
        asm.on_terminal(
            client_order_id="R47:49",
            lifecycle_status="filled",
            ts_emit_ns=1,
        )

    def test_terminal_for_unknown_key_returns_none(self) -> None:
        asm = _make_assembler()
        result = asm.on_terminal(
            client_order_id="never-registered",
            lifecycle_status="filled",
            ts_emit_ns=0,
        )
        assert result is None


class TestCapacity:
    def test_capacity_eviction_emits_oldest_as_incomplete(self) -> None:
        emitted: list[OrderExplanation] = []
        asm = _make_assembler(sink=emitted.append, max_in_flight=2)

        # Fill capacity then add one more — oldest should be evicted as incomplete.
        for i in range(3):
            asm.on_intent(
                client_order_id=f"R47:{100 + i}",
                trace_id=f"trace-{i}",
                strategy_id="R47_MAKER_TMF",
                symbol="TMFR1",
            )

        # One eviction event was finalized as 'incomplete'.
        incomplete_events = [e for e in emitted if e.lifecycle_status == "incomplete"]
        assert len(incomplete_events) == 1
        assert asm.in_flight == 2
