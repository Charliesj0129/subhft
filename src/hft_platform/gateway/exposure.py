"""CE2-04: ExposureStore — atomic CAS exposure tracking with integer arithmetic.

Architecture (D2):
- Lock scope: dict lookup + integer arithmetic only (~200 ns).
- All values are scaled integers (Precision Law).
- CANCEL/FORCE_FLAT intents skip check_and_update.
- AMEND intents compute delta exposure against the original order's reservation.
- Symbol cardinality is bounded by _max_symbols (CE2-12); zero-balance eviction
  runs before rejecting new symbols.
- Per-order tracking enables lifecycle-based release and TTL expiry.
"""

from __future__ import annotations

import os
import threading
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Optional

from structlog import get_logger

from hft_platform.contracts.strategy import IntentType, OrderIntent

if TYPE_CHECKING:
    from hft_platform.contracts.ref import ContractRef

logger = get_logger("gateway.exposure")


class ExposureLimitError(RuntimeError):
    """Raised when ExposureStore cannot admit a new symbol entry after eviction."""


@dataclass(slots=True)
class ExposureKey:
    """Identifies a single exposure bucket.

    ``symbol`` remains the canonical string key for the internal dict
    (avoiding per-tick ContractRef hashing).
    ``contract`` is optional structured metadata (Gate 3) carried for
    observability.
    When both are set we trust ``symbol`` (callers build it via
    :meth:`from_intent` which derives it from ``contract.display()`` when
    ``contract`` is set).
    """

    account: str
    strategy_id: str
    symbol: str
    contract: Optional["ContractRef"] = field(default=None, compare=False)

    @classmethod
    def from_intent(
        cls,
        intent: OrderIntent,
        *,
        account: str = "default",
    ) -> "ExposureKey":
        """Build an :class:`ExposureKey` from an :class:`OrderIntent`.

        When ``intent.contract`` is set, its ``display()`` is used as the
        canonical ``symbol`` — this guarantees identical bucketing between
        intents that carry a structured ref and intents that only carry
        ``symbol``. The raw ``contract`` is stored for downstream
        observability and migration consumers.
        """
        contract: Any = getattr(intent, "contract", None)
        if contract is not None:
            try:
                canonical = contract.display()
            except Exception:  # noqa: BLE001 — defensive; fall back to symbol
                canonical = intent.symbol
        else:
            canonical = intent.symbol
        return cls(
            account=account,
            strategy_id=intent.strategy_id,
            symbol=canonical,
            contract=contract,
        )


@dataclass(slots=True)
class ExposureLimits:
    max_notional_scaled: int = 0  # 0 = unlimited per account and strategy
    global_max_notional_scaled: int = 0  # 0 = unlimited global


class ExposureStore:
    """Thread-safe per-account/strategy/symbol notional tracker.

    Env vars:
        HFT_EXPOSURE_GLOBAL_MAX_NOTIONAL: global max notional (scaled int; 0=disabled)
        HFT_EXPOSURE_MAX_SYMBOLS: max unique (acct, strategy, symbol) entries (default 10_000)
    """

    def __init__(
        self,
        global_max_notional: int | None = None,
        limits: Optional[dict[str, ExposureLimits]] = None,
        max_symbols: int | None = None,
    ) -> None:
        _gmax = (
            global_max_notional
            if global_max_notional is not None
            else int(os.getenv("HFT_EXPOSURE_GLOBAL_MAX_NOTIONAL", "0"))
        )
        self._global_max: int = _gmax
        self._max_symbols: int = (
            max_symbols if max_symbols is not None else int(os.getenv("HFT_EXPOSURE_MAX_SYMBOLS", "10000"))
        )
        # acct → strategy_id → symbol → notional_scaled
        self._exposure: dict[str, dict[str, dict[str, int]]] = {}
        self._symbol_count: int = 0  # tracks total leaf entries
        self._global_notional: int = 0
        self._strategy_notional = dict[tuple[str, str], int]()
        self._lock = threading.Lock()
        self._limits: dict[str, ExposureLimits] = limits or {}
        # Per-order tracking: hold reservations until fill/cancel/TTL (Bug #6+#7 fix)
        self._order_notionals: dict[str, int] = {}  # order_key → reserved notional
        self._order_ts: dict[str, float] = {}  # order_key → monotonic timestamp
        self._order_exp_keys: dict[str, tuple[str, str, str]] = {}  # order_key → (acct, strat, sym)
        self._pending_amend_deltas: dict[str, int] = {}  # target_key → last delta (for rollback)

    # ── Hot path ──────────────────────────────────────────────────────────

    def _evict_zeroes(self) -> None:
        """Remove leaf entries with zero notional. Called under self._lock."""
        # A rejected downward amend may restore a temporarily empty bucket.
        rollback_buckets = {
            self._order_exp_keys[target] for target, delta in self._pending_amend_deltas.items() if delta < 0
        }
        removed = 0
        for acct, strat_map in list(self._exposure.items()):
            for strat_id, sym_map in list(strat_map.items()):
                for sym, val in list(sym_map.items()):
                    if val == 0 and (acct, strat_id, sym) not in rollback_buckets:
                        del sym_map[sym]
                        removed += 1
                if not sym_map:
                    del strat_map[strat_id]
            if not strat_map:
                del self._exposure[acct]
        self._symbol_count -= removed

    def _admit_symbol(self, key):
        """Create a zero-balance bucket under the store lock."""
        if key.symbol in self._exposure.get(key.account, {}).get(key.strategy_id, {}):
            return
        if self._symbol_count >= self._max_symbols:
            self._evict_zeroes()
            if self._symbol_count >= self._max_symbols:
                logger.warning(
                    "exposure_symbol_limit_reached",
                    max_symbols=self._max_symbols,
                    account=key.account,
                    strategy_id=key.strategy_id,
                    symbol=key.symbol,
                )
                raise ExposureLimitError(
                    f"ExposureStore symbol limit ({self._max_symbols}) reached; "
                    f"cannot admit ({key.account}, {key.strategy_id}, {key.symbol})"
                )
        self._exposure.setdefault(key.account, {}).setdefault(key.strategy_id, {})[key.symbol] = 0
        self._symbol_count += 1

    def check_and_update(
        self,
        key: ExposureKey,
        intent: OrderIntent,
        *,
        order_key: str = "",
    ) -> tuple[bool, str]:
        """Atomic check-and-update with per-order tracking.

        Returns (approved: bool, reason: str).
        CANCEL/FORCE_FLAT intents always return (True, "OK").
        AMEND intents compute delta exposure against the original reservation.

        Args:
            order_key: Unique key for per-order tracking (e.g. idempotency_key).
                If empty, per-order tracking is skipped (backward compat).

        Raises:
            ExposureLimitError: if a new symbol entry cannot be admitted even
                after zero-balance eviction (CE2-12 memory bound).
        """
        return self.check_and_update_typed(
            key,
            intent_type=intent.intent_type,
            price=intent.price,
            qty=intent.qty,
            order_key=order_key,
            target_order_key=intent.target_order_id or "",
        )

    def _adjust_notional(self, account, strategy, symbol, delta):
        """Apply an exposure delta under the store lock."""
        strategy_key = (account, strategy)
        strat_exp = self._exposure[account][strategy]
        current = strat_exp.get(symbol, 0)
        updated = max(0, current + delta)
        strat_exp[symbol] = updated
        applied = updated - current
        self._global_notional += applied
        strategy_total = self._strategy_notional.get(strategy_key, 0) + applied
        if strategy_total:
            self._strategy_notional[strategy_key] = strategy_total
        else:
            self._strategy_notional.pop(strategy_key, None)

    def _check_amend_impl(
        self,
        key: ExposureKey,
        target_key: str,
        new_notional: int,
    ) -> tuple[bool, str]:
        """Check AMEND delta exposure against limits.

        Computes delta = new_notional - old_notional (from per-order tracking).
        If target_key is unknown, treats full new_notional as the delta (conservative).
        """
        with self._lock:
            bucket = (key.account, key.strategy_id, key.symbol)
            reserved_bucket = self._order_exp_keys.get(target_key)
            if reserved_bucket is not None and reserved_bucket != bucket:
                return False, "AMEND_EXPOSURE_KEY_MISMATCH"

            old_notional = self._order_notionals.get(target_key, 0)
            delta = new_notional - old_notional

            if delta > 0:
                # Check global limit for the positive delta
                if self._global_max > 0 and self._global_notional + delta > self._global_max:
                    return False, "GLOBAL_EXPOSURE_LIMIT"

                # Check per-strategy limit for the positive delta
                strat_limits = self._limits.get(key.strategy_id)
                if strat_limits and strat_limits.max_notional_scaled > 0:
                    current = self._strategy_notional.get((key.account, key.strategy_id), 0)
                    if current + delta > strat_limits.max_notional_scaled:
                        return False, "STRATEGY_EXPOSURE_LIMIT"

            self._admit_symbol(key)
            self._adjust_notional(key.account, key.strategy_id, key.symbol, delta)

            # Update per-order record and save delta for possible rollback
            if target_key:
                self._order_notionals[target_key] = new_notional
                self._order_ts[target_key] = time.monotonic()
                self._order_exp_keys.setdefault(target_key, bucket)
                self._pending_amend_deltas[target_key] = delta

        return True, "OK"

    def check_and_update_typed(
        self,
        key: ExposureKey,
        *,
        intent_type: int,
        price: int,
        qty: int,
        order_key: str = "",
        target_order_key: str = "",
    ) -> tuple[bool, str]:
        """Typed fast-path variant using primitive fields (avoids OrderIntent materialization)."""
        if int(intent_type) in (int(IntentType.CANCEL), int(IntentType.FORCE_FLAT)):
            return True, "OK"

        if int(intent_type) == int(IntentType.AMEND):
            target_key = target_order_key or order_key
            new_notional = int(price) * int(qty)
            return self._check_amend_impl(key, target_key, new_notional)

        notional = int(price) * int(qty)

        with self._lock:
            if self._global_max > 0 and self._global_notional + notional > self._global_max:
                return False, "GLOBAL_EXPOSURE_LIMIT"

            strat_limits = self._limits.get(key.strategy_id)
            if strat_limits and strat_limits.max_notional_scaled > 0:
                current = self._strategy_notional.get((key.account, key.strategy_id), 0)
                if current + notional > strat_limits.max_notional_scaled:
                    return False, "STRATEGY_EXPOSURE_LIMIT"

            self._admit_symbol(key)
            self._adjust_notional(key.account, key.strategy_id, key.symbol, notional)

            # Per-order tracking
            if order_key:
                self._order_notionals[order_key] = notional
                self._order_ts[order_key] = time.monotonic()
                self._order_exp_keys[order_key] = (key.account, key.strategy_id, key.symbol)

        return True, "OK"

    def release_exposure(
        self,
        key: ExposureKey,
        intent: OrderIntent,
        *,
        order_key: str = "",
    ) -> None:
        """Reduce exposure on rejection.  Uses per-order tracking when available.

        - CANCEL: no-op (CANCEL does not reserve exposure).
        - AMEND: rolls back the delta committed by check_and_update.
        - NEW: releases via per-order tracking if order_key provided,
          otherwise falls back to notional subtraction.
        """
        if intent.intent_type in (IntentType.CANCEL, IntentType.FORCE_FLAT):
            return

        if intent.intent_type == IntentType.AMEND:
            target_key = intent.target_order_id or order_key
            if target_key:
                self._rollback_amend(key, target_key)
            return

        # NEW path — prefer per-order release
        _ok = order_key or intent.idempotency_key
        if _ok and _ok in self._order_notionals:
            self.release_by_order(_ok)
            return

        # Legacy fallback: release by notional value
        notional = intent.price * intent.qty
        with self._lock:
            if key.symbol in self._exposure.get(key.account, {}).get(key.strategy_id, {}):
                self._adjust_notional(key.account, key.strategy_id, key.symbol, -notional)

    def release_exposure_typed(
        self,
        key: ExposureKey,
        *,
        intent_type: int,
        price: int,
        qty: int,
        order_key: str = "",
        target_order_key: str = "",
    ) -> None:
        if int(intent_type) in (int(IntentType.CANCEL), int(IntentType.FORCE_FLAT)):
            return

        if int(intent_type) == int(IntentType.AMEND):
            target_key = target_order_key or order_key
            if target_key:
                self._rollback_amend(key, target_key)
            return

        # NEW path — prefer per-order release
        if order_key and order_key in self._order_notionals:
            self.release_by_order(order_key)
            return

        # Legacy fallback
        notional = int(price) * int(qty)
        with self._lock:
            if key.symbol in self._exposure.get(key.account, {}).get(key.strategy_id, {}):
                self._adjust_notional(key.account, key.strategy_id, key.symbol, -notional)

    # ── Per-order lifecycle ──────────────────────────────────────────────

    def release_by_order(self, order_key: str) -> int:
        """Release exposure for a specific order.  Returns released notional."""
        with self._lock:
            notional = self._order_notionals.pop(order_key, 0)
            self._order_ts.pop(order_key, None)
            exp_tuple = self._order_exp_keys.pop(order_key, None)
            self._pending_amend_deltas.pop(order_key, None)
            if notional > 0 and exp_tuple:
                acct, strat, sym = exp_tuple
                if sym in self._exposure.get(acct, {}).get(strat, {}):
                    self._adjust_notional(acct, strat, sym, -notional)
        return notional

    def _rollback_amend(self, key: ExposureKey, target_key: str) -> None:
        """Undo the delta committed by a rejected AMEND."""
        with self._lock:
            delta = self._pending_amend_deltas.pop(target_key, 0)
            if delta == 0:
                return
            # Reverse the delta
            if key.symbol in self._exposure.get(key.account, {}).get(key.strategy_id, {}):
                self._adjust_notional(key.account, key.strategy_id, key.symbol, -delta)
            # Restore per-order notional to pre-amend value
            if target_key in self._order_notionals:
                self._order_notionals[target_key] = max(0, self._order_notionals[target_key] - delta)

    def expire_stale_orders(self, max_age_s: float) -> int:
        """Expire per-order reservations older than *max_age_s*.  Returns count expired."""
        now = time.monotonic()
        expired_keys: list[str] = []
        with self._lock:
            for ok, ts in self._order_ts.items():
                if now - ts > max_age_s:
                    expired_keys.append(ok)
        count = 0
        for ok in expired_keys:
            released = self.release_by_order(ok)
            if released > 0:
                count += 1
                logger.info("exposure_order_expired", order_key=ok, notional=released)
        return count

    def get_exposure(self, account: str, strategy_id: str, symbol: str) -> int:
        """Read current exposure (thread-safe snapshot)."""
        with self._lock:
            return self._exposure.get(account, {}).get(strategy_id, {}).get(symbol, 0)

    @property
    def global_notional(self) -> int:
        """Current global exposure notional (thread-safe snapshot)."""
        with self._lock:
            return self._global_notional

    def get_global_notional(self) -> int:
        with self._lock:
            return self._global_notional
