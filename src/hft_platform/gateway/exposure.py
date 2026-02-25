"""CE2-04: ExposureStore — atomic CAS exposure tracking with integer arithmetic.

Architecture (D2):
- Lock scope: dict lookup + integer arithmetic only (~200 ns).
- All values are scaled integers (Precision Law).
- CANCEL intents skip check_and_update; release_exposure reduces notional.
- Symbol cardinality is bounded by _max_symbols (CE2-12); zero-balance eviction
  runs before rejecting new symbols.
"""

from __future__ import annotations

import os
import threading
from dataclasses import dataclass
from typing import Optional

from structlog import get_logger

from hft_platform.contracts.strategy import IntentType, OrderIntent

logger = get_logger("gateway.exposure")


class ExposureLimitError(RuntimeError):
    """Raised when ExposureStore cannot admit a new symbol entry after eviction."""


@dataclass(slots=True)
class ExposureKey:
    account: str
    strategy_id: str
    symbol: str


@dataclass(slots=True)
class ExposureLimits:
    max_notional_scaled: int = 0  # 0 = unlimited per-account-strategy-symbol
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
        self._lock = threading.Lock()
        self._limits: dict[str, ExposureLimits] = limits or {}

    # ── Hot path ──────────────────────────────────────────────────────────

    def _evict_zeroes(self) -> None:
        """Remove leaf entries with zero notional. Called under self._lock."""
        removed = 0
        for acct, strat_map in list(self._exposure.items()):
            for strat_id, sym_map in list(strat_map.items()):
                for sym, val in list(sym_map.items()):
                    if val == 0:
                        del sym_map[sym]
                        removed += 1
                if not sym_map:
                    del strat_map[strat_id]
            if not strat_map:
                del self._exposure[acct]
        self._symbol_count -= removed

    def check_and_update(
        self,
        key: ExposureKey,
        intent: OrderIntent,
    ) -> tuple[bool, str]:
        """Atomic check-and-update.

        Returns (approved: bool, reason: str).
        CANCEL intents always return (True, "OK").

        Raises:
            ExposureLimitError: if a new symbol entry cannot be admitted even
                after zero-balance eviction (CE2-12 memory bound).
        """
        if intent.intent_type == IntentType.CANCEL:
            return True, "OK"

        # Notional = price * qty  (both already scaled integers)
        notional = intent.price * intent.qty

        with self._lock:
            # Global check
            if self._global_max > 0 and self._global_notional + notional > self._global_max:
                return False, "GLOBAL_EXPOSURE_LIMIT"

            # Per-strategy limit check
            strat_limits = self._limits.get(key.strategy_id)
            if strat_limits and strat_limits.max_notional_scaled > 0:
                current = self._exposure.get(key.account, {}).get(key.strategy_id, {}).get(key.symbol, 0)
                if current + notional > strat_limits.max_notional_scaled:
                    return False, "STRATEGY_EXPOSURE_LIMIT"

            # Symbol cardinality bound (CE2-12)
            is_new_symbol = key.symbol not in self._exposure.get(key.account, {}).get(key.strategy_id, {})
            if is_new_symbol and self._symbol_count >= self._max_symbols:
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

            # Commit
            self._global_notional += notional
            acct_exp = self._exposure.setdefault(key.account, {})
            strat_exp = acct_exp.setdefault(key.strategy_id, {})
            if key.symbol not in strat_exp:
                self._symbol_count += 1
            strat_exp[key.symbol] = strat_exp.get(key.symbol, 0) + notional

        return True, "OK"

    def check_and_update_typed(
        self,
        key: ExposureKey,
        *,
        intent_type: int,
        price: int,
        qty: int,
    ) -> tuple[bool, str]:
        """Typed fast-path variant using primitive fields (avoids OrderIntent materialization)."""
        if int(intent_type) == int(IntentType.CANCEL):
            return True, "OK"

        notional = int(price) * int(qty)

        with self._lock:
            if self._global_max > 0 and self._global_notional + notional > self._global_max:
                return False, "GLOBAL_EXPOSURE_LIMIT"

            strat_limits = self._limits.get(key.strategy_id)
            if strat_limits and strat_limits.max_notional_scaled > 0:
                current = self._exposure.get(key.account, {}).get(key.strategy_id, {}).get(key.symbol, 0)
                if current + notional > strat_limits.max_notional_scaled:
                    return False, "STRATEGY_EXPOSURE_LIMIT"

            is_new_symbol = key.symbol not in self._exposure.get(key.account, {}).get(key.strategy_id, {})
            if is_new_symbol and self._symbol_count >= self._max_symbols:
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

            self._global_notional += notional
            acct_exp = self._exposure.setdefault(key.account, {})
            strat_exp = acct_exp.setdefault(key.strategy_id, {})
            if key.symbol not in strat_exp:
                self._symbol_count += 1
            strat_exp[key.symbol] = strat_exp.get(key.symbol, 0) + notional

        return True, "OK"

    def release_exposure(
        self,
        key: ExposureKey,
        intent: OrderIntent,
    ) -> None:
        """Reduce exposure on fill/cancel/reject (immutable tuple replacement pattern)."""
        if intent.intent_type == IntentType.CANCEL:
            return

        notional = intent.price * intent.qty

        with self._lock:
            self._global_notional = max(0, self._global_notional - notional)
            strat_exp = self._exposure.get(key.account, {}).get(key.strategy_id, {})
            if key.symbol in strat_exp:
                strat_exp[key.symbol] = max(0, strat_exp[key.symbol] - notional)

    def release_exposure_typed(
        self,
        key: ExposureKey,
        *,
        intent_type: int,
        price: int,
        qty: int,
    ) -> None:
        if int(intent_type) == int(IntentType.CANCEL):
            return
        notional = int(price) * int(qty)
        with self._lock:
            self._global_notional = max(0, self._global_notional - notional)
            strat_exp = self._exposure.get(key.account, {}).get(key.strategy_id, {})
            if key.symbol in strat_exp:
                strat_exp[key.symbol] = max(0, strat_exp[key.symbol] - notional)

    def get_exposure(self, account: str, strategy_id: str, symbol: str) -> int:
        """Read current exposure (thread-safe snapshot)."""
        with self._lock:
            return self._exposure.get(account, {}).get(strategy_id, {}).get(symbol, 0)

    def get_global_notional(self) -> int:
        with self._lock:
            return self._global_notional
