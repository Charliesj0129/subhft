"""CE2-03: GatewayService — owns RiskEngine + OrderAdapter dispatch loop.

Architecture (D2):
- Runs as a single asyncio task; serializes all intent processing.
- RiskEngine.evaluate() is called synchronously (CPU-only, no I/O).
- RiskEngine.run() is NOT started as a separate task.
- Processing pipeline per envelope:
    1. dedup.check_or_reserve(idempotency_key) → return cached if hit
    2. policy.gate(intent, sg_state)            → reject if policy blocks
    3. exposure.check_and_update(key, intent)   → reject if overshoot
    4. risk_engine.evaluate(intent)             → synchronous
    5. risk_engine.create_command(intent)       → synchronous
    6. dedup.commit(key, approved, reason, cmd_id)
    7. order_adapter._api_queue.put_nowait(cmd)
- Latency histogram CE2-07 wraps steps 1-7.
"""

from __future__ import annotations

import asyncio
import time
from typing import Any

from structlog import get_logger

from hft_platform.contracts.strategy import IntentType
from hft_platform.gateway.channel import IntentEnvelope, LocalIntentChannel
from hft_platform.gateway.dedup import IdempotencyStore
from hft_platform.gateway.exposure import ExposureKey, ExposureStore
from hft_platform.gateway.policy import GatewayPolicy

logger = get_logger("gateway.service")


class GatewayService:
    """Orchestrates intent validation and dispatch in a single asyncio task.

    Parameters mirror plan D2:
        channel:        LocalIntentChannel — receives intents from StrategyRunner
        risk_engine:    RiskEngine — evaluate() + create_command() called synchronously
        order_adapter:  OrderAdapter — _api_queue.put_nowait(cmd)
        exposure_store: ExposureStore
        dedup_store:    IdempotencyStore
        storm_guard:    StormGuard
        policy:         GatewayPolicy
    """

    def __init__(
        self,
        channel: LocalIntentChannel,
        risk_engine: Any,
        order_adapter: Any,
        exposure_store: ExposureStore,
        dedup_store: IdempotencyStore,
        storm_guard: Any,
        policy: GatewayPolicy,
    ) -> None:
        self._channel = channel
        self._risk_engine = risk_engine
        self._order_adapter = order_adapter
        self._exposure = exposure_store
        self._dedup = dedup_store
        self._storm_guard = storm_guard
        self._policy = policy
        self.running = False
        self._dispatched = 0
        self._rejected = 0
        self._dedup_hits = 0

    # ── Main loop ─────────────────────────────────────────────────────────

    async def run(self) -> None:
        self.running = True
        logger.info("GatewayService started")
        try:
            while self.running:
                envelope = await self._channel.receive()
                try:
                    await self._process_envelope(envelope)
                except Exception as exc:
                    logger.error(
                        "GatewayService envelope error",
                        ack_token=envelope.ack_token,
                        error=str(exc),
                    )
                finally:
                    self._channel.task_done()
        except asyncio.CancelledError:
            logger.info("GatewayService stopping")
        finally:
            self.running = False
            # Persist dedup state on clean shutdown (async-safe: run in thread)
            try:
                await asyncio.to_thread(self._dedup.persist)
            except Exception as exc:
                logger.warning("Dedup persist on shutdown failed", error=str(exc))

    async def _process_envelope(self, envelope: IntentEnvelope) -> None:
        intent = envelope.intent
        key = intent.idempotency_key
        t0 = time.perf_counter_ns()

        # Step 1: Dedup check
        existing = self._dedup.check_or_reserve(key) if key else None
        if existing is not None and existing.approved is not None:
            self._dedup_hits += 1
            try:
                from hft_platform.observability.metrics import MetricsRegistry

                MetricsRegistry.get().gateway_dedup_hits_total.inc()
            except Exception:
                pass
            logger.debug(
                "Dedup hit — returning cached decision",
                key=key,
                approved=existing.approved,
                reason=existing.reason_code,
            )
            return

        # Step 2: Policy gate
        sg_state = self._storm_guard.state
        allowed, reason = self._policy.gate(intent, sg_state)
        if not allowed:
            self._rejected += 1
            self._dedup.commit(key, False, reason, 0)
            self._emit_reject(reason)
            logger.debug("Gateway policy rejected", reason=reason, ack_token=envelope.ack_token)
            self._record_latency(t0)
            return

        # Step 3: Exposure check
        exp_key = ExposureKey(
            account="default",
            strategy_id=intent.strategy_id,
            symbol=intent.symbol,
        )
        if intent.intent_type != IntentType.CANCEL:
            exp_ok, exp_reason = self._exposure.check_and_update(exp_key, intent)
            if not exp_ok:
                self._rejected += 1
                self._dedup.commit(key, False, exp_reason, 0)
                self._emit_reject(exp_reason)
                logger.debug(
                    "Gateway exposure rejected",
                    reason=exp_reason,
                    ack_token=envelope.ack_token,
                )
                self._record_latency(t0)
                return

        # Step 4: Risk evaluate (synchronous, CPU-only)
        decision = self._risk_engine.evaluate(intent)

        # Step 5: Create command
        if decision.approved:
            cmd = self._risk_engine.create_command(decision.intent)
            # Step 6: Commit dedup
            self._dedup.commit(key, True, "OK", cmd.cmd_id)
            # Step 7: Dispatch to order adapter
            try:
                self._order_adapter._api_queue.put_nowait(cmd)
                self._dispatched += 1
            except asyncio.QueueFull:
                self._dedup.commit(key, False, "ORDER_QUEUE_FULL", 0)
                self._emit_reject("ORDER_QUEUE_FULL")
                logger.warning("Order queue full — intent dropped", ack_token=envelope.ack_token)
        else:
            self._rejected += 1
            self._dedup.commit(key, False, decision.reason_code, 0)
            self._emit_reject(decision.reason_code)
            # Release exposure on rejection (was reserved in step 3)
            if intent.intent_type != IntentType.CANCEL:
                self._exposure.release_exposure(exp_key, intent)
            logger.debug(
                "Risk rejected intent",
                reason=decision.reason_code,
                ack_token=envelope.ack_token,
            )

        self._record_latency(t0)
        self._update_channel_depth_metric()

    # ── Health ────────────────────────────────────────────────────────────

    def get_health(self) -> dict:
        """Return basic health snapshot (used by system.py supervisor)."""
        return {
            "running": self.running,
            "dispatched": self._dispatched,
            "rejected": self._rejected,
            "dedup_hits": self._dedup_hits,
            "channel_depth": self._channel.qsize(),
            "policy_mode": self._policy.mode.value,
        }

    # ── Private helpers ───────────────────────────────────────────────────

    def _emit_reject(self, reason: str) -> None:
        try:
            from hft_platform.observability.metrics import MetricsRegistry

            MetricsRegistry.get().gateway_reject_total.labels(reason=reason).inc()
        except Exception:
            pass

    def _record_latency(self, t0: int) -> None:
        try:
            from hft_platform.observability.metrics import MetricsRegistry

            MetricsRegistry.get().gateway_dispatch_latency_ns.observe(time.perf_counter_ns() - t0)
        except Exception:
            pass

    def _update_channel_depth_metric(self) -> None:
        try:
            from hft_platform.observability.metrics import MetricsRegistry

            MetricsRegistry.get().gateway_intent_channel_depth.set(self._channel.qsize())
        except Exception:
            pass
