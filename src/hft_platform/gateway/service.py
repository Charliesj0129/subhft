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
import os
import time
from typing import Any

from structlog import get_logger

from hft_platform.contracts.strategy import IntentType
from hft_platform.gateway.channel import (
    IntentEnvelope,
    LocalIntentChannel,
    TypedIntentEnvelope,
)
from hft_platform.gateway.dedup import IdempotencyStore
from hft_platform.gateway.exposure import ExposureKey, ExposureLimitError, ExposureStore
from hft_platform.gateway.policy import GatewayPolicy

logger = get_logger("gateway.service")


def _bool_env(value: Any, default: bool = False) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


def _obs_policy() -> str:
    value = str(os.getenv("HFT_GATEWAY_OBS_POLICY", os.getenv("HFT_OBS_POLICY", ""))).strip().lower()
    if value in {"minimal", "balanced", "debug"}:
        return value
    return ""


def _int_env(name: str, default: int) -> int:
    try:
        return max(1, int(os.getenv(name, str(default))))
    except ValueError:
        return max(1, int(default))


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
        risk_engine: Any,  # deferred: RiskEngine lives in hft_platform.risk — avoids circular import
        order_adapter: Any,  # deferred: OrderAdapter lives in hft_platform.execution — avoids circular import
        exposure_store: ExposureStore,
        dedup_store: IdempotencyStore,
        storm_guard: Any,  # deferred: StormGuard lives in hft_platform.risk — avoids circular import
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
        self._metrics = None
        self._metrics_owner_id: int | None = None
        self._gateway_reject_metric_cache: dict[str, Any] = {}
        self._gateway_dispatch_latency_metric = None
        self._gateway_depth_metric = None
        self._gateway_dedup_hits_metric = None
        self._gateway_reject_counter = 0
        self._gateway_latency_counter = 0
        self._gateway_depth_counter = 0
        self._gateway_dedup_counter = 0
        obs_policy = _obs_policy()
        default_every = 1 if obs_policy in {"", "debug"} else (4 if obs_policy == "balanced" else 16)
        default_latency_every = 1 if obs_policy in {"", "debug"} else (4 if obs_policy == "balanced" else 16)
        default_depth_every = 8 if obs_policy in {"", "debug"} else (16 if obs_policy == "balanced" else 64)
        default_reject_every = 1
        default_dedup_every = 8 if obs_policy in {"", "debug"} else (16 if obs_policy == "balanced" else 64)
        self._gateway_metrics_sample_every = _int_env("HFT_GATEWAY_METRICS_SAMPLE_EVERY", default_every)
        self._gateway_latency_sample_every = _int_env(
            "HFT_GATEWAY_LATENCY_SAMPLE_EVERY",
            default_latency_every,
        )
        self._gateway_depth_sample_every = _int_env(
            "HFT_GATEWAY_DEPTH_SAMPLE_EVERY",
            default_depth_every,
        )
        self._gateway_reject_sample_every = _int_env(
            "HFT_GATEWAY_REJECT_METRICS_SAMPLE_EVERY",
            default_reject_every,
        )
        self._gateway_dedup_sample_every = _int_env(
            "HFT_GATEWAY_DEDUP_METRICS_SAMPLE_EVERY",
            default_dedup_every,
        )
        self._metrics_enabled = not (
            policy == "minimal" and _bool_env(os.getenv("HFT_GATEWAY_METRICS", "1"), default=True) is False
        )
        self._metrics_enabled = _bool_env(os.getenv("HFT_GATEWAY_METRICS", "1"), default=self._metrics_enabled)
        self._refresh_metrics_registry()

    # ── Main loop ─────────────────────────────────────────────────────────

    async def run(self) -> None:
        self.running = True
        logger.info("GatewayService started")
        try:
            while self.running:
                receive_raw = getattr(self._channel, "receive_raw", None)
                if callable(receive_raw):
                    envelope = await receive_raw()
                else:
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

    async def _process_envelope(self, envelope: IntentEnvelope | TypedIntentEnvelope) -> None:
        typed_frame = envelope.payload if isinstance(envelope, TypedIntentEnvelope) else None
        if typed_frame is not None and hasattr(self._risk_engine, "typed_frame_view"):
            intent = self._risk_engine.typed_frame_view(typed_frame)
        else:
            intent = envelope.intent  # type: ignore[union-attr]
        key = getattr(intent, "idempotency_key", "")
        is_typed_view = typed_frame is not None
        intent_type_value = int(intent.intent_type)
        t0 = time.perf_counter_ns()

        # Step 1: Dedup check
        if key:
            if is_typed_view and hasattr(self._dedup, "check_or_reserve_typed"):
                existing = self._dedup.check_or_reserve_typed(key)
            else:
                existing = self._dedup.check_or_reserve(key)
        else:
            existing = None
        if existing is not None and existing.approved is not None:
            self._dedup_hits += 1
            self._inc_dedup_hit_metric()
            logger.debug(
                "Dedup hit — returning cached decision",
                key=key,
                approved=existing.approved,
                reason=existing.reason_code,
            )
            return

        # Step 2: Policy gate
        sg_state = self._storm_guard.state
        if is_typed_view and hasattr(self._policy, "gate_typed"):
            allowed, reason = self._policy.gate_typed(intent_type_value, sg_state)
        else:
            allowed, reason = self._policy.gate(intent, sg_state)
        if not allowed:
            self._rejected += 1
            if is_typed_view and hasattr(self._dedup, "commit_typed"):
                self._dedup.commit_typed(key, False, reason, 0)
            else:
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
        if intent_type_value != int(IntentType.CANCEL):
            try:
                if is_typed_view and hasattr(self._exposure, "check_and_update_typed"):
                    exp_ok, exp_reason = self._exposure.check_and_update_typed(
                        exp_key,
                        intent_type=intent_type_value,
                        price=int(intent.price),
                        qty=int(intent.qty),
                    )
                else:
                    exp_ok, exp_reason = self._exposure.check_and_update(exp_key, intent)
            except ExposureLimitError as exc:
                # Symbol-cardinality hard limit reached; reject and commit dedup so
                # the same key is not retried in a busy-loop (CE2-12).
                self._rejected += 1
                if is_typed_view and hasattr(self._dedup, "commit_typed"):
                    self._dedup.commit_typed(key, False, "EXPOSURE_SYMBOL_LIMIT", 0)
                else:
                    self._dedup.commit(key, False, "EXPOSURE_SYMBOL_LIMIT", 0)
                self._emit_reject("EXPOSURE_SYMBOL_LIMIT")
                logger.error(
                    "GatewayService exposure symbol limit hit",
                    ack_token=envelope.ack_token,
                    error=str(exc),
                )
                self._record_latency(t0)
                return
            if not exp_ok:
                self._rejected += 1
                if is_typed_view and hasattr(self._dedup, "commit_typed"):
                    self._dedup.commit_typed(key, False, exp_reason, 0)
                else:
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
        if typed_frame is not None and hasattr(self._risk_engine, "evaluate_typed_frame"):
            decision = self._risk_engine.evaluate_typed_frame(typed_frame, intent_view=intent)
        else:
            decision = self._risk_engine.evaluate(intent)

        # Step 5: Create command
        if decision.approved:
            cmd = None
            typed_cmd_frame = None
            typed_submit = getattr(self._order_adapter, "submit_typed_command_nowait", None)
            typed_adapter_supported = getattr(self._order_adapter, "_supports_typed_command_ingress", False) is True
            if (
                typed_frame is not None
                and typed_adapter_supported
                and callable(typed_submit)
                and hasattr(self._risk_engine, "create_typed_command_frame_from_typed_frame")
            ):
                typed_cmd_frame = self._risk_engine.create_typed_command_frame_from_typed_frame(typed_frame)
                cmd_id_for_commit = int(typed_cmd_frame[1]) if len(typed_cmd_frame) > 1 else 0
            else:
                if typed_frame is not None and hasattr(self._risk_engine, "create_command_from_typed_frame"):
                    # Materialize only after passing policy/exposure/risk checks.
                    cmd = self._risk_engine.create_command_from_typed_frame(typed_frame, intent_view=intent)
                else:
                    cmd = self._risk_engine.create_command(decision.intent)
                cmd_id_for_commit = int(cmd.cmd_id)
            # Step 6: Commit dedup
            if is_typed_view and hasattr(self._dedup, "commit_typed"):
                self._dedup.commit_typed(key, True, "OK", cmd_id_for_commit)
            else:
                self._dedup.commit(key, True, "OK", cmd_id_for_commit)
            # Step 7: Dispatch to order adapter
            try:
                if typed_cmd_frame is not None and callable(typed_submit):
                    typed_submit(typed_cmd_frame)
                else:
                    self._order_adapter._api_queue.put_nowait(cmd)
                self._dispatched += 1
            except asyncio.QueueFull:
                self._rejected += 1
                if is_typed_view and hasattr(self._dedup, "commit_typed"):
                    self._dedup.commit_typed(key, False, "ORDER_QUEUE_FULL", 0)
                else:
                    self._dedup.commit(key, False, "ORDER_QUEUE_FULL", 0)
                self._emit_reject("ORDER_QUEUE_FULL")
                logger.warning("Order queue full — intent dropped", ack_token=envelope.ack_token)
        else:
            self._rejected += 1
            if is_typed_view and hasattr(self._dedup, "commit_typed"):
                self._dedup.commit_typed(key, False, decision.reason_code, 0)
            else:
                self._dedup.commit(key, False, decision.reason_code, 0)
            self._emit_reject(decision.reason_code)
            # Release exposure on rejection (was reserved in step 3)
            if intent_type_value != int(IntentType.CANCEL):
                if is_typed_view and hasattr(self._exposure, "release_exposure_typed"):
                    self._exposure.release_exposure_typed(
                        exp_key,
                        intent_type=intent_type_value,
                        price=int(intent.price),
                        qty=int(intent.qty),
                    )
                else:
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
        if not self._metrics_enabled:
            return
        try:
            self._gateway_reject_counter = (self._gateway_reject_counter + 1) % self._gateway_reject_sample_every
            if self._gateway_reject_counter != 0:
                return
            metrics = self._metrics_or_refresh()
            if metrics is None:
                return
            child = self._gateway_reject_metric_cache.get(reason)
            if child is None:
                child = metrics.gateway_reject_total.labels(reason=reason)
                self._gateway_reject_metric_cache[reason] = child
            child.inc()
        except Exception:  # noqa: BLE001  # best-effort metrics: never break hot path
            pass

    def _record_latency(self, t0: int) -> None:
        if not self._metrics_enabled:
            return
        try:
            self._gateway_latency_counter = (self._gateway_latency_counter + 1) % self._gateway_latency_sample_every
            if self._gateway_latency_counter != 0:
                return
            metrics = self._metrics_or_refresh()
            if metrics is None:
                return
            metric = self._gateway_dispatch_latency_metric or metrics.gateway_dispatch_latency_ns
            self._gateway_dispatch_latency_metric = metric
            metric.observe(time.perf_counter_ns() - t0)
        except Exception:  # noqa: BLE001  # best-effort metrics: never break hot path
            pass

    def _update_channel_depth_metric(self) -> None:
        if not self._metrics_enabled:
            return
        try:
            self._gateway_depth_counter = (self._gateway_depth_counter + 1) % self._gateway_depth_sample_every
            if self._gateway_depth_counter != 0:
                return
            metrics = self._metrics_or_refresh()
            if metrics is None:
                return
            metric = self._gateway_depth_metric or metrics.gateway_intent_channel_depth
            self._gateway_depth_metric = metric
            metric.set(self._channel.qsize())
        except Exception:  # noqa: BLE001  # best-effort metrics: never break hot path
            pass

    def _refresh_metrics_registry(self) -> None:
        try:
            from hft_platform.observability.metrics import MetricsRegistry

            self._metrics = MetricsRegistry.get()
            self._metrics_owner_id = id(self._metrics) if self._metrics is not None else None
            self._gateway_reject_metric_cache.clear()
            if self._metrics is not None:
                self._gateway_dispatch_latency_metric = self._metrics.gateway_dispatch_latency_ns
                self._gateway_depth_metric = self._metrics.gateway_intent_channel_depth
                self._gateway_dedup_hits_metric = self._metrics.gateway_dedup_hits_total
            else:
                self._gateway_dispatch_latency_metric = None
                self._gateway_depth_metric = None
                self._gateway_dedup_hits_metric = None
        except Exception:
            self._metrics = None
            self._metrics_owner_id = None
            self._gateway_reject_metric_cache.clear()
            self._gateway_dispatch_latency_metric = None
            self._gateway_depth_metric = None
            self._gateway_dedup_hits_metric = None

    def _metrics_or_refresh(self):
        metrics = self._metrics
        if metrics is None:
            self._refresh_metrics_registry()
            return self._metrics
        owner_id = id(metrics)
        if owner_id != self._metrics_owner_id:
            self._refresh_metrics_registry()
            return self._metrics
        return metrics

    def _inc_dedup_hit_metric(self) -> None:
        if not self._metrics_enabled:
            return
        try:
            self._gateway_dedup_counter = (self._gateway_dedup_counter + 1) % self._gateway_dedup_sample_every
            if self._gateway_dedup_counter != 0:
                return
            metrics = self._metrics_or_refresh()
            if metrics is None:
                return
            metric = self._gateway_dedup_hits_metric or metrics.gateway_dedup_hits_total
            self._gateway_dedup_hits_metric = metric
            metric.inc()
        except Exception:
            pass
