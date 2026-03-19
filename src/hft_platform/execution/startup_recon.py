"""WU-02: Crash Recovery Position Verification.

One-shot async check comparing broker positions vs local PositionStore
at startup, before the trading loop begins.
"""

from __future__ import annotations

import asyncio
import json
import os
from typing import Any, Dict, List

from prometheus_client import Gauge
from structlog import get_logger

from hft_platform.core import timebase
from hft_platform.execution.positions import PositionStore
from hft_platform.execution.reconciliation import (
    PositionDiscrepancy,
    ReconciliationService,
)

logger = get_logger("startup_recon")

# 0=not_run, 1=pass, 2=discrepancy, 3=error
startup_recon_status = Gauge(
    "startup_recon_status",
    "Startup position reconciliation status (0=not_run, 1=pass, 2=discrepancy, 3=error)",
)
startup_recon_status.set(0)

_BLOCK_ENV = "HFT_STARTUP_RECON_BLOCK"
_CHECKPOINT_PATH_ENV = "HFT_POSITION_CHECKPOINT_PATH"


def _load_checkpoint(path: str) -> Dict[str, int]:
    """Load position checkpoint from a JSON file.

    Expected format: ``{"SYMBOL": qty, ...}`` where qty is an integer.
    Returns an empty dict on any failure.
    """
    try:
        with open(path, "r") as fh:
            data = json.loads(fh.read())
        if not isinstance(data, dict):
            logger.warning("startup_recon: checkpoint is not a dict", path=path)
            return {}
        return {str(k): int(v) for k, v in data.items()}
    except FileNotFoundError:
        logger.warning("startup_recon: checkpoint file not found", path=path)
        return {}
    except Exception as exc:
        logger.error("startup_recon: failed to load checkpoint", path=path, error=str(exc))
        return {}


class StartupPositionVerifier:
    """One-shot verifier that compares broker vs local positions at startup."""

    def __init__(
        self,
        client: Any,
        position_store: PositionStore,
        *,
        blocking: bool | None = None,
        checkpoint_path: str | None = None,
    ) -> None:
        self.client = client
        self.store = position_store

        # Resolve blocking mode from arg or env
        if blocking is not None:
            self.blocking = blocking
        else:
            self.blocking = os.environ.get(_BLOCK_ENV, "0") == "1"

        # Resolve checkpoint path from arg or env
        self.checkpoint_path = checkpoint_path or os.environ.get(_CHECKPOINT_PATH_ENV)

        self.discrepancies: List[PositionDiscrepancy] = []
        self.status: int = 0  # mirrors the gauge

    async def verify(self) -> List[PositionDiscrepancy]:
        """Run the one-shot verification.

        Returns the list of discrepancies found (empty means positions match).
        Updates the ``startup_recon_status`` Prometheus gauge.

        If *blocking* is ``True`` and discrepancies are found, raises
        ``RuntimeError`` to prevent the system from starting.
        """
        logger.info(
            "startup_recon: starting position verification",
            blocking=self.blocking,
            checkpoint_path=self.checkpoint_path,
        )
        t0 = timebase.now_ns()

        try:
            # 1. Fetch broker positions
            broker_map = await self._fetch_broker_positions()

            # 2. Build local position map
            local_map = self._build_local_map()

            # 3. Optionally merge checkpoint data (for symbols not in local store)
            if self.checkpoint_path:
                checkpoint_map = _load_checkpoint(self.checkpoint_path)
                if checkpoint_map:
                    logger.info(
                        "startup_recon: loaded checkpoint",
                        symbols=len(checkpoint_map),
                    )
                    for sym, qty in checkpoint_map.items():
                        if sym not in local_map:
                            local_map[sym] = qty

            # 4. Compute discrepancies via the same logic as ReconciliationService
            self.discrepancies = ReconciliationService._compute_discrepancies(
                None,  # type: ignore[arg-type]  # static-compatible call
                local_map,
                broker_map,
            )

            elapsed_us = (timebase.now_ns() - t0) // 1000
            if self.discrepancies:
                self.status = 2
                startup_recon_status.set(2)
                logger.warning(
                    "startup_recon: discrepancies found",
                    count=len(self.discrepancies),
                    elapsed_us=elapsed_us,
                    discrepancies=[
                        {
                            "symbol": d.symbol,
                            "local": d.local_qty,
                            "broker": d.broker_qty,
                            "diff": d.diff,
                        }
                        for d in self.discrepancies
                    ],
                )
                if self.blocking:
                    raise RuntimeError(
                        f"startup_recon: {len(self.discrepancies)} position "
                        f"discrepancies found in blocking mode — refusing to start"
                    )
            else:
                self.status = 1
                startup_recon_status.set(1)
                logger.info(
                    "startup_recon: positions match",
                    symbols_checked=len(set(local_map) | set(broker_map)),
                    elapsed_us=elapsed_us,
                )

        except RuntimeError:
            # Re-raise blocking-mode errors without masking
            raise
        except Exception as exc:
            self.status = 3
            startup_recon_status.set(3)
            logger.error(
                "startup_recon: verification failed",
                error=str(exc),
            )
            if self.blocking:
                raise RuntimeError(f"startup_recon: verification error in blocking mode — {exc}") from exc

        return self.discrepancies

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    async def _fetch_broker_positions(self) -> Dict[str, int]:
        """Fetch positions from broker and return {symbol: qty} map."""
        raw_positions = await asyncio.to_thread(self.client.get_positions)
        broker_map: Dict[str, int] = {}
        for pos in raw_positions:
            code = getattr(pos, "code", None) or (pos.get("code") if isinstance(pos, dict) else None)
            qty = getattr(pos, "quantity", None) or (pos.get("quantity", 0) if isinstance(pos, dict) else 0)
            direction = getattr(pos, "direction", "")
            if str(direction) == "Action.Sell":
                qty = -qty
            if code:
                broker_map[code] = int(qty)
        return broker_map

    def _build_local_map(self) -> Dict[str, int]:
        """Build {symbol: qty} map from PositionStore."""
        local_map: Dict[str, int] = {}
        for _key, pos in self.store.positions.items():
            symbol = pos.symbol
            local_map[symbol] = local_map.get(symbol, 0) + pos.net_qty
        return local_map
