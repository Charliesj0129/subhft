"""CE2-04: ExposureStore — atomic CAS exposure tracking with integer arithmetic.

Architecture (D2):
- Lock scope: dict lookup + integer arithmetic only (~200 ns).
- All values are scaled integers (Precision Law).
- CANCEL intents skip check_and_update; release_exposure reduces notional.
"""
from __future__ import annotations

import os
import threading
from dataclasses import dataclass
from typing import Optional

from structlog import get_logger

from hft_platform.contracts.strategy import IntentType, OrderIntent

logger = get_logger("gateway.exposure")


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
    """

    def __init__(
        self,
        global_max_notional: int | None = None,
        limits: Optional[dict[str, ExposureLimits]] = None,
    ) -> None:
        _gmax = global_max_notional if global_max_notional is not None else int(
            os.getenv("HFT_EXPOSURE_GLOBAL_MAX_NOTIONAL", "0")
        )
        self._global_max: int = _gmax
        # acct → strategy_id → symbol → notional_scaled
        self._exposure: dict[str, dict[str, dict[str, int]]] = {}
        self._global_notional: int = 0
        self._lock = threading.Lock()
        self._limits: dict[str, ExposureLimits] = limits or {}

    # ── Hot path ──────────────────────────────────────────────────────────

    def check_and_update(
        self,
        key: ExposureKey,
        intent: OrderIntent,
    ) -> tuple[bool, str]:
        """Atomic check-and-update.

        Returns (approved: bool, reason: str).
        CANCEL intents always return (True, "OK").
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

            # Commit
            self._global_notional += notional
            acct_exp = self._exposure.setdefault(key.account, {})
            strat_exp = acct_exp.setdefault(key.strategy_id, {})
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

    def get_exposure(self, account: str, strategy_id: str, symbol: str) -> int:
        """Read current exposure (thread-safe snapshot)."""
        with self._lock:
            return self._exposure.get(account, {}).get(strategy_id, {}).get(symbol, 0)

    def get_global_notional(self) -> int:
        with self._lock:
            return self._global_notional
