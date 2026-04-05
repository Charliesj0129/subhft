import asyncio
import collections
import importlib
import os
import threading
import time
from typing import Any

import yaml
from structlog import get_logger

from hft_platform.contracts.strategy import IntentType, OrderCommand, OrderIntent, RiskDecision, RiskFeedback, StormGuardState
from hft_platform.core import timebase
from hft_platform.core.pricing import PriceScaleProvider
from hft_platform.observability.latency import LatencyRecorder
from hft_platform.observability.metrics import MetricsRegistry
from hft_platform.risk.storm_guard import StormGuard
from hft_platform.risk.validators import (
    DailyLossLimitValidator,
    MaxNotionalValidator,
    PerSymbolNotionalValidator,
    PositionLimitValidator,
    PriceBandValidator,
)

logger = get_logger("risk_engine")

# Lazy import for Rust risk validator
_RustRiskValidator = None


def _load_rust_risk_validator() -> Any:
    global _RustRiskValidator
    if _RustRiskValidator is not None:
        return _RustRiskValidator
    try:
        rust_module = importlib.import_module("hft_platform.rust_core")
    except ImportError:
        try:
            rust_module = importlib.import_module("rust_core")
        except ImportError:
            rust_module = None
    _RustRiskValidator = getattr(rust_module, "RustRiskValidator", None) if rust_module is not None else None
    return _RustRiskValidator


def _obs_policy() -> str:
    value = str(os.getenv("HFT_RISK_OBS_POLICY", os.getenv("HFT_OBS_POLICY", ""))).strip().lower()
    if value in {"minimal", "balanced", "debug"}:
        return value
    return ""


def _get_trace_sampler() -> Any | None:
    try:
        from hft_platform.diagnostics.trace import get_trace_sampler

        return get_trace_sampler()
    except ImportError:
        logger.debug("Trace sampler unavailable", exc_info=True)
        return None


class RiskEngine:
    __slots__ = (
        "config_path",
        "intent_queue",
        "order_queue",
        "running",
        "config",
        "metrics",
        "latency",
        "_reject_metric_cache",
        "_reject_metric_cache_owner_id",
        "_reject_metric_counter",
        "_reject_metric_sample_every",
        "validators",
        "storm_guard",
        "_rust_validator",
        "_rust_validator_reason_map",
        "_cmd_id_lock_enabled",
        "_cmd_id_lock",
        "_monotonic_cmd_id",
        "_fast_gate",
        "_fast_gate_reason_map",
        "_trace_sampler",
        "_notification_dispatcher",
        "_order_dlq",
        "_ORDER_DLQ_MAX",
        "_dlq_ttl_ns",
        "_dlq_drain_interval",
        "_dlq_drain_counter",
        "_position_provider",
        "_rejection_sink",
        "_greeks_validator",
        "_validator0",
        "__dict__",
    )

    def __init__(
        self,
        config_path: str,
        intent_queue: asyncio.Queue,
        order_queue: asyncio.Queue,
        price_scale_provider: PriceScaleProvider | None = None,
        storm_guard: StormGuard | None = None,
        notification_dispatcher: Any | None = None,
        position_provider: Any | None = None,
        rejection_sink: asyncio.Queue | None = None,
        greeks_provider: Any | None = None,
    ):
        self.config_path = config_path
        self.intent_queue = intent_queue  # Input
        self.order_queue = order_queue  # Output
        self.running = False
        self.load_config()
        self.metrics = MetricsRegistry.get()
        self.latency = LatencyRecorder.get()
        self._reject_metric_cache: dict[tuple[str, str], Any] = {}
        self._reject_metric_cache_owner_id: int | None = id(self.metrics) if self.metrics is not None else None
        self._reject_metric_counter = 0
        risk_obs_policy = _obs_policy()
        default_reject_every = 1 if risk_obs_policy in {"", "debug", "balanced"} else 4
        self._reject_metric_sample_every = self._parse_sample_every(
            "HFT_RISK_REJECT_METRICS_SAMPLE_EVERY",
            default=default_reject_every,
        )
        self._position_provider = position_provider

        # Validators
        self.validators = [
            PriceBandValidator(self.config, price_scale_provider),
            MaxNotionalValidator(self.config, price_scale_provider),
            PerSymbolNotionalValidator(self.config, price_scale_provider),
            PositionLimitValidator(
                self.config,
                price_scale_provider,
                position_provider=self._current_strategy_symbol_net_position,
            ),
            DailyLossLimitValidator(self.config, price_scale_provider),
        ]
        # Pre-compute validators that Rust doesn't cover (avoid per-call isinstance)
        self._rust_uncovered_validators = [
            v for v in self.validators
            if isinstance(v, (PositionLimitValidator, DailyLossLimitValidator))
        ]
        shared_scale_cache: dict[str, int] = {}
        for validator in self.validators:
            if hasattr(validator, "_shared_scale_cache"):
                validator._shared_scale_cache = shared_scale_cache
        # Cache validator[0] reference to avoid list index on hot path
        self._validator0 = self.validators[0] if self.validators else None
        self.storm_guard = storm_guard if storm_guard is not None else StormGuard()
        self._notification_dispatcher = notification_dispatcher
        self._rust_validator = self._init_rust_validator(price_scale_provider)
        self._rust_validator_reason_map = {
            1: "PRICE_ZERO_OR_NEG",
            2: "PRICE_EXCEEDS_CAP",
            3: "PRICE_OUTSIDE_BAND",
            4: "MAX_NOTIONAL_EXCEEDED",
        }
        self._cmd_id_lock_enabled = self._bool_env(os.getenv("HFT_RISK_CMD_ID_LOCK", "0"), default=False)
        self._cmd_id_lock = threading.Lock() if self._cmd_id_lock_enabled else None
        self._monotonic_cmd_id = 0
        self._fast_gate = self._init_fast_gate()
        self._fast_gate_reason_map = {
            1: "FASTGATE_KILL_SWITCH",
            2: "FASTGATE_BAD_PRICE_NEG",
            3: "FASTGATE_BAD_PRICE_MAX",
            4: "FASTGATE_BAD_QTY_MAX",
            5: "FASTGATE_BAD_QTY_NEG",
        }
        self._trace_sampler = _get_trace_sampler()
        self._order_dlq: collections.deque = collections.deque()
        self._ORDER_DLQ_MAX: int = 256
        self._dlq_ttl_ns: int = int(float(os.getenv("HFT_RISK_DLQ_TTL_S", "30")) * 1_000_000_000)
        self._dlq_drain_interval: int = int(os.getenv("HFT_RISK_DLQ_DRAIN_INTERVAL", "50"))
        self._dlq_drain_counter: int = 0
        self._rejection_sink = rejection_sink
        self._greeks_validator = None
        if greeks_provider is not None:
            try:
                from hft_platform.risk.greeks_limit_validator import GreeksLimitValidator
                self._greeks_validator = GreeksLimitValidator(self.config, greeks_provider)
            except ImportError:
                logger.warning("greeks_limit_validator_unavailable")

    def _current_strategy_symbol_net_position(self, symbol: str, strategy_id: str) -> int:
        provider = self._position_provider
        if provider is None:
            return 0
        if callable(provider):
            return int(provider(symbol, strategy_id) or 0)

        positions = getattr(provider, "positions", {})
        net_qty = 0
        for pos in positions.values():
            if getattr(pos, "symbol", None) != symbol:
                continue
            if getattr(pos, "strategy_id", strategy_id) != strategy_id:
                continue
            net_qty += int(getattr(pos, "net_qty", 0) or 0)
        return net_qty

    @staticmethod
    def _bool_env(value: Any, default: bool = False) -> bool:
        if value is None:
            return default
        if isinstance(value, bool):
            return value
        return str(value).strip().lower() in {"1", "true", "yes", "on"}

    @classmethod
    def _parse_sample_every(cls, name: str, default: int = 1) -> int:
        try:
            return max(1, int(os.getenv(name, str(default))))
        except ValueError:
            return max(1, int(default))

    def _init_fast_gate(self) -> Any | None:
        if not self._bool_env(os.getenv("HFT_RISK_FAST_GATE", "0"), default=False):
            return None
        try:
            from hft_platform.risk.fast_gate import FastGate
        except ImportError as exc:  # pragma: no cover - import/jit environment dependent
            logger.warning("FastGate unavailable; disabling", error=str(exc))
            return None

        defaults = self.config.get("global_defaults", {})
        risk_cfg = self.config.get("risk", {})
        try:
            scale = int(os.getenv("HFT_RISK_FAST_GATE_PRICE_SCALE", "10000"))
        except ValueError:
            scale = 10_000
        max_price_cap = float(defaults.get("max_price_cap", 5000.0))  # precision-config
        all_caps = [max_price_cap]
        for key in ("max_price_cap_futures", "max_price_cap_options"):
            val = defaults.get(key)
            if val is not None:
                all_caps.append(float(val))
        coarse_cap = max(all_caps)
        max_price_scaled = int(coarse_cap * max(1, scale))
        max_qty = int(
            os.getenv(
                "HFT_RISK_FAST_GATE_MAX_QTY",
                str(risk_cfg.get("max_order_size", defaults.get("max_qty", 1_000_000))),
            )
        )
        create_shm = self._bool_env(os.getenv("HFT_RISK_FAST_GATE_CREATE_SHM", "0"), default=False)
        try:
            gate = FastGate(max_price=max_price_scaled, max_qty=max_qty, create_shm=create_shm, price_scale=scale)
            logger.info(
                "FastGate enabled",
                max_price_scaled=max_price_scaled,
                max_qty=max_qty,
                create_shm=create_shm,
            )
            return gate
        except (OSError, RuntimeError) as exc:  # pragma: no cover - environment dependent
            logger.warning("FastGate init failed; disabling", error=str(exc))
            return None

    def _init_rust_validator(self, price_scale_provider: PriceScaleProvider | None = None) -> Any | None:
        if not self._bool_env(os.getenv("HFT_RISK_RUST_VALIDATOR", "0"), default=False):
            return None
        cls = _load_rust_risk_validator()
        if cls is None:
            return None
        try:
            defaults = self.config.get("global_defaults", {})
            max_price_cap_raw = float(defaults.get("max_price_cap", 5000.0))  # precision-config
            all_caps = [max_price_cap_raw]
            for key in ("max_price_cap_futures", "max_price_cap_options"):
                val = defaults.get(key)
                if val is not None:
                    all_caps.append(float(val))
            max_price_cap_raw = max(all_caps)
            tick_size_raw = float(defaults.get("tick_size", 0.01))  # precision-config
            band_ticks = int(defaults.get("price_band_ticks", 20))
            max_notional_raw = defaults.get("max_notional", 10_000_000)
            # Use default scale factor (10000) for the validator
            scale = 10_000
            if price_scale_provider is not None:
                try:
                    from hft_platform.core.pricing import PriceCodec

                    codec = PriceCodec(price_scale_provider)
                    scale = int(codec.scale_factor("default")) or 10_000
                except (ImportError, TypeError, ValueError) as exc:
                    logger.warning("Failed to resolve price scale from provider, using default 10000", error=str(exc))
            max_price_cap_scaled = int(max_price_cap_raw * scale)
            tick_size_scaled = int(tick_size_raw * scale)
            max_notional_scaled = int(max_notional_raw * scale)
            rv = cls(max_price_cap_scaled, tick_size_scaled, band_ticks, max_notional_scaled)
            # Populate per-strategy configs
            for strat_id, strat_cfg in self.config.get("strategies", {}).items():
                if "price_band_ticks" in strat_cfg:
                    rv.set_band_ticks(strat_id, int(strat_cfg["price_band_ticks"]))
                if "max_notional" in strat_cfg:
                    # Per-strategy notional needs symbol info; use default symbol scale
                    rv.set_max_notional(strat_id, "*", int(strat_cfg["max_notional"]) * scale)
            logger.info("RustRiskValidator enabled", max_price_cap_scaled=max_price_cap_scaled)
            return rv
        except (OSError, RuntimeError, TypeError, ValueError) as exc:
            logger.warning("RustRiskValidator init failed; disabling", error=str(exc))
            return None

    def load_config(self) -> None:
        with open(self.config_path, "r") as f:
            self.config = yaml.safe_load(f)

    def reload_config(self) -> None:
        """Re-read strategy_limits.yaml and update validators with new thresholds."""
        try:
            old_config = self.config
            self.load_config()
            new_config = self.config

            # Log diff of key settings
            old_defaults = old_config.get("global_defaults", {})
            new_defaults = new_config.get("global_defaults", {})
            changed: dict[str, dict[str, Any]] = {}
            for key in set(old_defaults) | set(new_defaults):
                if old_defaults.get(key) != new_defaults.get(key):
                    changed[key] = {"old": old_defaults.get(key), "new": new_defaults.get(key)}

            self.on_config_reload(new_config)

            # Reload StormGuard thresholds
            if hasattr(self.storm_guard, "reload_thresholds"):
                self.storm_guard.reload_thresholds(new_config)

            logger.info(
                "Risk config reloaded via SIGHUP",
                changed_keys=list(changed.keys()),
                changes=changed,
            )
        except Exception as exc:
            logger.error("Risk config reload failed", error=str(exc))

    def on_config_reload(self, new_config: dict[str, Any]) -> None:
        """Callback invoked by ConfigWatcher when strategy_limits.yaml changes.

        Updates internal config and clears all validator caches so that
        subsequent checks pick up the new limits.
        """
        self.config = new_config
        for v in self.validators:
            # Clear per-validator caches
            for attr in list(vars(v)):
                if "cache" in attr.lower():
                    obj = getattr(v, attr, None)
                    if isinstance(obj, dict):
                        obj.clear()
            # Also update the config references on each validator
            v.config = new_config
            v.defaults = new_config.get("global_defaults", {})
            v.strat_configs = new_config.get("strategies", {})
        self._validator0 = self.validators[0] if self.validators else None
        logger.info("RiskEngine config reloaded", strategies=list(new_config.get("strategies", {}).keys()))

    async def run(self) -> None:
        self.running = True
        logger.info("RiskEngine started")

        while self.running:
            try:
                intent: OrderIntent = await self.intent_queue.get()

                # TTL expiry check — reject stale intents before evaluation
                if intent.ttl_ns > 0 and intent.timestamp_ns > 0:
                    age_ns = timebase.now_ns() - intent.timestamp_ns
                    if age_ns > intent.ttl_ns:
                        logger.warning(
                            "risk_intent_ttl_expired",
                            intent_id=intent.intent_id,
                            strategy_id=intent.strategy_id,
                            symbol=intent.symbol,
                            age_ms=age_ns / 1_000_000,
                            ttl_ms=intent.ttl_ns / 1_000_000,
                        )
                        self._emit_reject_metric(intent.strategy_id, "TTL_EXPIRED")
                        if self._rejection_sink is not None:
                            try:
                                self._rejection_sink.put_nowait(RiskFeedback(
                                    intent_id=intent.intent_id,
                                    strategy_id=intent.strategy_id,
                                    symbol=intent.symbol,
                                    reason_code="TTL_EXPIRED",
                                    timestamp_ns=timebase.now_ns(),
                                ))
                            except asyncio.QueueFull:
                                pass
                        self.intent_queue.task_done()
                        continue

                start_ns = time.perf_counter_ns()
                decision = self.evaluate(intent)
                duration = time.perf_counter_ns() - start_ns
                if self.latency:
                    self.latency.record(
                        "risk",
                        duration,
                        trace_id=intent.trace_id,
                        symbol=intent.symbol,
                        strategy_id=intent.strategy_id,
                    )

                if decision.approved:
                    cmd = self.create_command(decision.intent)
                    _is_safety_order = (
                        cmd.intent.intent_type in (IntentType.CANCEL, IntentType.FORCE_FLAT)
                        or self._is_halt_exempt(intent.strategy_id)
                    )
                    if self.storm_guard.state == StormGuardState.HALT and not _is_safety_order:
                        logger.warning(
                            "risk_engine_blocked_by_halt",
                            cmd_id=cmd.cmd_id,
                            strategy_id=intent.strategy_id,
                            symbol=intent.symbol,
                        )
                        self.metrics.risk_halt_blocked_total.inc()
                        self._emit_reject_metric(intent.strategy_id, "HALT_BLOCKED_POST_APPROVE")
                        if self._rejection_sink is not None:
                            try:
                                self._rejection_sink.put_nowait(RiskFeedback(
                                    intent_id=getattr(intent, "intent_id", 0),
                                    strategy_id=getattr(intent, "strategy_id", ""),
                                    symbol=getattr(intent, "symbol", ""),
                                    reason_code="HALT_BLOCKED_POST_APPROVE",
                                    timestamp_ns=timebase.now_ns(),
                                ))
                            except asyncio.QueueFull:
                                pass
                    else:
                        try:
                            self.order_queue.put_nowait(cmd)
                        except asyncio.QueueFull:
                            logger.error(
                                "order_queue_full_in_risk",
                                cmd_id=cmd.cmd_id,
                                strategy_id=cmd.intent.strategy_id,
                                symbol=cmd.intent.symbol,
                            )
                            self.metrics.order_queue_full_total.inc()
                            self._order_dlq.append((cmd, time.monotonic_ns()))
                            if len(self._order_dlq) > self._ORDER_DLQ_MAX:
                                self._order_dlq.popleft()
                            self.storm_guard.trigger_halt("order_queue_full")
                else:
                    logger.warning("Order Rejected by Risk", sid=intent.strategy_id, reason=decision.reason_code)
                    self._emit_reject_metric(intent.strategy_id, decision.reason_code)
                    # In real system: Feedback to strategy via side channel
                    if self._rejection_sink is not None:
                        try:
                            self._rejection_sink.put_nowait(RiskFeedback(
                                intent_id=getattr(intent, "intent_id", 0),
                                strategy_id=getattr(intent, "strategy_id", ""),
                                symbol=getattr(intent, "symbol", ""),
                                reason_code=decision.reason_code,
                                timestamp_ns=timebase.now_ns(),
                            ))
                        except asyncio.QueueFull:
                            pass

                self.intent_queue.task_done()

                # Periodic DLQ drain
                self._dlq_drain_counter += 1
                if self._dlq_drain_counter >= self._dlq_drain_interval:
                    self._dlq_drain_counter = 0
                    self._drain_order_dlq()
            except asyncio.CancelledError:
                logger.info("RiskEngine stopped")
                break
            except Exception as e:  # noqa: BLE001 — wraps external risk validators
                logger.exception("RiskEngine error", error=str(e), error_type=type(e).__name__)
                self.intent_queue.task_done()

    def _drain_order_dlq(self) -> None:
        """Drain stale-filtered DLQ entries back into order_queue."""
        if not self._order_dlq:
            return
        # During HALT, clear all pending DLQ entries — they were approved under
        # pre-HALT conditions and must not be replayed to the broker.
        if self.storm_guard.state == StormGuardState.HALT:
            cleared = len(self._order_dlq)
            self._order_dlq.clear()
            logger.warning("risk_dlq_cleared_during_halt", cleared=cleared)
            self.metrics.risk_dlq_expired_total.inc(cleared)
            return
        now_ns = time.monotonic_ns()
        ttl_ns = self._dlq_ttl_ns
        drained = 0
        expired = 0
        while self._order_dlq:
            cmd, enqueued_ns = self._order_dlq[0]
            # Re-check HALT during drain to prevent commands leaking through
            # during concurrent HALT transitions (TOCTOU defense-in-depth)
            if self.storm_guard.state == StormGuardState.HALT:
                cleared = len(self._order_dlq)
                self._order_dlq.clear()
                logger.warning("risk_dlq_cleared_during_halt_mid_drain", cleared=cleared)
                self.metrics.risk_dlq_expired_total.inc(cleared)
                break
            # Expire stale entries (DLQ TTL)
            if now_ns - enqueued_ns > ttl_ns:
                self._order_dlq.popleft()
                expired += 1
                continue
            # Expire commands whose execution deadline has passed
            if cmd.deadline_ns > 0 and now_ns > cmd.deadline_ns:
                self._order_dlq.popleft()
                expired += 1
                continue
            # Try to push back to order_queue
            try:
                self.order_queue.put_nowait(cmd)
                self._order_dlq.popleft()
                drained += 1
            except asyncio.QueueFull:
                break  # Queue still full — stop draining
        if expired > 0:
            logger.warning(
                "risk_dlq_entries_expired",
                expired=expired,
                remaining=len(self._order_dlq),
            )
            self.metrics.risk_dlq_expired_total.inc(expired)
        if drained > 0:
            self.metrics.risk_dlq_drained_total.inc(drained)

    def evaluate(self, intent: Any) -> RiskDecision:  # noqa: C901
        price = getattr(intent, "price", None)
        if isinstance(price, float):
            self._emit_trace("risk_reject", intent, {"stage": "type_check", "reason": "FLOAT_PRICE"})
            return self._reject(intent, "FLOAT_PRICE")

        if self._fast_gate is not None:
            try:
                if int(getattr(intent, "intent_type", IntentType.NEW)) != int(IntentType.CANCEL):
                    ok, code = self._fast_gate.check(int(getattr(intent, "price", 0)), int(getattr(intent, "qty", 0)))
                    if not ok:
                        reason = self._fast_gate_reason_map.get(int(code), "FASTGATE_REJECT")
                        self._emit_trace("risk_reject", intent, {"stage": "fast_gate", "reason": reason})
                        return self._reject(intent, reason)
            except (OSError, RuntimeError) as exc:
                # FastGate failure must fail-closed: reject order when risk gate errors.
                logger.error("FastGate check error — rejecting order (fail-closed)", error=str(exc))
                self._emit_trace("risk_reject", intent, {"stage": "fast_gate", "reason": "FASTGATE_ERROR"})
                return self._reject(intent, "FASTGATE_ERROR")
        # 1. StormGuard Check
        ok, reason = self.storm_guard.validate(intent)
        if not ok:
            self._emit_trace("risk_reject", intent, {"stage": "storm_guard", "reason": reason})
            return self._reject(intent, reason)

        # 2. Hard Validators — Rust fast path or Python fallback
        rv = self._rust_validator
        if rv is not None:
            try:
                v0 = self._validator0
                _get_mid = getattr(v0, "_get_mid_price", None) if v0 is not None else None
                mid_price = int(_get_mid(getattr(intent, "symbol", "")) or 0) if callable(_get_mid) else 0
                ok, code = rv.check(
                    int(getattr(intent, "intent_type", 0)),
                    int(getattr(intent, "price", 0)),
                    int(getattr(intent, "qty", 0)),
                    str(getattr(intent, "strategy_id", "")),
                    str(getattr(intent, "symbol", "")),
                    mid_price,
                )
                if not ok:
                    reason = self._rust_validator_reason_map.get(int(code), "RUST_VALIDATOR_REJECT")
                    self._emit_trace("risk_reject", intent, {"stage": "rust_validator", "reason": reason})
                    return self._reject(intent, reason)
                # Rust fast path passed — still run validators Rust doesn't cover
                for v in self._rust_uncovered_validators:
                    ok, reason = v.check(intent)
                    if not ok:
                        self._emit_trace(
                            "risk_reject",
                            intent,
                            {"stage": "validator", "reason": reason, "validator": type(v).__name__},
                        )
                        self._check_daily_loss_halt()
                        return self._reject(intent, reason)
            except (OSError, RuntimeError) as exc:
                logger.error("RustRiskValidator error — falling through to Python", error=str(exc))
                # Fall through to Python validators on error
                for v in self.validators:
                    ok, reason = v.check(intent)
                    if not ok:
                        self._emit_trace(
                            "risk_reject",
                            intent,
                            {"stage": "validator", "reason": reason, "validator": type(v).__name__},
                        )
                        self._check_daily_loss_halt()
                        return self._reject(intent, reason)
        else:
            for v in self.validators:
                ok, reason = v.check(intent)
                if not ok:
                    self._emit_trace(
                        "risk_reject",
                        intent,
                        {"stage": "validator", "reason": reason, "validator": type(v).__name__},
                    )
                    self._check_daily_loss_halt()
                    return self._reject(intent, reason)

        # Check if DailyLossLimitValidator triggered HALT after the validator loop
        self._check_daily_loss_halt()

        if self._greeks_validator is not None:
            ok, reason = self._greeks_validator.check(intent)
            if not ok:
                self._emit_trace("risk_reject", intent, {"stage": "greeks_limit", "reason": reason})
                return self._reject(intent, reason)

        self._emit_trace("risk_approve", intent, {"stage": "evaluate"})
        decision = RiskDecision(True, intent)
        self._audit_risk_decision(intent, decision)
        return decision

    def evaluate_typed_frame(self, frame: Any, *, intent_view: Any | None = None) -> RiskDecision:
        """Risk evaluation on a typed intent frame using a lightweight view object."""
        if intent_view is None:
            intent_view = self.typed_frame_view(frame)
        return self.evaluate(intent_view)  # validators only require attribute access

    def typed_frame_view(self, frame: Any) -> Any:
        try:
            from hft_platform.gateway.channel import typed_frame_to_view

            return typed_frame_to_view(frame)
        except (KeyError, TypeError, ValueError) as exc:
            # Fallback: materialize full OrderIntent if frame is malformed/unsupported
            logger.warning("typed_frame_to_view failed, falling back to full materialization", error=str(exc))
            from hft_platform.gateway.channel import typed_frame_to_intent

            return typed_frame_to_intent(frame)

    def create_command_from_typed_frame(self, frame: Any, *, intent_view: Any | None = None) -> OrderCommand:
        from hft_platform.gateway.channel import typed_frame_to_intent, typed_view_to_intent

        if intent_view is not None:
            if isinstance(intent_view, OrderIntent):
                return self.create_command(intent_view)
            try:
                return self.create_command(typed_view_to_intent(intent_view))
            except (KeyError, TypeError, ValueError) as exc:
                logger.warning("typed_view_to_intent failed, falling back to frame materialization", error=str(exc))
        return self.create_command(typed_frame_to_intent(frame))

    def create_typed_command_frame_from_typed_frame(self, frame: Any) -> tuple[Any, ...]:
        """Prototype typed command frame for OrderAdapter typed consume path."""
        cmd_id = self._next_cmd_id()
        deadline = time.monotonic_ns() + 500_000_000
        created_ns = timebase.now_ns()
        return (
            "typed_order_cmd_v1",
            int(cmd_id),
            int(deadline),
            int(self.storm_guard.state),
            int(created_ns),
            frame,
        )

    @property
    def monotonic_cmd_id(self) -> int:
        """Thread-safe access to command ID counter."""
        lock = self._cmd_id_lock
        if lock is None:
            return self._monotonic_cmd_id
        with lock:
            return self._monotonic_cmd_id

    def _next_cmd_id(self) -> int:
        """Thread-safe increment and return of command ID."""
        lock = self._cmd_id_lock
        if lock is None:
            self._monotonic_cmd_id += 1
            return self._monotonic_cmd_id
        with lock:
            self._monotonic_cmd_id += 1
            return self._monotonic_cmd_id

    def create_command(self, intent: OrderIntent) -> OrderCommand:
        cmd_id = self._next_cmd_id()
        # Set 500ms deadline from now (relaxed for Python/Docker latency)
        deadline = time.monotonic_ns() + 500_000_000

        cmd = OrderCommand(
            cmd_id=cmd_id,
            intent=intent,
            deadline_ns=deadline,
            storm_guard_state=self.storm_guard.state,
            created_ns=timebase.now_ns(),
            decision_price=intent.decision_price,
        )
        self._emit_trace("risk_command", intent, {"cmd_id": int(cmd_id), "deadline_ns": int(deadline)})
        return cmd

    def _is_halt_exempt(self, strategy_id: str) -> bool:
        """Check if a strategy is halt-exempt via StormGuard."""
        sg = self.storm_guard
        is_exempt = getattr(sg, "is_halt_exempt", None)
        if callable(is_exempt):
            return is_exempt(strategy_id)
        return strategy_id in getattr(sg, "_halt_exempt_strategies", frozenset())

    def _emit_reject_metric(self, strategy_id: str, reason: str) -> None:
        metrics = self.metrics
        if metrics is None:
            return
        try:
            self._reject_metric_counter = (self._reject_metric_counter + 1) % self._reject_metric_sample_every
            if self._reject_metric_counter != 0:
                return
            owner_id = id(metrics)
            if self._reject_metric_cache_owner_id != owner_id:
                self._reject_metric_cache.clear()
                self._reject_metric_cache_owner_id = owner_id
            key = (str(strategy_id), str(reason))
            child = self._reject_metric_cache.get(key)
            if child is None:
                child = metrics.risk_reject_total.labels(strategy=key[0], reason=key[1])
                self._reject_metric_cache[key] = child
            child.inc()
        except Exception as exc:
            logger.debug("reject_metric_emit_failed", error=str(exc))

    def _reject(self, intent: Any, reason: str) -> RiskDecision:
        """Create a rejection decision and audit it."""
        decision = RiskDecision(False, intent, reason)
        self._audit_risk_decision(intent, decision)
        return decision

    def _audit_risk_decision(self, intent: Any, decision: RiskDecision) -> None:
        """Non-blocking audit log of risk evaluation result."""
        try:
            from hft_platform.recorder.audit import get_audit_writer

            audit = get_audit_writer()
            audit.log_risk_decision(
                {
                    "strategy_id": str(getattr(intent, "strategy_id", "")),
                    "symbol": str(getattr(intent, "symbol", "")),
                    "intent_type": int(getattr(intent, "intent_type", 0)),
                    "price": int(getattr(intent, "price", 0)),
                    "qty": int(getattr(intent, "qty", 0)),
                    "approved": decision.approved,
                    "reason_code": decision.reason_code,
                }
            )
        except Exception as exc:
            logger.debug("audit_risk_decision_failed", error=str(exc))

    def _emit_trace(self, stage: str, intent: Any, payload: dict[str, Any]) -> None:
        sampler = getattr(self, "_trace_sampler", None)
        if sampler is None:
            return
        try:
            sampler.emit(
                stage=stage,
                trace_id=str(getattr(intent, "trace_id", "") or ""),
                payload={
                    "strategy_id": getattr(intent, "strategy_id", ""),
                    "symbol": getattr(intent, "symbol", ""),
                    **payload,
                },
            )
        except Exception as exc:
            logger.debug("trace_emit_failed", error=str(exc))

    def notify_fill_pnl(self, strategy_id: str, pnl_delta: int) -> None:
        """Forward realized PnL delta to the DailyLossLimitValidator."""
        for v in self.validators:
            if isinstance(v, DailyLossLimitValidator):
                v.record_pnl(strategy_id, pnl_delta)
                return

    def update_unrealized_pnl(self, unrealized_scaled: int) -> None:
        """Forward unrealized PnL to the DailyLossLimitValidator."""
        for v in self.validators:
            if isinstance(v, DailyLossLimitValidator):
                v.update_unrealized(unrealized_scaled)
                return

    def _check_daily_loss_halt(self) -> None:
        """Check if DailyLossLimitValidator has triggered a HALT; if so, escalate StormGuard.

        Non-blocking: Telegram notification is scheduled via asyncio.create_task so it
        never delays the evaluate() hot path.
        """
        if self.storm_guard.state == StormGuardState.HALT:
            return  # Already in HALT — nothing to do

        for v in self.validators:
            if isinstance(v, DailyLossLimitValidator) and v.halt_triggered:
                logger.critical(
                    "DailyLossLimit HALT triggered — escalating StormGuard to HALT",
                    accumulated_loss=v._accumulated_loss,
                    unrealized_pnl=v._unrealized_pnl,
                )
                self.storm_guard.trigger_halt("DAILY_LOSS_LIMIT_EXCEEDED")

                dispatcher = self._notification_dispatcher
                if dispatcher is not None:
                    try:
                        loop = asyncio.get_event_loop()
                        if loop.is_running():
                            total_pnl = sum(v._accumulated_loss.values()) + v._unrealized_pnl
                            limit = v._default_max_daily_loss
                            asyncio.create_task(dispatcher.notify_daily_loss(total_pnl, limit))
                            asyncio.create_task(dispatcher.notify_halt("DAILY_LOSS_LIMIT_EXCEEDED"))
                    except RuntimeError:
                        pass  # No event loop — skip notification (e.g. sync test context)
                return
