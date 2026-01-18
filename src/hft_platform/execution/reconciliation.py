import asyncio
import time

from structlog import get_logger

from hft_platform.execution.positions import PositionStore

logger = get_logger("reconciliation")


class ReconciliationService:
    def __init__(self, client, position_store: PositionStore, config):
        self.client = client
        self.store = position_store
        self.config = config
        self.check_interval_s = config.get("reconciliation", {}).get("heartbeat_threshold_ms", 1000) / 1000.0
        self.last_heartbeat = time.time()
        self.running = False

    async def run(self):
        self.running = True
        logger.info("ReconciliationService started")

        # 1. Startup Sync
        await self.sync_portfolio()

        while self.running:
            await asyncio.sleep(self.check_interval_s)

            # 2. Runtime Check
            # In real impl, check if last_heartbeat from raw queue is too old
            # If so, poll
            pass

    async def sync_portfolio(self):
        logger.info("Starting Portfolio Sync...")
        try:
            # 1. Fetch positions from Shioaji
            # Docs: api.list_positions(account) -> List[Position object]
            # We assume client helper provides get_positions() returning standardized list or we call api directly?
            # FeedAdapter's ShioajiClientWrapper usually exposes raw api or helpers.
            # Assuming client.get_positions() returns list of dicts or objects with 'code' and 'quantity'.

            raw_positions = await asyncio.to_thread(self.client.get_positions)

            # 2. Compare with internal store
            # discrepancies = []

            # Map raw to dict {symbol: qty}
            remote_map = {}
            for pos in raw_positions:
                # pos might be object or dict
                code = getattr(pos, "code", None) or pos.get("code")
                qty = getattr(pos, "quantity", None) or pos.get("quantity", 0)
                # Direction handling might be needed (Buy/Sell to signed int)
                # Shioaji 'quantity' is absolute? 'direction' is Action.Buy/Sell?
                # For Phase 2 simple check: just log what we see
                direction = getattr(pos, "direction", "")
                if str(direction) == "Action.Sell":
                    qty = -qty

                if code:
                    remote_map[code] = int(qty)

            # Check for diffs
            # Assuming store has all symbols we care about
            # Note: Store structure not fully defined in snippet, assuming simple dict-like access
            # If not, skip rigorous check for now.
            logger.info("Portfolio Sync: Remote State", positions=remote_map)

            # TODO: Hard diff against self.store.positions

            logger.info("Portfolio Sync Complete", count=len(remote_map))

        except Exception as e:
            logger.error("Portfolio Sync Failed", error=str(e), exc_info=True)
