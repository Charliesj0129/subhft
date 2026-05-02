"""Broker position sync data holder for risk engine (WU-05).

Thread-safe container exposing broker-reported positions to risk validators.
"""

from __future__ import annotations

import threading
from typing import TYPE_CHECKING

import structlog

from hft_platform.core import timebase

if TYPE_CHECKING:
    pass

logger = structlog.get_logger(__name__)


class RiskPositionSyncer:
    """Thread-safe holder of broker-reported positions for risk validation.

    Updated by the reconciliation layer; read by risk validators to
    cross-check internal position state against the broker's view.

    .. note:: **Dead code — not wired into production (M4).**

        As of 2026-04, this class is defined and unit-tested in isolation but is
        never instantiated by any production code path.  ``ReconciliationService``
        does **not** call ``syncer.update()``, and no risk validator reads from it.

        When wiring is needed, the recommended integration is:
        1. Accept a ``RiskPositionSyncer`` instance in ``ReconciliationService.__init__``.
        2. Call ``syncer.update(discrepancies, broker_map)`` at the end of
           ``sync_portfolio()`` after discrepancies are computed.
        3. Inject the same instance into the relevant ``RustRiskValidator`` or
           Python risk guard so it can call ``get_broker_qty()`` during evaluation.

        Until that integration is implemented, this class serves as scaffolding only.
    """

    __slots__ = ("_lock", "_broker_positions", "_discrepancies", "last_sync_ts")

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._broker_positions: dict[str, int] = {}
        self._discrepancies: list[object] = []
        self.last_sync_ts: float = 0.0

    # ------------------------------------------------------------------
    # Write path (called from reconciliation / position sync worker)
    # ------------------------------------------------------------------

    def update(
        self,
        discrepancies: list[object],
        broker_map: dict[str, int],
    ) -> None:
        """Replace internal state with latest broker snapshot.

        Parameters
        ----------
        discrepancies:
            List of discrepancy records from the reconciliation layer.
        broker_map:
            Mapping of symbol -> net quantity as reported by the broker.
        """
        ts = timebase.now_ns()
        with self._lock:
            self._broker_positions = dict(broker_map)
            self._discrepancies = list(discrepancies)
            self.last_sync_ts = ts
        logger.info(
            "risk_position_sync.updated",
            symbols=len(broker_map),
            discrepancies=len(discrepancies),
            ts_ns=ts,
        )

    # ------------------------------------------------------------------
    # Read path (called from risk validators)
    # ------------------------------------------------------------------

    def get_broker_qty(self, symbol: str) -> int | None:
        """Return the broker-reported net quantity for *symbol*, or ``None``."""
        with self._lock:
            return self._broker_positions.get(symbol)

    def get_all_broker_positions(self) -> dict[str, int]:
        """Return a **copy** of the full broker position map."""
        with self._lock:
            return dict(self._broker_positions)
