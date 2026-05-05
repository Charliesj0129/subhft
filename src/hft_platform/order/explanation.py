"""Canonical per-order explanation assembler (loop_v1 step L8).

Today an order's reason is scattered across strategy logs, risk logs, the
order adapter audit hook, the fill recorder, decision traces, and metrics.
``OrderExplanationAssembler`` collects these signals keyed on
``(trace_id, client_order_id)`` and emits a single ``OrderExplanation`` row
when the order reaches a terminal lifecycle state.

Join keys
---------
* ``client_order_id`` — primary join key. Set on ``OrderCommand`` at
  dispatch and stamped onto every ``FillEvent`` by ``ExecutionRouter``.
* ``trace_id`` — secondary key, propagated from ``OrderIntent`` through
  ``OrderCommand`` and (post-L8) onto ``FillEvent``. Empty when a fill
  was reconstructed from a phantom-order DLQ replay; the assembler
  treats empty trace as "no explanation row" and drops the entry.

Lifecycle TTL
-------------
Lifecycle TTL is governed by ``OrderAdapter._live_orders_ttl_s`` (default
300s, env-tunable) — the assembler does not impose a fixed 60s window.
When an entry exceeds TTL without seeing a terminal callback, the sweeper
emits the explanation with ``lifecycle_status = "incomplete"``.

Fan-in
------
A single ``OrderIntent`` may produce many ``OrderCommand`` events
(NEW + AMEND + CANCEL) and many ``FillEvent`` rows (partial fills). The
assembler accumulates fills and cancels into list fields and emits one
explanation per intent at terminal time.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Callable, Literal

from structlog import get_logger

logger = get_logger("hft_platform.order.explanation")

LifecycleStatus = Literal["filled", "partial", "canceled", "rejected", "incomplete"]


@dataclass(frozen=True, slots=True)
class OrderExplanation:
    """Frozen, serializable canonical record of why an order was placed.

    All scalar fields default to empty/zero so a partial assembly remains
    representable (the recorder writes ``''`` to ClickHouse for missing
    audit fields, matching L7's dual-write contract).
    """

    trace_id: str
    client_order_id: str
    loop_id: str
    strategy_id: str
    strategy_version: str
    config_hash: str
    git_sha: str
    data_session_id: str
    symbol: str
    feature_snapshot: dict[str, Any]
    strategy_decision: dict[str, Any]
    risk_decision: dict[str, Any]
    order: dict[str, Any]
    fills: list[dict[str, Any]]
    cancels: list[dict[str, Any]]
    pnl_after: dict[str, Any] | None
    lifecycle_status: LifecycleStatus
    ts_emit: int  # ns since epoch


@dataclass(slots=True)
class _PendingExplanation:
    """In-flight assembly state for a single ``client_order_id``.

    Fields mirror ``OrderExplanation`` but allow mutation as new signals
    arrive. Promoted to ``OrderExplanation`` on terminal/sweep.
    """

    trace_id: str
    client_order_id: str
    strategy_id: str
    symbol: str
    inserted_mono: float
    feature_snapshot: dict[str, Any] = field(default_factory=dict)
    strategy_decision: dict[str, Any] = field(default_factory=dict)
    risk_decision: dict[str, Any] = field(default_factory=dict)
    order: dict[str, Any] = field(default_factory=dict)
    fills: list[dict[str, Any]] = field(default_factory=list)
    cancels: list[dict[str, Any]] = field(default_factory=list)
    pnl_after: dict[str, Any] | None = None


class OrderExplanationAssembler:
    """Collect order-lifecycle signals and emit one ``OrderExplanation``
    per terminal order.

    The assembler is a passive collector. Callers (OrderAdapter,
    ExecutionRouter, optionally RiskEngine) feed it via ``on_*`` methods.
    On terminal state or TTL expiry it emits to ``sink`` — typically the
    recorder queue.

    Construction parameters identify the loop and the running build so
    every emitted row can be traced back to the exact code+config that
    produced it. ``ttl_s`` mirrors ``OrderAdapter._live_orders_ttl_s``.
    """

    def __init__(
        self,
        *,
        loop_id: str,
        strategy_version: str,
        config_hash: str,
        git_sha: str,
        data_session_id: str,
        sink: Callable[[OrderExplanation], None] | None = None,
        ttl_s: float = 300.0,
        max_in_flight: int = 10000,
    ) -> None:
        self._loop_id = loop_id
        self._strategy_version = strategy_version
        self._config_hash = config_hash
        self._git_sha = git_sha
        self._data_session_id = data_session_id
        self._sink = sink
        self._ttl_s = float(ttl_s)
        self._max_in_flight = int(max_in_flight)
        self._pending: dict[str, _PendingExplanation] = {}
        self._last_sweep_mono: float = 0.0

    # ------------------------------------------------------------------ ingestion

    def on_intent(
        self,
        *,
        client_order_id: str,
        trace_id: str,
        strategy_id: str,
        symbol: str,
        intent_payload: dict[str, Any] | None = None,
        feature_snapshot: dict[str, Any] | None = None,
    ) -> None:
        """Register a new in-flight order. Called when OrderAdapter dispatches NEW.

        ``trace_id`` empty => the assembler drops the registration. Empty
        traces happen on phantom-order DLQ replays where the original
        intent was lost; emitting an explanation in that case would
        produce a row with no causal anchor.
        """
        if not trace_id:
            return
        if not client_order_id:
            return
        if client_order_id in self._pending:
            # Re-registration during reconnect: keep existing entry, do not
            # overwrite collected fills/cancels.
            return
        self._evict_oldest_if_full()
        self._pending[client_order_id] = _PendingExplanation(
            trace_id=trace_id,
            client_order_id=client_order_id,
            strategy_id=strategy_id,
            symbol=symbol,
            inserted_mono=time.monotonic(),
            strategy_decision=dict(intent_payload or {}),
            feature_snapshot=dict(feature_snapshot or {}),
        )

    def on_command(self, *, client_order_id: str, command_payload: dict[str, Any]) -> None:
        """Record the OrderCommand details (decision_price, arrival_price, qty, side)."""
        entry = self._pending.get(client_order_id)
        if entry is None:
            return
        entry.order = dict(command_payload)

    def on_risk_decision(self, *, client_order_id: str, decision_payload: dict[str, Any]) -> None:
        entry = self._pending.get(client_order_id)
        if entry is None:
            return
        entry.risk_decision = dict(decision_payload)

    def on_fill(self, *, client_order_id: str, fill_payload: dict[str, Any]) -> None:
        entry = self._pending.get(client_order_id)
        if entry is None:
            return
        entry.fills.append(dict(fill_payload))

    def on_cancel(self, *, client_order_id: str, cancel_payload: dict[str, Any]) -> None:
        entry = self._pending.get(client_order_id)
        if entry is None:
            return
        entry.cancels.append(dict(cancel_payload))

    def on_pnl_after(self, *, client_order_id: str, pnl_payload: dict[str, Any]) -> None:
        entry = self._pending.get(client_order_id)
        if entry is None:
            return
        entry.pnl_after = dict(pnl_payload)

    def on_terminal(
        self,
        *,
        client_order_id: str,
        lifecycle_status: LifecycleStatus,
        ts_emit_ns: int,
    ) -> OrderExplanation | None:
        """Promote the pending entry to a frozen ``OrderExplanation`` and emit.

        Returns the emitted explanation (also passed to ``sink`` if set)
        or ``None`` when there was no pending entry for the key.
        """
        entry = self._pending.pop(client_order_id, None)
        if entry is None:
            return None
        return self._finalize(entry, lifecycle_status, ts_emit_ns)

    # ------------------------------------------------------------------ sweeping

    def sweep_stale(self, *, now_mono: float | None = None, now_ns: int | None = None) -> int:
        """Emit ``incomplete`` explanations for entries past ``ttl_s``.

        Returns the number of swept entries. Rate-limited to once per 60s
        to keep the cost off the order-dispatch path even when called from
        a per-tick supervisor loop.
        """
        now_mono = time.monotonic() if now_mono is None else float(now_mono)
        if now_mono - self._last_sweep_mono < 60.0:
            return 0
        self._last_sweep_mono = now_mono
        cutoff_mono = now_mono - self._ttl_s
        ts_ns = int(time.time_ns()) if now_ns is None else int(now_ns)
        stale_keys = [k for k, e in self._pending.items() if e.inserted_mono < cutoff_mono]
        for k in stale_keys:
            entry = self._pending.pop(k, None)
            if entry is None:
                continue
            self._finalize(entry, "incomplete", ts_ns)
        if stale_keys:
            logger.warning(
                "order_explanation_stale_swept",
                count=len(stale_keys),
                remaining=len(self._pending),
                ttl_s=self._ttl_s,
            )
        return len(stale_keys)

    # ------------------------------------------------------------------ helpers

    def _finalize(
        self,
        entry: _PendingExplanation,
        lifecycle_status: LifecycleStatus,
        ts_emit_ns: int,
    ) -> OrderExplanation:
        explanation = OrderExplanation(
            trace_id=entry.trace_id,
            client_order_id=entry.client_order_id,
            loop_id=self._loop_id,
            strategy_id=entry.strategy_id,
            strategy_version=self._strategy_version,
            config_hash=self._config_hash,
            git_sha=self._git_sha,
            data_session_id=self._data_session_id,
            symbol=entry.symbol,
            feature_snapshot=dict(entry.feature_snapshot),
            strategy_decision=dict(entry.strategy_decision),
            risk_decision=dict(entry.risk_decision),
            order=dict(entry.order),
            fills=[dict(f) for f in entry.fills],
            cancels=[dict(c) for c in entry.cancels],
            pnl_after=dict(entry.pnl_after) if entry.pnl_after else None,
            lifecycle_status=lifecycle_status,
            ts_emit=int(ts_emit_ns),
        )
        if self._sink is not None:
            try:
                self._sink(explanation)
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "order_explanation_sink_error",
                    error=str(exc),
                    client_order_id=explanation.client_order_id,
                )
        return explanation

    def _evict_oldest_if_full(self) -> None:
        if len(self._pending) < self._max_in_flight:
            return
        try:
            oldest_key = min(self._pending, key=lambda k: self._pending[k].inserted_mono)
        except ValueError:
            return
        evicted = self._pending.pop(oldest_key, None)
        if evicted is None:
            return
        # Promote to incomplete so the operator still sees the orphan instead
        # of silently dropping it.
        self._finalize(evicted, "incomplete", int(time.time_ns()))
        logger.warning(
            "order_explanation_evicted_for_capacity",
            client_order_id=oldest_key,
            in_flight=len(self._pending),
            max_in_flight=self._max_in_flight,
        )

    @property
    def in_flight(self) -> int:
        return len(self._pending)
