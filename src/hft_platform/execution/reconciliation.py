from __future__ import annotations

import asyncio
import os
import random
import time
from dataclasses import dataclass
from typing import Any, Dict, List

from structlog import get_logger

from hft_platform.contracts.constants import MANUAL_STRATEGY_ID
from hft_platform.core import timebase
from hft_platform.core.symbol_classifier import is_futures_symbol
from hft_platform.execution.positions import PositionStore
from hft_platform.observability.metrics import MetricsRegistry
from hft_platform.ops.platform_degrade import get_shared_platform_degrade_controller
from hft_platform.risk import manual_latches
from hft_platform.risk.storm_guard import StormGuard

logger = get_logger("reconciliation")

_DEFAULT_CHECK_INTERVAL_S = float(os.environ.get("HFT_RECON_CHECK_INTERVAL", "5"))
_DEFAULT_GRACE_FAILURES = int(os.environ.get("HFT_RECON_GRACE_FAILURES", "10"))
_DEFAULT_BACKOFF_BASE = float(os.environ.get("HFT_RECON_BACKOFF_BASE", "2"))
_DEFAULT_BACKOFF_MAX = float(os.environ.get("HFT_RECON_BACKOFF_MAX", "60"))
_BACKOFF_JITTER = 0.2


def _extract_positions_error(client: Any) -> str | None:
    """Best-effort extraction of the underlying broker error captured by the
    account gateway. Used to enrich the recon RuntimeError so logs surface the
    actual broker-side reason (e.g. ``stock: 500 Please check param.``) instead
    of the generic "returned None" message that hid the cause for months.

    Only string values are accepted to avoid leaking ``MagicMock`` placeholders
    from tests or unrelated attribute namespaces.
    """
    gw = getattr(client, "account_gateway", None)
    if gw is not None:
        detail = getattr(gw, "last_positions_error", None)
        if isinstance(detail, str) and detail:
            return detail
    direct = getattr(client, "last_positions_error", None)
    if isinstance(direct, str) and direct:
        return direct
    return None


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
        if (self.local_qty > 0 and self.broker_qty < 0) or (self.local_qty < 0 and self.broker_qty > 0):
            return True
        if self.is_futures:
            return abs(self.diff) >= 1
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
    base: float,
    max_delay: float,
    jitter: float,
) -> float:
    """Compute exponential backoff delay with jitter.

    ``attempt`` is 0-indexed (first failure = attempt 0).
    """
    raw = min(base ** (attempt + 1), max_delay)
    jitter_factor = random.uniform(1 - jitter, 1 + jitter)
    return raw * jitter_factor


_NON_COMPARABLE_ORDER_MODES = frozenset({"sim", "simulation", "paper"})

_UNOWNED_STRATEGY_IDS = frozenset({"*", MANUAL_STRATEGY_ID, "UNKNOWN"})


class ReconciliationService:
    def __init__(
        self,
        client: Any,
        position_store: PositionStore,
        config: dict,
        storm_guard: StormGuard,
        *,
        symbol_source: Any = None,
        order_mode: str | None = None,
    ) -> None:
        self.client = client
        self.store = position_store
        self.config = config
        self._symbol_source = symbol_source if symbol_source is not None else client
        self._order_mode = (order_mode or os.environ.get("HFT_ORDER_MODE", "")).strip().lower()
        self.storm_guard = storm_guard
        self.platform_degrade_controller = get_shared_platform_degrade_controller()

        recon_cfg = config.get("reconciliation", {})

        self.check_interval_s: float = recon_cfg.get(
            "check_interval_s",
            _DEFAULT_CHECK_INTERVAL_S,
        )
        self.grace_failures: int = recon_cfg.get(
            "grace_failures",
            _DEFAULT_GRACE_FAILURES,
        )
        self.backoff_base: float = recon_cfg.get(
            "backoff_base",
            _DEFAULT_BACKOFF_BASE,
        )
        self.backoff_max: float = recon_cfg.get(
            "backoff_max",
            _DEFAULT_BACKOFF_MAX,
        )
        self.broker_zero_debounce_observations: int = int(
            recon_cfg.get(
                "broker_zero_debounce_observations",
                os.environ.get("HFT_RECON_BROKER_ZERO_DEBOUNCE_OBSERVATIONS", "2"),
            )
        )
        self.untrusted_sample_degrade_after: int = int(
            recon_cfg.get(
                "untrusted_sample_degrade_after",
                os.environ.get("HFT_RECON_UNTRUSTED_SAMPLE_DEGRADE_AFTER", "3"),
            )
        )

        self.sync_log_heartbeat_s: float = float(
            recon_cfg.get(
                "sync_log_heartbeat_s",
                os.environ.get("HFT_RECON_SYNC_LOG_HEARTBEAT_S", "300"),
            )
        )
        self._last_sync_log_signature: tuple[tuple[tuple[str, int], ...], ...] | None = None
        self._last_sync_log_ts: float = 0.0
        self._suppressed_sync_logs: int = 0

        self.last_heartbeat: float = timebase.now_s()
        self.running: bool = False
        self._last_discrepancies: List[PositionDiscrepancy] = []
        self._last_noncritical_drift_signature: dict[str, int] = {}
        self._noncritical_drift_streak: int = 0
        self._drift_reduce_only: bool = False
        self._untrusted_sample_streak: int = 0
        self._clean_sync_streak: int = 0
        self._verified_mono_ns: int = 0
        self._latch_release_after: int = max(1, int(os.environ.get("HFT_RECON_LATCH_RELEASE_AFTER_CLEAN", "2")))
        self._unverifiable_reduce_only: bool = False
        self._broker_zero_streak: int = 0
        self._consecutive_failures: int = 0
        self._halt_triggered: bool = False
        self._critical_drift_streak: int = 0
        self._critical_drift_debounce: int = int(os.environ.get("HFT_RECON_CRITICAL_DEBOUNCE_OBSERVATIONS", "3"))
        self._auto_correct_after: int = int(os.environ.get("HFT_RECON_AUTO_CORRECT_AFTER", "5"))
        self._auto_correct_enabled: bool = os.environ.get("HFT_RECON_AUTO_CORRECT_ENABLED", "1") == "1"
        self._auto_correct_futures_max_qty: int = int(os.environ.get("HFT_RECON_AUTO_CORRECT_FUTURES_MAX_QTY", "2"))
        self._auto_correct_stock_max_qty: int = int(os.environ.get("HFT_RECON_AUTO_CORRECT_STOCK_MAX_QTY", "10"))

    @property
    def positions_verified_mono_ns(self) -> int:
        """``time.monotonic_ns()`` of the last sync that confirmed every position; 0 if the last one did not."""
        return self._verified_mono_ns

    def _not_verified(self) -> None:
        self._clean_sync_streak = 0
        self._verified_mono_ns = 0

    async def _verified(self) -> None:
        """A trusted sync found the books equal: count it, and end a mismatch latch once due."""
        self._clean_sync_streak += 1
        self._verified_mono_ns = time.monotonic_ns()
        if self._clean_sync_streak < self._latch_release_after:
            return
        streak = self._clean_sync_streak
        try:
            released = await asyncio.to_thread(
                manual_latches.release_when,
                manual_latches.RECONCILIATION_MISMATCH,
                lambda _record: True,
                reason=f"{streak} consecutive trusted reconciliations found no discrepancy",
                actor="engine:reconciliation",
            )
        except Exception as exc:
            logger.error("reconciliation_latch_release_failed", error=str(exc))
            return
        if released:
            logger.warning("reconciliation_latch_released", clean_syncs=streak)

    @property
    def drift_streak(self) -> int:
        """Number of consecutive non-critical drift observations (read-only)."""
        return self._noncritical_drift_streak

    @property
    def reconciliation_unverifiable_active(self) -> bool:
        """Whether reconciliation has been unable to take a trustworthy sample.

        Distinct from ``drift_reduce_only_active`` on purpose: "the books
        disagree" and "I cannot read the books" need different operator
        responses, and collapsing them would hide a broker-side outage inside a
        position-drift alert. Level-triggered for the same reason as its
        sibling -- see the note below.
        """
        return self._unverifiable_reduce_only

    @property
    def drift_reduce_only_active(self) -> bool:
        """Whether position drift is currently asserting platform reduce-only.

        Level-triggered on purpose. ``reconciliation_drift`` is listed in
        ``_AUTO_RECOVERABLE_REASONS``, and ``check_auto_recovery`` drops any
        auto-recoverable reason the live inputs no longer report -- so a reason
        that was never reported by an input at all was dropped on the FIRST
        supervisor tick after it latched, about a second later, and reduce-only
        exited ~60 s after that with the drift unresolved. Absence of a signal
        was being read as "the condition cleared".
        """
        return self._drift_reduce_only

    @staticmethod
    def _metrics() -> MetricsRegistry:
        return MetricsRegistry.get()

    def _record_sync_result(self, result: str) -> None:
        self._metrics().reconciliation_sync_total.labels(result=result).inc()

    def _set_not_comparable(self, active: bool) -> None:
        gauge = getattr(self._metrics(), "reconciliation_not_comparable", None)
        if gauge is not None:
            gauge.set(1 if active else 0)

    def _record_sync_duration(self, duration_s: float) -> None:
        self._metrics().reconciliation_sync_duration_seconds.observe(duration_s)

    def _set_untrusted_sample_streak(self, value: int) -> None:
        try:
            gauge = getattr(self._metrics(), "reconciliation_untrusted_sample_streak", None)
            if gauge is not None:
                gauge.set(value)
        except Exception:
            pass

    def _record_discrepancy(self, severity: str) -> None:
        self._metrics().reconciliation_discrepancy_total.labels(severity=severity).inc()

    def _update_failure_gauge(self) -> None:
        self._metrics().reconciliation_consecutive_failures.set(self._consecutive_failures)

    def _sync_log_due(self, broker_map: Dict[str, int], local_map: Dict[str, int]) -> bool:
        """Whether this cycle's clean-sync lines are worth writing.

        True whenever the observed state differs from the last one logged, and
        at least once per ``sync_log_heartbeat_s`` regardless — a quiet log has
        to stay distinguishable from a stopped loop. Cycles that are skipped are
        counted and reported on the next line that does get written.
        """
        signature = (
            tuple(sorted(broker_map.items())),
            tuple(sorted(local_map.items())),
        )
        now = time.monotonic()
        if signature != self._last_sync_log_signature or (now - self._last_sync_log_ts) >= self.sync_log_heartbeat_s:
            self._last_sync_log_signature = signature
            self._last_sync_log_ts = now
            return True
        self._suppressed_sync_logs += 1
        return False

    def _take_suppressed_sync_logs(self) -> int:
        """Read and clear the collapsed-cycle count for the line about to say it."""
        count = self._suppressed_sync_logs
        self._suppressed_sync_logs = 0
        return count

    def _update_last_success_ts(self) -> None:
        self._metrics().reconciliation_last_success_ts.set(timebase.now_ns() / 1e9)

    def _in_trading_hours(self) -> bool:
        """Bug #38: TAIFEX-aware check used to suppress HALT escalation when
        the market is closed. Outside trading hours ``get_positions()`` will
        often return None / fail; before this gate that drove the failure
        counter past ``grace_failures`` and tripped HALT every overnight
        cycle. Defaults to True if the calendar can't be loaded so we keep
        the legacy behaviour rather than silently disabling escalation.
        """
        if os.environ.get("HFT_RECON_OFF_SESSION_GUARD", "1") not in {"1", "true", "yes", "on"}:
            return True
        try:
            from hft_platform.core.market_calendar import get_calendar

            return get_calendar().is_trading_hours(product_type="future")
        except Exception as exc:  # pragma: no cover — fail-safe path
            logger.debug("reconciliation_calendar_lookup_failed", error=str(exc))
            return True

    async def run(self) -> None:
        self.running = True
        logger.info("ReconciliationService started")

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

            try:
                await self.sync_portfolio()
                self._consecutive_failures = 0
                self._update_failure_gauge()
            except Exception as e:
                if not self._in_trading_hours():
                    logger.warning(
                        "reconciliation_skipped_off_session",
                        error=str(e),
                        consecutive_failures=self._consecutive_failures,
                    )
                    continue

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
        t0 = time.monotonic()
        try:
            fill_gen_before = getattr(self.store, "fill_generation", None)

            raw_positions = await asyncio.to_thread(self.client.get_positions)

            if raw_positions is None:
                broker_detail = _extract_positions_error(self.client)
                msg = "get_positions() returned None — broker query unhealthy"
                if broker_detail:
                    msg = f"{msg}: {broker_detail}"
                raise RuntimeError(msg)

            broker_map = self._build_broker_map(raw_positions)

            local_map: Dict[str, int] = {}
            per_strategy_map: Dict[str, Dict[str, int]] = {}
            snapshot, recovery = self._local_snapshot()
            fill_gen_after = getattr(self.store, "fill_generation", None)

            untrusted: str | None = None
            if fill_gen_before != fill_gen_after:
                untrusted = "straddled_fill"
            elif _extract_positions_error(self.client):
                untrusted = "partial_broker_snapshot"

            for key, pos in snapshot.items():
                symbol = pos.symbol
                local_map[symbol] = local_map.get(symbol, 0) + pos.net_qty
                strat = pos.strategy_id
                strat_positions = per_strategy_map.setdefault(strat, {})
                strat_positions[symbol] = strat_positions.get(symbol, 0) + pos.net_qty
            live_keys = {
                (
                    str(getattr(pos, "account_id", "") or ""),
                    str(getattr(pos, "strategy_id", "") or ""),
                    str(getattr(pos, "symbol", "") or ""),
                )
                for pos in snapshot.values()
            }
            live_account_symbols = {(acct, symbol) for acct, _strat, symbol in live_keys if acct and symbol}

            if recovery:
                for rkey, rdata in recovery.items():
                    if not isinstance(rdata, dict):
                        continue
                    qty = int(rdata.get("net_qty", 0) or 0)
                    if qty == 0:
                        continue
                    account_id = str(rdata.get("account_id") or rkey.split(":", 1)[0])
                    symbol = str(rdata.get("symbol") or rkey.rsplit(":", 1)[-1])
                    strategy_id = str(rdata.get("strategy_id") or "")
                    if strategy_id:
                        if (account_id, strategy_id, symbol) in live_keys:
                            logger.debug(
                                "reconciliation_skip_stale_recovery_exact_duplicate",
                                account_id=account_id,
                                strategy_id=strategy_id,
                                symbol=symbol,
                            )
                            continue
                    elif (account_id, symbol) in live_account_symbols:
                        logger.debug(
                            "reconciliation_skip_stale_recovery_legacy_duplicate",
                            account_id=account_id,
                            symbol=symbol,
                        )
                        continue
                    local_map[symbol] = local_map.get(symbol, 0) + qty
                    strat = strategy_id or "*"
                    strat_positions = per_strategy_map.setdefault(strat, {})
                    strat_positions[symbol] = strat_positions.get(symbol, 0) + qty

            sync_log_due = self._sync_log_due(broker_map, local_map)
            if sync_log_due:
                logger.debug("Portfolio Sync: Broker State", positions=broker_map)
                logger.debug(
                    "Portfolio Sync: Per-strategy position breakdown",
                    strategies=list(per_strategy_map.keys()),
                    per_strategy=per_strategy_map,
                )
                logger.debug("Portfolio Sync: Local State", positions=local_map)
            self.platform_degrade_controller.update_reference_positions(local_map=local_map, broker_map=broker_map)

            if untrusted is not None:
                self._untrusted_sample_streak += 1
                self._set_untrusted_sample_streak(self._untrusted_sample_streak)
                log = logger.error if self._untrusted_sample_streak >= 3 else logger.info
                log(
                    "reconciliation_sample_untrusted",
                    reason=untrusted,
                    consecutive=self._untrusted_sample_streak,
                    broker_error=_extract_positions_error(self.client),
                    drift_reduce_only=self._drift_reduce_only,
                    halt_triggered=self._halt_triggered,
                    consequence="no state transition this cycle; latches preserved",
                )
                if (
                    self._untrusted_sample_streak >= self.untrusted_sample_degrade_after
                    and not self._unverifiable_reduce_only
                ):
                    self._unverifiable_reduce_only = True
                    logger.error(
                        "reconciliation_unverifiable_entering_reduce_only",
                        reason=untrusted,
                        consecutive=self._untrusted_sample_streak,
                        threshold=self.untrusted_sample_degrade_after,
                    )
                    await self.platform_degrade_controller.enter_reduce_only_async(reason="reconciliation_unverifiable")
                self._not_verified()
                self._record_sync_duration(time.monotonic() - t0)
                self._record_sync_result("skipped_untrusted")
                return
            if self._untrusted_sample_streak:
                self._untrusted_sample_streak = 0
                self._set_untrusted_sample_streak(0)
            self._unverifiable_reduce_only = False

            broker_has_positions = any(int(qty) != 0 for qty in broker_map.values())
            local_has_positions = any(int(qty) != 0 for qty in local_map.values())
            if local_has_positions and not broker_has_positions:
                self._broker_zero_streak += 1
                if self._broker_zero_streak < self.broker_zero_debounce_observations:
                    self._last_discrepancies = []
                    self._last_noncritical_drift_signature = {}
                    self._noncritical_drift_streak = 0
                    self._not_verified()
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

            discrepancies = self._compute_discrepancies(local_map, broker_map)

            _sim_unverifiable: list[PositionDiscrepancy] = []
            if self._order_mode in _NON_COMPARABLE_ORDER_MODES:
                _kept_sim: list[PositionDiscrepancy] = []
                for d in discrepancies:
                    if d.broker_qty == 0 and d.local_qty != 0:
                        _sim_unverifiable.append(d)
                    else:
                        _kept_sim.append(d)
                discrepancies = _kept_sim
                if _sim_unverifiable and sync_log_due:
                    logger.warning(
                        "reconciliation_not_comparable_under_sim_order_mode",
                        order_mode=self._order_mode,
                        symbols=[d.symbol for d in _sim_unverifiable],
                        consequence=(
                            "platform positions cannot be confirmed against a live account "
                            "while orders route to paper; not drift, and never auto-resolved"
                        ),
                    )
            self._set_not_comparable(bool(_sim_unverifiable))

            discrepancies_before_resolution = discrepancies

            platform_codes = self._get_platform_symbols()
            if platform_codes and self._order_mode not in _NON_COMPARABLE_ORDER_MODES:
                protected = platform_codes | self._strategy_owned_symbols(per_strategy_map)
                resolved: list[PositionDiscrepancy] = []
                kept: list[PositionDiscrepancy] = []
                for d in discrepancies:
                    if d.symbol not in protected and d.broker_qty == 0 and d.local_qty != 0:
                        self.store.clear_symbol_positions(d.symbol)
                        resolved.append(d)
                    else:
                        kept.append(d)
                if resolved:
                    logger.warning(
                        "non_platform_phantom_auto_resolved",
                        resolved=[{"symbol": d.symbol, "local_qty": d.local_qty} for d in resolved],
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

            self._metrics().reconciliation_discrepancy_count.set(len(discrepancies_before_resolution))

            for d in discrepancies:
                self._record_discrepancy(d.severity)

            _drift_gauge = getattr(self._metrics(), "position_drift_qty", None)
            if _drift_gauge is not None:
                _m = self._metrics()
                for d in discrepancies:
                    _capped = _m.cap_symbol(d.symbol) if _m else d.symbol
                    _drift_gauge.labels(strategy="", symbol=_capped).set(abs(d.diff))

            duration = time.monotonic() - t0
            self._record_sync_duration(duration)
            self._record_sync_result("not_comparable" if _sim_unverifiable else "success")
            self._update_last_success_ts()

            if discrepancies:
                self._not_verified()
                logger.warning(
                    "Position discrepancies detected",
                    count=len(discrepancies),
                    discrepancies=[
                        {"symbol": d.symbol, "local": d.local_qty, "broker": d.broker_qty, "diff": d.diff}
                        for d in discrepancies
                    ],
                )

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

                critical = [d for d in discrepancies if d.is_critical]
                if critical:
                    self._last_noncritical_drift_signature = {}
                    self._noncritical_drift_streak = 0
                    self._critical_drift_streak += 1
                    if not self._drift_reduce_only:
                        self._drift_reduce_only = True
                        await self.platform_degrade_controller.enter_reduce_only_async(reason="reconciliation_drift")
                    if self._halt_triggered:
                        logger.warning(
                            "critical_drift_persists_during_halt",
                            streak=self._critical_drift_streak,
                            symbols=[d.symbol for d in critical],
                        )
                        if self._auto_correct_enabled and self._critical_drift_streak >= self._auto_correct_after:
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
                    try:
                        _streak_g = getattr(self._metrics(), "reconciliation_drift_streak", None)
                        if _streak_g is not None:
                            _m = self._metrics()
                            for d in discrepancies:
                                _capped = _m.cap_symbol(d.symbol) if _m else d.symbol
                                _streak_g.labels(symbol=_capped).set(self._noncritical_drift_streak)
                    except Exception:
                        pass
                    if persists_or_grows and self._noncritical_drift_streak >= 2:
                        self._drift_reduce_only = True
                        await self.platform_degrade_controller.enter_reduce_only_async(reason="reconciliation_drift")
            else:
                self._drift_reduce_only = False
                try:
                    _streak_g = getattr(self._metrics(), "reconciliation_drift_streak", None)
                    if _streak_g is not None and self._last_noncritical_drift_signature:
                        _m = self._metrics()
                        for _sym in self._last_noncritical_drift_signature.keys():
                            _capped = _m.cap_symbol(_sym) if _m else _sym
                            _streak_g.labels(symbol=_capped).set(0)
                except Exception:
                    pass
                self._last_noncritical_drift_signature = {}
                self._noncritical_drift_streak = 0
                self._critical_drift_streak = 0
                if self._halt_triggered:
                    self.storm_guard.set_reconciliation_hold(False)
                self._halt_triggered = False
                if _sim_unverifiable:
                    self._not_verified()
                else:
                    await self._verified()
                if sync_log_due:
                    logger.info(
                        "Portfolio Sync Complete - No discrepancies",
                        count=len(broker_map),
                        suppressed_cycles=self._take_suppressed_sync_logs(),
                    )

        except Exception as e:
            self._last_noncritical_drift_signature = {}
            self._noncritical_drift_streak = 0
            self._not_verified()
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
        return any(current_signature[s] >= previous_signature[s] for s in overlapping_symbols)

    @staticmethod
    def _strategy_owned_symbols(per_strategy_map: dict[str, dict[str, int]]) -> set[str]:
        """Symbols the platform's own book attributes to a named strategy.

        Used only to *protect* positions from the non-platform auto-resolve. A
        position carrying a real strategy_id was opened by this platform, so
        the premise of that path -- "this was placed externally" -- is false
        for it by construction, regardless of what the symbol universe happens
        to contain.

        Three ids are deliberately NOT protected, because none of them asserts
        platform ownership: ``MANUAL`` is the explicit marker for a manual or
        orphan broker-app position (exactly what auto-resolve exists to clear),
        ``UNKNOWN`` is the attribution-failure sentinel, and ``"*"`` is the
        unattributed bucket. The symbol-universe check still governs all three.
        """
        owned: set[str] = set()
        for strategy_id, positions in per_strategy_map.items():
            if not strategy_id or strategy_id in _UNOWNED_STRATEGY_IDS:
                continue
            for symbol, qty in positions.items():
                if int(qty) != 0:
                    owned.add(str(symbol))
        return owned

    def _get_platform_symbols(self) -> set[str]:
        """Return the set of symbols the platform is actively managing.

        Combines the symbol source's subscribed codes with alias→actual
        mappings. Returns empty set if unavailable (disables the non-platform
        filter).

        ``subscribed_codes`` and ``alias_to_actual`` are QUOTE-side attributes.
        Handed the order client -- which has neither -- this degraded silently
        to the config list, and under loop_v1 that list is
        ``['TMFR1', 'TXFR1']``: continuous front-month aliases, not tradable
        codes. Every resolved contract the platform actually trades was
        therefore "not a platform symbol", while the set stayed non-empty so
        the filter still ran. Hence ``symbol_source``.
        """
        codes: set[str] = set()
        sub = getattr(self._symbol_source, "subscribed_codes", None)
        if sub:
            codes |= set(sub)
        alias_map = getattr(self._symbol_source, "alias_to_actual", None)
        if alias_map:
            codes |= set(alias_map.keys())
            codes |= set(alias_map.values())
        for s in self.config.get("symbols", []):
            if isinstance(s, dict):
                code = s.get("code", "")
                if code:
                    codes.add(code)
            elif isinstance(s, str):
                codes.add(s)
        return codes

    @staticmethod
    def _broker_position_kind(pos: Any) -> str:
        """Return a coarse broker position kind for collision handling.

        Runtime reconciliation is symbol-scoped. If Shioaji returns the same
        literal ``code`` from heterogeneous position types (for example stock
        and futopt snapshots), summing them inflates ``broker_qty``. We treat
        heterogeneous duplicates as ambiguous and keep the first-seen row.
        """
        if isinstance(pos, dict):
            for key in ("position_type", "product_type", "security_type", "kind"):
                value = pos.get(key)
                if value:
                    return str(value)
            return "dict"
        return type(pos).__name__

    @classmethod
    def _build_broker_map(cls, raw_positions: List[Any]) -> Dict[str, int]:
        broker_map: Dict[str, int] = {}
        code_kind: Dict[str, str] = {}
        for pos in raw_positions:
            code = getattr(pos, "code", None) or (pos.get("code") if isinstance(pos, dict) else None)
            qty = getattr(pos, "quantity", None) or (pos.get("quantity", 0) if isinstance(pos, dict) else 0)
            direction = getattr(pos, "direction", "") or (pos.get("direction", "") if isinstance(pos, dict) else "")
            if str(direction) in ("Action.Sell", "Short"):
                qty = -qty
            if not code:
                continue
            kind = cls._broker_position_kind(pos)
            previous_kind = code_kind.get(str(code))
            if previous_kind is not None and previous_kind != kind:
                logger.warning(
                    "broker_position_code_collision_across_types",
                    symbol=code,
                    first_kind=previous_kind,
                    ignored_kind=kind,
                )
                continue
            code_kind[str(code)] = kind
            broker_map[str(code)] = broker_map.get(str(code), 0) + int(qty)
        return broker_map

    @staticmethod
    def _is_futures(symbol: str) -> bool:
        """Heuristic: futures symbols contain common TAIFEX prefixes.

        Delegates to :func:`hft_platform.core.symbol_classifier.is_futures_symbol`
        to keep a single source of truth shared with ``startup_recon``.
        """
        return is_futures_symbol(symbol)

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

    def _filter_auto_correctable(self, discrepancies: List[PositionDiscrepancy]) -> List[PositionDiscrepancy]:
        """Filter discrepancies eligible for auto-correction.

        Two directions are supported:
        - local=0, broker>0: orphaned phantom fill — adopt broker position.
        - local>0, broker=0: expired/cleared position (e.g. option expiry,
          manual broker-side close) — clear phantom local position.

        Large or sign-mismatch discrepancies require manual intervention.
        """
        result: List[PositionDiscrepancy] = []
        for d in discrepancies:
            max_qty = self._auto_correct_futures_max_qty if d.is_futures else self._auto_correct_stock_max_qty
            if d.local_qty == 0 and abs(d.broker_qty) <= max_qty:
                result.append(d)
                continue
            if d.broker_qty == 0 and abs(d.local_qty) <= max_qty:
                result.append(d)
                continue
        return result

    def _local_snapshot(self) -> tuple[Dict[str, Any], Dict[str, Any]]:
        """Live positions and parked recovery lots, read under ONE ``_fill_lock``.

        Reading ``_recovery_positions`` after a separate ``snapshot_positions()``
        could miss a lot from both: the first fill for its key moves it from
        recovery into positions between the two reads, and the drift check then
        counts it as absent locally.
        """
        both = getattr(self.store, "snapshot_positions_with_recovery", None)
        if callable(both):
            result = both()
            if isinstance(result, tuple) and len(result) == 2:
                positions, recovery = result
                if isinstance(positions, dict) and isinstance(recovery, dict):
                    return positions, recovery
        snapshot = (
            self.store.snapshot_positions()
            if hasattr(self.store, "snapshot_positions")
            else dict(getattr(self.store, "positions", {}))
        )
        recovery_raw = getattr(self.store, "_recovery_positions", None)
        return snapshot, dict(recovery_raw) if isinstance(recovery_raw, dict) else {}

    def _resolve_clear_strategy_scope(self, symbol: str, expected_local_qty: int) -> str | None:
        """Return a ``strategy_id`` to scope ``clear_symbol_positions`` to.

        When the phantom local position is wholly attributable to MANUAL
        (either live entries or pending recovery rows), the clear is
        scoped to ``MANUAL_STRATEGY_ID`` so active strategy positions on
        the same symbol are preserved. Otherwise returns ``None`` which
        falls back to the original symbol-wide clear.
        """
        manual_qty = 0
        other_qty = 0
        snapshot, recovery = self._local_snapshot()
        for pos in snapshot.values():
            if getattr(pos, "symbol", None) != symbol:
                continue
            qty = int(getattr(pos, "net_qty", 0) or 0)
            if qty == 0:
                continue
            if getattr(pos, "strategy_id", "") == MANUAL_STRATEGY_ID:
                manual_qty += qty
            else:
                other_qty += qty
        for rdata in recovery.values():
            if not isinstance(rdata, dict) or rdata.get("symbol") != symbol:
                continue
            qty = int(rdata.get("net_qty", 0) or 0)
            if qty == 0:
                continue
            if rdata.get("strategy_id", "") == MANUAL_STRATEGY_ID:
                manual_qty += qty
            else:
                other_qty += qty

        if manual_qty != 0 and other_qty == 0:
            return MANUAL_STRATEGY_ID
        if manual_qty != 0 and manual_qty == expected_local_qty:
            return MANUAL_STRATEGY_ID
        return None

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
                scoped_strategy = self._resolve_clear_strategy_scope(d.symbol, d.local_qty)
                self.store.clear_symbol_positions(d.symbol, strategy_id=scoped_strategy)
            else:
                self.store.load_recovery(
                    account_id="default",
                    symbol=d.symbol,
                    net_qty=d.broker_qty,
                    avg_price_scaled=-1,
                    realized_pnl_scaled=0,
                    fees_scaled=0,
                    strategy_id=MANUAL_STRATEGY_ID,
                )
            try:
                self._metrics().reconciliation_auto_corrected_total.labels(symbol=d.symbol).inc()
            except Exception:
                pass

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

        self.storm_guard.set_reconciliation_hold(True)
        self.storm_guard.trigger_halt(reason)

        try:
            await asyncio.to_thread(
                manual_latches.activate, manual_latches.RECONCILIATION_MISMATCH, reason, actor="engine"
            )
        except Exception as exc:
            logger.error("reconciliation_latch_write_failed", error=str(exc))
