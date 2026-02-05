import asyncio
import time
from dataclasses import dataclass
from typing import Dict, List, Optional

from hft_platform.execution.positions import PositionStore
from hft_platform.risk.storm_guard import StormGuard
from structlog import get_logger

logger = get_logger("reconciliation")


@dataclass
class PositionDiscrepancy:
    """Represents a mismatch between local and broker positions."""

    symbol: str
    local_qty: int
    broker_qty: int
    diff: int

    @property
    def is_critical(self) -> bool:
        """Critical if signs differ or absolute diff exceeds threshold."""
        if self.local_qty == 0 and self.broker_qty == 0:
            return False
        # Sign mismatch is always critical
        if (self.local_qty > 0 and self.broker_qty < 0) or (self.local_qty < 0 and self.broker_qty > 0):
            return True
        # Large absolute diff is critical (threshold: 100 shares or 10% of position)
        threshold = max(100, abs(self.local_qty) // 10) if self.local_qty != 0 else 100
        return abs(self.diff) > threshold


class ReconciliationService:
    def __init__(
        self,
        client,
        position_store: PositionStore,
        config,
        storm_guard: Optional[StormGuard] = None,
    ):
        self.client = client
        self.store = position_store
        self.config = config
        self.storm_guard = storm_guard
        self.check_interval_s = config.get("reconciliation", {}).get("heartbeat_threshold_ms", 1000) / 1000.0
        self.last_heartbeat = time.time()
        self.running = False
        self._last_discrepancies: List[PositionDiscrepancy] = []

    async def run(self):
        self.running = True
        logger.info("ReconciliationService started")

        # 1. Startup Sync
        await self.sync_portfolio()

        while self.running:
            await asyncio.sleep(self.check_interval_s)

            # 2. Runtime Check - periodic reconciliation
            try:
                await self.sync_portfolio()
            except Exception as e:
                logger.error("Runtime reconciliation failed", error=str(e))

    async def sync_portfolio(self):
        logger.info("Starting Portfolio Sync...")
        try:
            # 1. Fetch positions from broker
            raw_positions = await asyncio.to_thread(self.client.get_positions)

            # 2. Build broker position map {symbol: qty}
            broker_map: Dict[str, int] = {}
            for pos in raw_positions:
                code = getattr(pos, "code", None) or (pos.get("code") if isinstance(pos, dict) else None)
                qty = getattr(pos, "quantity", None) or (pos.get("quantity", 0) if isinstance(pos, dict) else 0)
                direction = getattr(pos, "direction", "")
                if str(direction) == "Action.Sell":
                    qty = -qty
                if code:
                    broker_map[code] = int(qty)

            logger.info("Portfolio Sync: Broker State", positions=broker_map)

            # 3. Build local position map {symbol: qty}
            local_map: Dict[str, int] = {}
            for key, pos in self.store.positions.items():
                # Key format: "account:strategy:symbol"
                symbol = pos.symbol
                # Aggregate across strategies for the same symbol
                local_map[symbol] = local_map.get(symbol, 0) + pos.net_qty

            logger.info("Portfolio Sync: Local State", positions=local_map)

            # 4. Compute discrepancies
            discrepancies = self._compute_discrepancies(local_map, broker_map)
            self._last_discrepancies = discrepancies

            if discrepancies:
                logger.warning(
                    "Position discrepancies detected",
                    count=len(discrepancies),
                    discrepancies=[
                        {"symbol": d.symbol, "local": d.local_qty, "broker": d.broker_qty, "diff": d.diff}
                        for d in discrepancies
                    ],
                )

                # 5. Check for critical discrepancies and trigger HALT if needed
                critical = [d for d in discrepancies if d.is_critical]
                if critical:
                    await self._trigger_halt(critical)
            else:
                logger.info("Portfolio Sync Complete - No discrepancies", count=len(broker_map))

        except Exception as e:
            logger.error("Portfolio Sync Failed", error=str(e), exc_info=True)

    def _compute_discrepancies(
        self, local_map: Dict[str, int], broker_map: Dict[str, int]
    ) -> List[PositionDiscrepancy]:
        """Compare local and broker positions, return list of discrepancies."""
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
                    )
                )

        return discrepancies

    async def _trigger_halt(self, critical_discrepancies: List[PositionDiscrepancy]) -> None:
        """Trigger StormGuard HALT due to reconciliation mismatch."""
        symbols = [d.symbol for d in critical_discrepancies]
        reason = (
            f"RECONCILIATION_MISMATCH: {len(critical_discrepancies)} critical discrepancies ({symbols[:3]})"
        )

        logger.critical(
            "Triggering HALT due to reconciliation mismatch",
            critical_count=len(critical_discrepancies),
            symbols=symbols,
        )

        if self.storm_guard:
            self.storm_guard.trigger_halt(reason)
        else:
            logger.error("No StormGuard configured - HALT not triggered (manual intervention required)")
