from __future__ import annotations

import asyncio
import os
import random
import time
from dataclasses import dataclass
from typing import Any, Dict, List

from structlog import get_logger

from hft_platform.core import timebase
from hft_platform.execution.positions import PositionStore
from hft_platform.observability.metrics import MetricsRegistry
from hft_platform.ops.platform_degrade import get_shared_platform_degrade_controller
from hft_platform.risk.storm_guard import StormGuard

logger = get_logger("reconciliation")

# ---------------------------------------------------------------------------
# Environment-configurable resilience defaults (WU-04)
# ---------------------------------------------------------------------------
_DEFAULT_CHECK_INTERVAL_S = float(os.environ.get("HFT_RECON_CHECK_INTERVAL", "5"))  # precision-ok
_DEFAULT_GRACE_FAILURES = int(os.environ.get("HFT_RECON_GRACE_FAILURES", "10"))
_DEFAULT_BACKOFF_BASE = float(os.environ.get("HFT_RECON_BACKOFF_BASE", "2"))  # precision-ok
_DEFAULT_BACKOFF_MAX = float(os.environ.get("HFT_RECON_BACKOFF_MAX", "60"))  # precision-ok
_BACKOFF_JITTER = 0.2


@dataclass(slots=True)
class PositionDiscrepancy:
    """Represents a mismatch between local and broker positions."""

    symbol: str
    local_qty: int
    broker_qty: int
    diff: int
    is_futures: bool = False

    @property
    def is_critical(self) -> bool:
        """Critical if signs differ or absolute diff exceeds threshold."""
        if self.local_qty == 0 and self.broker_qty == 0:
            return False
        # Sign mismatch is always critical
        if (self.local_qty > 0 and self.broker_qty < 0) or (self.local_qty < 0 and self.broker_qty > 0):
            return True
        # Futures: any lot discrepancy is critical — stock threshold is too wide
        if self.is_futures:
            return abs(self.diff) >= 1
        # Stocks: large absolute diff is critical (threshold: 100 shares or 10% of position)
        threshold = max(100, abs(self.local_qty) // 10) if self.local_qty != 0 else 100
        return abs(self.diff) > threshold

    @property
    def severity(self) -> str:
        """Return severity label for metrics: critical, warning, or info."""
        if self.is_critical:
            return "critical"
        if abs(self.diff) > 10:
            return "warning"
        return "info"


def _compute_backoff_delay(
    attempt: int,
    base: float,  # precision-ok
    max_delay: float,  # precision-ok
    jitter: float,  # precision-ok
) -> float:  # precision-ok: timing
    """Compute exponential backoff delay with jitter.

    ``attempt`` is 0-indexed (first failure = attempt 0).
    """
    raw = min(base ** (attempt + 1), max_delay)
    jitter_factor = random.uniform(1 - jitter, 1 + jitter)
    return raw * jitter_factor


class ReconciliationService:
    def __init__(
        self,
        client: Any,
        position_store: PositionStore,
        config: dict,
        storm_guard: StormGuard,
    ) -> None:
        self.client = client
        self.store = position_store
        self.config = config
        self.storm_guard = storm_guard
        self.platform_degrade_controller = get_shared_platform_degrade_controller()

        recon_cfg = config.get("reconciliation", {})

        # WU-04: resilient defaults
        self.check_interval_s: float = recon_cfg.get(  # precision-ok
            "check_interval_s",
            _DEFAULT_CHECK_INTERVAL_S,
        )
        self.grace_failures: int = recon_cfg.get(
            "grace_failures",
            _DEFAULT_GRACE_FAILURES,
        )
        self.backoff_base: float = recon_cfg.get(  # precision-ok
            "backoff_base",
            _DEFAULT_BACKOFF_BASE,
        )
        self.backoff_max: float = recon_cfg.get(  # precision-ok
            "backoff_max",
            _DEFAULT_BACKOFF_MAX,
        )
        self.broker_zero_debounce_observations: int = int(
            recon_cfg.get(
                "broker_zero_debounce_observations",
                os.environ.get("HFT_RECON_BROKER_ZERO_DEBOUNCE_OBSERVATIONS", "2"),
            )
        )

        self.last_heartbeat: float = timebase.now_s()  # precision-ok
        self.running: bool = False
        self._last_discrepancies: List[PositionDiscrepancy] = []
        self._last_noncritical_drift_signature: dict[str, int] = {}
        self._noncritical_drift_streak: int = 0
        self._broker_zero_streak: int = 0
        self._consecutive_failures: int = 0
        self._halt_triggered: bool = False
        self._critical_drift_streak: int = 0
        self._critical_drift_debounce: int = int(os.environ.get("HFT_RECON_CRITICAL_DEBOUNCE_OBSERVATIONS", "3"))
        # Auto-correct: after N consecutive observations of the SAME persistent
        # critical drift, adopt broker state to break the drift loop.
        # Default 5 = ~25s at 5s interval (conservative: gives time for fills).
        self._auto_correct_after: int = int(
            os.environ.get("HFT_RECON_AUTO_CORRECT_AFTER", "5")
        )
        self._auto_correct_enabled: bool = (
            os.environ.get("HFT_RECON_AUTO_CORRECT_ENABLED", "1") == "1"
        )
        # Futures auto-correct threshold: only auto-correct if abs(diff) <= this
        self._auto_correct_futures_max_qty: int = int(
            os.environ.get("HFT_RECON_AUTO_CORRECT_FUTURES_MAX_QTY", "2")
        )
        # Stock auto-correct threshold
        self._auto_correct_stock_max_qty: int = int(
            os.environ.get("HFT_RECON_AUTO_CORRECT_STOCK_MAX_QTY", "10")
        )
        # Platform-managed symbols: derived dynamically from client at sync time.
        # Non-platform symbols (e.g. manually traded via broker app) get relaxed
        # treatment: phantom positions are auto-resolved without triggering HALT.

    @property
    def drift_streak(self) -> int:
        """Number of consecutive non-critical drift observations (read-only)."""
        return self._noncritical_drift_streak

    # ------------------------------------------------------------------
    # Metrics helpers (WU-18)
    # ------------------------------------------------------------------

    @staticmethod
    def _metrics() -> MetricsRegistry:
        return MetricsRegistry.get()

    def _record_sync_result(self, result: str) -> None:
        self._metrics().reconciliation_sync_total.labels(result=result).inc()

    def _record_sync_duration(self, duration_s: float) -> None:  # precision-ok
        self._metrics().reconciliation_sync_duration_seconds.observe(duration_s)

    def _record_discrepancy(self, severity: str) -> None:
        self._metrics().reconciliation_discrepancy_total.labels(severity=severity).inc()

    def _update_failure_gauge(self) -> None:
        self._metrics().reconciliation_consecutive_failures.set(self._consecutive_failures)

    def _update_last_success_ts(self) -> None:
        self._metrics().reconciliation_last_success_ts.set(timebase.now_ns() / 1e9)

    # ------------------------------------------------------------------
    # Main loop
    # ------------------------------------------------------------------

    async def run(self) -> None:
        self.running = True
        logger.info("ReconciliationService started")

        # 1. Startup Sync — protected by same grace/backoff as runtime syncs.
        # If broker is slow to respond after login, the first query can fail.
        try:
            await self.sync_portfolio()
        except Exception as e:
            self._consecutive_failures += 1
            self._update_failure_gauge()
            logger.error(
                "Startup reconciliation failed — will retry in periodic loop",
                error=str(e),
                consecutive_failures=self._consecutive_failures,
            )

        while self.running:
            await asyncio.sleep(self.check_interval_s)

            # 2. Runtime Check - periodic reconciliation
            try:
                await self.sync_portfolio()
                # Reset failure counter on success (WU-04).
                # Note: _halt_triggered is reset only when no discrepancies
                # are found (inside sync_portfolio), not here — resetting
                # unconditionally caused re-triggers every 5s.
                self._consecutive_failures = 0
                self._update_failure_gauge()
            except Exception as e:
                self._consecutive_failures += 1
                self._update_failure_gauge()
                remaining = self.grace_failures - self._consecutive_failures

                logger.error(
                    "Runtime reconciliation failed",
                    error=str(e),
                    consecutive_failures=self._consecutive_failures,
                    grace_failures=self.grace_failures,
                    remaining_before_halt=max(remaining, 0),
                )

                if self._consecutive_failures >= self.grace_failures and not self._halt_triggered:
                    reason = f"RECONCILIATION_UNAVAILABLE: {self._consecutive_failures} consecutive failures"
                    self._halt_triggered = True
                    logger.critical(
                        "Triggering HALT due to reconciliation unavailability",
                        consecutive_failures=self._consecutive_failures,
                    )
                    if self.storm_guard:
                        self.storm_guard.trigger_halt(reason)
                    else:
                        logger.error("No StormGuard configured - HALT not triggered (manual intervention required)")
                else:
                    # Exponential backoff before next retry (WU-04)
                    delay = _compute_backoff_delay(
                        attempt=self._consecutive_failures - 1,
                        base=self.backoff_base,
                        max_delay=self.backoff_max,
                        jitter=_BACKOFF_JITTER,
                    )
                    logger.warning(
                        "Reconciliation failure countdown",
                        failure=self._consecutive_failures,
                        grace_failures=self.grace_failures,
                        next_retry_seconds=round(delay, 2),
                    )
                    await asyncio.sleep(delay)

    async def sync_portfolio(self) -> None:  # noqa: C901
        logger.info("Starting Portfolio Sync...")
        t0 = time.monotonic()
        try:
            # 1. Fetch positions from broker
            raw_positions = await asyncio.to_thread(self.client.get_positions)

            # None means the query itself failed — treat as an unhealthy sync cycle.
            # Do NOT build broker_map from None; that would silently mask real positions.
            if raw_positions is None:
                raise RuntimeError("get_positions() returned None — broker query unhealthy")

            # 2. Build broker position map {symbol: qty}
            broker_map: Dict[str, int] = {}
            for pos in raw_positions:
                code = getattr(pos, "code", None) or (pos.get("code") if isinstance(pos, dict) else None)
                qty = getattr(pos, "quantity", None) or (pos.get("quantity", 0) if isinstance(pos, dict) else 0)
                direction = getattr(pos, "direction", "")
                # Shioaji futures positions use "Short"/"Long"; stock positions use
                # "Action.Sell"/"Action.Buy". Accept both forms.
                if str(direction) in ("Action.Sell", "Short"):
                    qty = -qty
                if code:
                    # Accumulate (not overwrite) to handle multiple account types
                    # (stock + futopt) returning the same symbol code.
                    broker_map[code] = broker_map.get(code, 0) + int(qty)

            logger.info("Portfolio Sync: Broker State", positions=broker_map)

            # 3. Build local position map {symbol: qty}
            # Also build per-strategy breakdown for drift attribution (M9)
            # Use snapshot_positions() to get a consistent copy under _fill_lock,
            # preventing "dictionary changed size during iteration" from concurrent fills.
            local_map: Dict[str, int] = {}
            per_strategy_map: Dict[str, Dict[str, int]] = {}  # strategy_id -> {symbol: qty}
            snapshot = (
                self.store.snapshot_positions()
                if hasattr(self.store, "snapshot_positions")
                else dict(self.store.positions)
            )
            for key, pos in snapshot.items():
                symbol = pos.symbol
                local_map[symbol] = local_map.get(symbol, 0) + pos.net_qty
                strat = pos.strategy_id
                strat_positions = per_strategy_map.setdefault(strat, {})
                strat_positions[symbol] = strat_positions.get(symbol, 0) + pos.net_qty

            recovery = getattr(self.store, "_recovery_positions", None)
            if recovery:
                for rkey, rdata in recovery.items():
                    if not isinstance(rdata, dict):
                        continue
                    qty = int(rdata.get("net_qty", 0) or 0)
                    if qty == 0:
                        continue
                    symbol = str(rdata.get("symbol") or rkey.rsplit(":", 1)[-1])
                    local_map[symbol] = local_map.get(symbol, 0) + qty
                    strat = str(rdata.get("strategy_id") or "*")
                    strat_positions = per_strategy_map.setdefault(strat, {})
                    strat_positions[symbol] = strat_positions.get(symbol, 0) + qty

            # Log per-strategy breakdown at INFO level (M9)
            logger.info(
                "Portfolio Sync: Per-strategy position breakdown",
                strategies=list(per_strategy_map.keys()),
                per_strategy=per_strategy_map,
            )

            logger.info("Portfolio Sync: Local State", positions=local_map)
            self.platform_degrade_controller.update_reference_positions(local_map=local_map, broker_map=broker_map)

            broker_has_positions = any(int(qty) != 0 for qty in broker_map.values())
            local_has_positions = any(int(qty) != 0 for qty in local_map.values())
            if local_has_positions and not broker_has_positions:
                self._broker_zero_streak += 1
                if self._broker_zero_streak < self.broker_zero_debounce_observations:
                    self._last_discrepancies = []
                    self._last_noncritical_drift_signature = {}
                    self._noncritical_drift_streak = 0
                    duration = time.monotonic() - t0
                    self._record_sync_duration(duration)
                    self._record_sync_result("success")
                    self._update_last_success_ts()
                    logger.warning(
                        "broker_zero_snapshot_debounced",
                        consecutive_observations=self._broker_zero_streak,
                        required_observations=self.broker_zero_debounce_observations,
                        local_positions=local_map,
                    )
                    return
            else:
                self._broker_zero_streak = 0

            # 4. Compute discrepancies
            discrepancies = self._compute_discrepancies(local_map, broker_map)

            # 4b. Auto-resolve non-platform symbol phantoms immediately.
            # Symbols not in the client's subscribed set were placed externally
            # (e.g. manual broker app trade).  If broker reports 0 for such a
            # symbol, clear the phantom position without entering the HALT path.
            platform_codes = self._get_platform_symbols()
            if platform_codes:
                resolved: list[PositionDiscrepancy] = []
                kept: list[PositionDiscrepancy] = []
                for d in discrepancies:
                    if d.symbol not in platform_codes and d.broker_qty == 0 and d.local_qty != 0:
                        self.store.clear_symbol_positions(d.symbol)
                        resolved.append(d)
                    else:
                        kept.append(d)
                if resolved:
                    logger.warning(
                        "non_platform_phantom_auto_resolved",
                        resolved=[
                            {"symbol": d.symbol, "local_qty": d.local_qty}
                            for d in resolved
                        ],
                    )
                    try:
                        for d in resolved:
                            self._metrics().reconciliation_auto_corrected_total.labels(
                                symbol=d.symbol,
                            ).inc()
                    except Exception:
                        pass
                discrepancies = kept

            self._last_discrepancies = discrepancies

            # 5. Update reconciliation discrepancy metric (legacy)
            self._metrics().reconciliation_discrepancy_count.set(len(discrepancies))

            # 6. Record per-severity discrepancy metrics (WU-18)
            for d in discrepancies:
                self._record_discrepancy(d.severity)

            # 6b. Emit per-symbol position drift gauge (M5)
            _drift_gauge = getattr(self._metrics(), "position_drift_qty", None)
            if _drift_gauge is not None:
                _m = self._metrics()
                for d in discrepancies:
                    _capped = _m.cap_symbol(d.symbol) if _m else d.symbol
                    _drift_gauge.labels(strategy="", symbol=_capped).set(abs(d.diff))

            # 7. Duration + success metrics
            duration = time.monotonic() - t0
            self._record_sync_duration(duration)
            self._record_sync_result("success")
            self._update_last_success_ts()

            if discrepancies:
                logger.warning(
                    "Position discrepancies detected",
                    count=len(discrepancies),
                    discrepancies=[
                        {"symbol": d.symbol, "local": d.local_qty, "broker": d.broker_qty, "diff": d.diff}
                        for d in discrepancies
                    ],
                )

                # Log which strategies contribute to each drifting symbol (M9)
                drifting_symbols = {d.symbol for d in discrepancies}
                strategy_drift_attribution: Dict[str, Dict[str, int]] = {}
                for symbol in drifting_symbols:
                    for strat, strat_positions in per_strategy_map.items():
                        qty = strat_positions.get(symbol, 0)
                        if qty != 0:
                            strategy_drift_attribution.setdefault(symbol, {})[strat] = qty
                logger.warning(
                    "Per-strategy drift attribution",
                    drifting_symbols=sorted(drifting_symbols),
                    attribution=strategy_drift_attribution,
                )

                # 8. Check for critical discrepancies and trigger HALT if needed
                critical = [d for d in discrepancies if d.is_critical]
                if critical:
                    self._last_noncritical_drift_signature = {}
                    self._noncritical_drift_streak = 0
                    self._critical_drift_streak += 1
                    if self._halt_triggered:
                        # Already in HALT — do not re-trigger (prevents
                        # resetting StormGuard's de-escalation counters).
                        logger.warning(
                            "critical_drift_persists_during_halt",
                            streak=self._critical_drift_streak,
                            symbols=[d.symbol for d in critical],
                        )
                        # Auto-correct: after sufficient consecutive observations
                        # of the same persistent drift, adopt broker state.
                        if (
                            self._auto_correct_enabled
                            and self._critical_drift_streak >= self._auto_correct_after
                        ):
                            correctable = self._filter_auto_correctable(critical)
                            if correctable:
                                await self._auto_correct_drift(correctable, broker_map)
                    elif self._critical_drift_streak >= self._critical_drift_debounce:
                        self._halt_triggered = True
                        await self._trigger_halt(critical)
                    else:
                        logger.warning(
                            "critical_drift_debounce",
                            streak=self._critical_drift_streak,
                            required=self._critical_drift_debounce,
                            symbols=[d.symbol for d in critical],
                        )
                else:
                    self._critical_drift_streak = 0
                    signature = self._noncritical_drift_signature_for(discrepancies)
                    persists_or_grows = self._noncritical_drift_persists_or_grows(signature)
                    self._noncritical_drift_streak = self._noncritical_drift_streak + 1 if persists_or_grows else 1
                    self._last_noncritical_drift_signature = signature
                    logger.warning(
                        "Non-critical reconciliation drift observed",
                        consecutive_observations=self._noncritical_drift_streak,
                        persists_or_grows=persists_or_grows,
                    )
                    if persists_or_grows and self._noncritical_drift_streak >= 2:
                        self.platform_degrade_controller.enter_reduce_only(reason="reconciliation_drift")
            else:
                self._last_noncritical_drift_signature = {}
                self._noncritical_drift_streak = 0
                self._critical_drift_streak = 0
                if self._halt_triggered:
                    # Drift resolved — release reconciliation hold so
                    # StormGuard can de-escalate from HALT.
                    self.storm_guard.set_reconciliation_hold(False)
                self._halt_triggered = False
                logger.info("Portfolio Sync Complete - No discrepancies", count=len(broker_map))

        except Exception as e:
            self._last_noncritical_drift_signature = {}
            self._noncritical_drift_streak = 0
            duration = time.monotonic() - t0
            self._record_sync_duration(duration)
            self._record_sync_result("failure")
            logger.error("Portfolio Sync Failed", error=str(e), exc_info=True)
            raise

    @staticmethod
    def _noncritical_drift_signature_for(discrepancies: List[PositionDiscrepancy]) -> dict[str, int]:
        return {d.symbol: abs(int(d.diff)) for d in discrepancies if not d.is_critical}

    def _noncritical_drift_persists_or_grows(self, current_signature: dict[str, int]) -> bool:
        previous_signature = self._last_noncritical_drift_signature
        if not previous_signature or not current_signature:
            return False
        overlapping_symbols = set(previous_signature) & set(current_signature)
        if not overlapping_symbols:
            return False
        # True if ANY overlapping symbol persisted or grew; false only if ALL shrank
        return any(current_signature[s] >= previous_signature[s] for s in overlapping_symbols)

    def _get_platform_symbols(self) -> set[str]:
        """Return the set of symbols the platform is actively managing.

        Combines the client's subscribed codes with alias→actual mappings.
        Returns empty set if unavailable (disables the non-platform filter).
        """
        codes: set[str] = set()
        sub = getattr(self.client, "subscribed_codes", None)
        if sub:
            codes |= set(sub)
        alias_map = getattr(self.client, "alias_to_actual", None)
        if alias_map:
            codes |= set(alias_map.keys())
            codes |= set(alias_map.values())
        # Also include config-defined symbols as fallback
        for s in self.config.get("symbols", []):
            if isinstance(s, dict):
                code = s.get("code", "")
                if code:
                    codes.add(code)
            elif isinstance(s, str):
                codes.add(s)
        return codes

    @staticmethod
    def _is_futures(symbol: str) -> bool:
        """Heuristic: futures symbols contain common TAIFEX prefixes."""
        return any(c in symbol.upper() for c in ("FD", "FX", "TX", "MX", "TE", "TF"))

    def _compute_discrepancies(
        self, local_map: Dict[str, int], broker_map: Dict[str, int]
    ) -> List[PositionDiscrepancy]:
        """Compare local and broker positions, return list of discrepancies.

        NOTE: startup_recon.py calls this as ReconciliationService._compute_discrepancies(None, ...)
        so this method must not use `self` — use the class-level static method directly.
        """
        discrepancies: List[PositionDiscrepancy] = []
        all_symbols = set(local_map.keys()) | set(broker_map.keys())

        for symbol in all_symbols:
            local_qty = local_map.get(symbol, 0)
            broker_qty = broker_map.get(symbol, 0)
            diff = local_qty - broker_qty

            if diff != 0:
                discrepancies.append(
                    PositionDiscrepancy(
                        symbol=symbol,
                        local_qty=local_qty,
                        broker_qty=broker_qty,
                        diff=diff,
                        is_futures=ReconciliationService._is_futures(symbol),
                    )
                )

        return discrepancies

    def _filter_auto_correctable(
        self, discrepancies: List[PositionDiscrepancy]
    ) -> List[PositionDiscrepancy]:
        """Filter discrepancies eligible for auto-correction.

        Two directions are supported:
        - local=0, broker>0: orphaned phantom fill — adopt broker position.
        - local>0, broker=0: expired/cleared position (e.g. option expiry,
          manual broker-side close) — clear phantom local position.

        Large or sign-mismatch discrepancies require manual intervention.
        """
        result: List[PositionDiscrepancy] = []
        for d in discrepancies:
            max_qty = (
                self._auto_correct_futures_max_qty
                if d.is_futures
                else self._auto_correct_stock_max_qty
            )
            # Direction 1: local=0, broker>0 (orphaned phantom fill)
            if d.local_qty == 0 and abs(d.broker_qty) <= max_qty:
                result.append(d)
                continue
            # Direction 2: local>0, broker=0 (expired/cleared position)
            if d.broker_qty == 0 and abs(d.local_qty) <= max_qty:
                result.append(d)
                continue
        return result

    async def _auto_correct_drift(
        self,
        correctable: List[PositionDiscrepancy],
        broker_map: Dict[str, int],
    ) -> None:
        """Adopt broker positions for auto-correctable drift.

        Two directions:
        - broker_qty > 0: load broker position into PositionStore via load_recovery.
        - broker_qty == 0: clear phantom local position via clear_symbol_positions
          (e.g. expired option, manual broker-side close).
        """
        for d in correctable:
            logger.critical(
                "auto_correct_drift: adopting broker position",
                symbol=d.symbol,
                local_qty=d.local_qty,
                broker_qty=d.broker_qty,
                streak=self._critical_drift_streak,
            )
            if d.broker_qty == 0:
                # Broker cleared position (expiry, manual close, etc.)
                # Remove all local entries for this symbol.
                self.store.clear_symbol_positions(d.symbol)
            else:
                # Use load_recovery to inject broker position into PositionStore
                # with strategy_id="*" (unknown ownership — matches startup_recon pattern).
                self.store.load_recovery(
                    account_id="default",
                    symbol=d.symbol,
                    net_qty=d.broker_qty,
                    avg_price_scaled=-1,  # sentinel: unknown cost basis
                    realized_pnl_scaled=0,
                    fees_scaled=0,
                    strategy_id="*",
                )
            try:
                self._metrics().reconciliation_auto_corrected_total.labels(
                    symbol=d.symbol
                ).inc()
            except Exception:
                pass

        # Reset drift state — next sync will verify correction took effect
        self._critical_drift_streak = 0
        self._halt_triggered = False
        self.storm_guard.set_reconciliation_hold(False)
        logger.warning(
            "auto_correct_drift: complete, reconciliation hold released",
            corrected_symbols=[d.symbol for d in correctable],
        )

    async def _trigger_halt(self, critical_discrepancies: List[PositionDiscrepancy]) -> None:
        """Trigger StormGuard HALT due to reconciliation mismatch."""
        symbols = [d.symbol for d in critical_discrepancies]
        reason = f"RECONCILIATION_MISMATCH: {len(critical_discrepancies)} critical discrepancies ({symbols[:3]})"

        logger.critical(
            "Triggering HALT due to reconciliation mismatch",
            critical_count=len(critical_discrepancies),
            symbols=symbols,
        )

        # Set reconciliation hold BEFORE triggering HALT so that the
        # HALT cannot auto-recover via StormGuard.update() while drift persists.
        self.storm_guard.set_reconciliation_hold(True)
        self.storm_guard.trigger_halt(reason)
