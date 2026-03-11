import asyncio
import os
import threading
import time
from typing import Any

import yaml
from structlog import get_logger

from hft_platform.contracts.strategy import IntentType, OrderCommand, OrderIntent, RiskDecision
from hft_platform.core import timebase
from hft_platform.core.pricing import PriceScaleProvider
from hft_platform.observability.latency import LatencyRecorder
from hft_platform.observability.metrics import MetricsRegistry
from hft_platform.risk.validators import MaxNotionalValidator, PriceBandValidator, StormGuardFSM

logger = get_logger("risk_engine")

# Lazy import for Rust risk validator
_RustRiskValidator = None


def _load_rust_risk_validator():
    global _RustRiskValidator
    if _RustRiskValidator is not None:
        return _RustRiskValidator
    try:
        from hft_platform.rust_core import RustRiskValidator

        _RustRiskValidator = RustRiskValidator
    except ImportError:
        try:
            from rust_core import RustRiskValidator

            _RustRiskValidator = RustRiskValidator
        except ImportError:
            _RustRiskValidator = None
    return _RustRiskValidator


def _obs_policy() -> str:
    value = str(os.getenv("HFT_RISK_OBS_POLICY", os.getenv("HFT_OBS_POLICY", ""))).strip().lower()
    if value in {"minimal", "balanced", "debug"}:
        return value
    return ""


def _get_trace_sampler():
    try:
        from hft_platform.diagnostics.trace import get_trace_sampler

        return get_trace_sampler()
    except Exception:
        return None


class RiskEngine:
    def __init__(
        self,
        config_path: str,
        intent_queue: asyncio.Queue,
        order_queue: asyncio.Queue,
        price_scale_provider: PriceScaleProvider | None = None,
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

        # Validators
        self.validators = [
            PriceBandValidator(self.config, price_scale_provider),
            MaxNotionalValidator(self.config, price_scale_provider),
        ]
        shared_scale_cache: dict[str, int] = {}
        for validator in self.validators:
            if hasattr(validator, "_shared_scale_cache"):
                validator._shared_scale_cache = shared_scale_cache
        self.storm_guard = StormGuardFSM(self.config)
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

    def _init_fast_gate(self):
        if not self._bool_env(os.getenv("HFT_RISK_FAST_GATE", "0"), default=False):
            return None
        try:
            from hft_platform.risk.fast_gate import FastGate
        except Exception as exc:  # pragma: no cover - import/jit environment dependent
            logger.warning("FastGate unavailable; disabling", error=str(exc))
            return None

        defaults = self.config.get("global_defaults", {})
        risk_cfg = self.config.get("risk", {})
        try:
            scale = int(os.getenv("HFT_RISK_FAST_GATE_PRICE_SCALE", "10000"))
        except ValueError:
            scale = 10_000
        max_price_cap = float(defaults.get("max_price_cap", 5000.0))  # precision-config
        max_price_scaled = int(max_price_cap * max(1, scale))
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
        except Exception as exc:  # pragma: no cover - environment dependent
            logger.warning("FastGate init failed; disabling", error=str(exc))
            return None

    def _init_rust_validator(self, price_scale_provider: PriceScaleProvider | None = None):
        if not self._bool_env(os.getenv("HFT_RISK_RUST_VALIDATOR", "0"), default=False):
            return None
        cls = _load_rust_risk_validator()
        if cls is None:
            return None
        try:
            defaults = self.config.get("global_defaults", {})
            max_price_cap_raw = float(defaults.get("max_price_cap", 5000.0))  # noqa: precision-config
            tick_size_raw = float(defaults.get("tick_size", 0.01))  # noqa: precision-config
            band_ticks = int(defaults.get("price_band_ticks", 20))
            max_notional_raw = defaults.get("max_notional", 10_000_000)
            # Use default scale factor (10000) for the validator
            scale = 10_000
            if price_scale_provider is not None:
                try:
                    from hft_platform.core.pricing import PriceCodec

                    codec = PriceCodec(price_scale_provider)
                    scale = int(codec.scale_factor("default")) or 10_000
                except Exception:
                    pass
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
        except Exception as exc:
            logger.warning("RustRiskValidator init failed; disabling", error=str(exc))
            return None

    def load_config(self):
        with open(self.config_path, "r") as f:
            self.config = yaml.safe_load(f)

    async def run(self):
        self.running = True
        logger.info("RiskEngine started")

        while self.running:
            try:
                intent: OrderIntent = await self.intent_queue.get()
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
                    await self.order_queue.put(cmd)
                else:
                    logger.warning("Order Rejected by Risk", sid=intent.strategy_id, reason=decision.reason_code)
                    self._emit_reject_metric(intent.strategy_id, decision.reason_code)
                    # In real system: Feedback to strategy via side channel

                self.intent_queue.task_done()
            except asyncio.CancelledError:
                logger.info("RiskEngine stopped")
                break
            except Exception as e:
                logger.exception("RiskEngine error", error=str(e), error_type=type(e).__name__)
                self.intent_queue.task_done()

    def evaluate(self, intent: Any) -> RiskDecision:
        if self._fast_gate is not None:
            try:
                if int(getattr(intent, "intent_type", IntentType.NEW)) != int(IntentType.CANCEL):
                    ok, code = self._fast_gate.check(int(getattr(intent, "price", 0)), int(getattr(intent, "qty", 0)))
                    if not ok:
                        reason = self._fast_gate_reason_map.get(int(code), "FASTGATE_REJECT")
                        self._emit_trace("risk_reject", intent, {"stage": "fast_gate", "reason": reason})
                        return RiskDecision(False, intent, reason)
            except Exception as exc:
                # FastGate failure must fail-closed: reject order when risk gate errors.
                logger.error("FastGate check error — rejecting order (fail-closed)", error=str(exc))
                self._emit_trace("risk_reject", intent, {"stage": "fast_gate", "reason": "FASTGATE_ERROR"})
                return RiskDecision(False, intent, "FASTGATE_ERROR")
        # 1. StormGuard Check
        ok, reason = self.storm_guard.validate(intent)
        if not ok:
            self._emit_trace("risk_reject", intent, {"stage": "storm_guard", "reason": reason})
            return RiskDecision(False, intent, reason)

        # 2. Hard Validators — Rust fast path or Python fallback
        rv = self._rust_validator
        if rv is not None:
            try:
                mid_price = 0
                lob = getattr(self.validators[0], "lob", None) if self.validators else None
                if lob is not None:
                    _get_mid = getattr(self.validators[0], "_get_mid_price", None)
                    if callable(_get_mid):
                        mid_price = int(_get_mid(getattr(intent, "symbol", "")) or 0)
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
                    return RiskDecision(False, intent, reason)
            except Exception as exc:
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
                        return RiskDecision(False, intent, reason)
        else:
            for v in self.validators:
                ok, reason = v.check(intent)
                if not ok:
                    self._emit_trace(
                        "risk_reject",
                        intent,
                        {"stage": "validator", "reason": reason, "validator": type(v).__name__},
                    )
                    return RiskDecision(False, intent, reason)

        self._emit_trace("risk_approve", intent, {"stage": "evaluate"})
        return RiskDecision(True, intent)

    def evaluate_typed_frame(self, frame: Any, *, intent_view: Any | None = None) -> RiskDecision:
        """Risk evaluation on a typed intent frame using a lightweight view object."""
        if intent_view is None:
            intent_view = self.typed_frame_view(frame)
        return self.evaluate(intent_view)  # validators only require attribute access

    def typed_frame_view(self, frame: Any) -> Any:
        try:
            from hft_platform.gateway.channel import typed_frame_to_view

            return typed_frame_to_view(frame)
        except Exception:
            # Fallback: materialize full OrderIntent if frame is malformed/unsupported
            from hft_platform.gateway.channel import typed_frame_to_intent

            return typed_frame_to_intent(frame)

    def create_command_from_typed_frame(self, frame: Any, *, intent_view: Any | None = None) -> OrderCommand:
        from hft_platform.gateway.channel import typed_frame_to_intent, typed_view_to_intent

        if intent_view is not None:
            if isinstance(intent_view, OrderIntent):
                return self.create_command(intent_view)
            try:
                return self.create_command(typed_view_to_intent(intent_view))
            except Exception:
                pass
        return self.create_command(typed_frame_to_intent(frame))

    def create_typed_command_frame_from_typed_frame(self, frame: Any) -> tuple[Any, ...]:
        """Prototype typed command frame for OrderAdapter typed consume path."""
        cmd_id = self._next_cmd_id()
        deadline = timebase.now_ns() + 500_000_000
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
        deadline = timebase.now_ns() + 500_000_000

        cmd = OrderCommand(
            cmd_id=cmd_id,
            intent=intent,
            deadline_ns=deadline,
            storm_guard_state=self.storm_guard.state,
            created_ns=timebase.now_ns(),
        )
        self._emit_trace("risk_command", intent, {"cmd_id": int(cmd_id), "deadline_ns": int(deadline)})
        return cmd

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
        except Exception:
            pass

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
        except Exception:
            pass
