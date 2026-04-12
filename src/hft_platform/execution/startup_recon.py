"""WU-02: Crash Recovery Position Verification.

One-shot async check comparing broker positions vs local PositionStore
at startup, before the trading loop begins.
"""

from __future__ import annotations

import asyncio
import json
import os
from dataclasses import dataclass, field
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
startup_recon_positions_loaded = Gauge(
    "startup_recon_positions_loaded",
    "Number of symbols loaded into PositionStore at startup",
)
startup_recon_auto_corrected = Gauge(
    "startup_recon_auto_corrected",
    "Number of position discrepancies auto-corrected at startup",
)

_BLOCK_ENV = "HFT_STARTUP_RECON_BLOCK"
_CHECKPOINT_PATH_ENV = "HFT_POSITION_CHECKPOINT_PATH"


@dataclass(slots=True)
class RecoveryResult:
    """Outcome of startup position recovery."""

    source: str  # "dual", "broker_only", "checkpoint_only", "empty"
    positions_loaded: int = 0
    auto_corrected: int = 0
    halted: bool = False
    mismatches: list[dict] = field(default_factory=list)


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
        qty_threshold: int | None = None,
        futures_qty_threshold: int | None = None,
    ) -> None:
        self.client = client
        self.store = position_store

        if blocking is not None:
            self.blocking = blocking
        else:
            self.blocking = os.environ.get(_BLOCK_ENV, "0") == "1"

        self.checkpoint_path = checkpoint_path or os.environ.get(_CHECKPOINT_PATH_ENV)

        self._qty_threshold = (
            qty_threshold if qty_threshold is not None else int(os.environ.get("HFT_STARTUP_RECON_QTY_THRESHOLD", "10"))
        )
        self._futures_qty_threshold = (
            futures_qty_threshold
            if futures_qty_threshold is not None
            else int(os.environ.get("HFT_STARTUP_RECON_FUTURES_QTY_THRESHOLD", "2"))
        )

        self.discrepancies: List[PositionDiscrepancy] = []
        self.status: int = 0

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
            # Align with runtime reconciliation: both Action.Sell (stocks)
            # and Short (futures) map to negative qty.
            if str(direction) in ("Action.Sell", "Short"):
                qty = -qty
            if code:
                # Accumulate (not overwrite) to handle multiple account types
                # (stock + futopt) returning the same symbol code.
                broker_map[code] = broker_map.get(code, 0) + int(qty)
        return broker_map

    def _build_local_map(self) -> Dict[str, int]:
        """Build {symbol: qty} map from PositionStore."""
        local_map: Dict[str, int] = {}
        for _key, pos in self.store.snapshot_positions().items():
            symbol = pos.symbol
            local_map[symbol] = local_map.get(symbol, 0) + pos.net_qty
        return local_map

    # ------------------------------------------------------------------
    # Position Recovery (dual-source merge + graduated response)
    # ------------------------------------------------------------------

    async def recover(
        self,
        *,
        trading_date: str | None = None,
        account_id: str = "default",
    ) -> RecoveryResult:
        """Dual-source position recovery with graduated response."""
        from datetime import datetime
        from zoneinfo import ZoneInfo

        from hft_platform.execution.checkpoint import PositionCheckpointWriter

        if trading_date is None:
            trading_date = datetime.fromtimestamp(timebase.now_s(), tz=ZoneInfo("Asia/Taipei")).strftime("%Y%m%d")

        logger.info("position_recovery: starting", trading_date=trading_date)

        # 1. Load checkpoint
        ckpt_data = None
        ckpt_positions: Dict[str, Dict[str, Any]] = {}
        ckpt_valid = False

        if self.checkpoint_path:
            ckpt_data = PositionCheckpointWriter.load_checkpoint(self.checkpoint_path)
            if ckpt_data is not None:
                ckpt_td = ckpt_data.get("trading_date")
                if ckpt_td == trading_date:
                    ckpt_valid = True
                    ckpt_positions = ckpt_data.get("positions", {})
                    # M2: Restore portfolio-level aggregates so StormGuard drawdown
                    # resumes from the correct high-watermark after crash recovery.
                    peak_equity = int(ckpt_data.get("peak_equity_scaled") or 0)
                    total_rpnl = int(ckpt_data.get("total_realized_pnl_scaled") or 0)
                    if peak_equity or total_rpnl:
                        self.store._peak_equity_scaled = peak_equity
                        self.store._total_realized_pnl_scaled = total_rpnl
                        logger.info(
                            "position_recovery: portfolio aggregates restored",
                            peak_equity_scaled=peak_equity,
                            total_realized_pnl_scaled=total_rpnl,
                        )
                    logger.info("position_recovery: checkpoint valid", symbols=len(ckpt_positions))
                else:
                    logger.warning(
                        "position_recovery: checkpoint stale",
                        checkpoint_date=ckpt_td,
                        current_date=trading_date,
                    )
            else:
                logger.info("position_recovery: no checkpoint found")

        # 2. Query broker
        broker_map: Dict[str, int] = {}
        broker_available = False
        try:
            broker_map = await self._fetch_broker_positions()
            broker_available = True
            logger.info("position_recovery: broker positions fetched", symbols=len(broker_map))
        except Exception as exc:
            logger.warning("position_recovery: broker unavailable", error=str(exc))

        # 3. Determine source and act
        if ckpt_valid and broker_available:
            return self._recover_dual(ckpt_positions, broker_map, account_id)
        elif broker_available:
            return self._recover_broker_only(broker_map, account_id)
        elif ckpt_valid:
            return self._recover_checkpoint_only(ckpt_positions, account_id)
        else:
            startup_recon_status.set(3)
            return RecoveryResult(source="empty", halted=True)

    @staticmethod
    def _parse_composite_key(key: str) -> tuple[str, str, str]:
        """Parse ``account:strategy:symbol`` or ``account:symbol`` composite key.

        Returns ``(account_id, strategy_id, symbol)``; strategy_id may be empty.
        """
        parts = key.split(":")
        if len(parts) >= 3:
            return parts[0], parts[1], ":".join(parts[2:])
        if len(parts) == 2:
            return parts[0], "", parts[1]
        return "", "", key

    def _recover_dual(
        self,
        ckpt_positions: Dict[str, Dict[str, Any]],
        broker_map: Dict[str, int],
        account_id: str,
    ) -> RecoveryResult:
        """Cross-validate checkpoint vs broker, apply graduated response.

        Comparison is at symbol-level (broker reports symbol-level only).
        Storage preserves per-strategy granularity from checkpoint.
        """
        all_symbols = set(broker_map.keys())
        for pos_data in ckpt_positions.values():
            sym = pos_data.get("symbol", "")
            if sym:
                all_symbols.add(sym)

        # Build symbol-level qty by ACCUMULATING across strategies
        ckpt_qty_map: Dict[str, int] = {}
        # Group checkpoint entries by symbol (preserving composite keys)
        ckpt_entries_by_symbol: Dict[str, list[tuple[str, Dict[str, Any]]]] = {}
        for _key, pos_data in ckpt_positions.items():
            sym = pos_data.get("symbol", _key.split(":")[-1])
            ckpt_qty_map[sym] = ckpt_qty_map.get(sym, 0) + pos_data.get("net_qty", 0)
            ckpt_entries_by_symbol.setdefault(sym, []).append((_key, pos_data))

        mismatches: list[dict] = []
        has_critical = False
        auto_corrected = 0
        merged: Dict[str, Dict[str, Any]] = {}

        for symbol in all_symbols:
            ckpt_qty = ckpt_qty_map.get(symbol, 0)
            broker_qty = broker_map.get(symbol, 0)
            classification = self._classify_discrepancy(symbol, ckpt_qty, broker_qty)

            entries = ckpt_entries_by_symbol.get(symbol, [])

            if classification == "critical":
                has_critical = True
                mismatches.append(
                    {
                        "symbol": symbol,
                        "checkpoint_qty": ckpt_qty,
                        "broker_qty": broker_qty,
                        "action": "halt",
                    }
                )
            elif classification == "minor":
                auto_corrected += 1
                mismatches.append(
                    {
                        "symbol": symbol,
                        "checkpoint_qty": ckpt_qty,
                        "broker_qty": broker_qty,
                        "action": "corrected",
                    }
                )
                # Distribute broker correction across per-strategy entries.
                # For single-strategy symbols, use broker qty directly.
                # For multi-strategy, scale proportionally (preserving sum = broker_qty).
                self._distribute_correction(entries, broker_qty, account_id, merged)
            else:
                if broker_qty != 0:
                    self._distribute_correction(entries, broker_qty, account_id, merged)

        if has_critical:
            startup_recon_status.set(3)
            return RecoveryResult(source="dual", halted=True, mismatches=mismatches)

        loaded = self._write_to_store(merged, account_id)
        status_val = 2 if auto_corrected > 0 else 1
        startup_recon_status.set(status_val)
        startup_recon_positions_loaded.set(loaded)
        startup_recon_auto_corrected.set(auto_corrected)
        return RecoveryResult(
            source="dual",
            positions_loaded=loaded,
            auto_corrected=auto_corrected,
            mismatches=mismatches,
        )

    def _distribute_correction(
        self,
        entries: list[tuple[str, Dict[str, Any]]],
        target_qty: int,
        account_id: str,
        merged: Dict[str, Dict[str, Any]],
    ) -> None:
        """Write checkpoint entries into *merged*, adjusting to match broker qty.

        For single-strategy entries: straightforward replacement.
        For multi-strategy: assign broker qty to the first entry and zero the rest
        (conservative approach — avoids inventing strategy-level splits the broker
        cannot confirm).
        """
        if not entries:
            return
        if len(entries) == 1:
            key, data = entries[0]
            ckpt_acct, ckpt_strat, sym = self._parse_composite_key(key)
            merged[key] = {
                "symbol": data.get("symbol", sym),
                "net_qty": target_qty,
                "avg_price_scaled": data.get("avg_price_scaled", 0),
                "realized_pnl_scaled": data.get("realized_pnl_scaled", 0),
                "fees_scaled": data.get("fees_scaled", 0),
                "account_id": ckpt_acct or account_id,
                "strategy_id": ckpt_strat,
            }
            return
        # Multi-strategy: preserve per-strategy split from checkpoint when total
        # matches. Otherwise assign proportionally (rounded, remainder to first).
        ckpt_total = sum(d.get("net_qty", 0) for _, d in entries)
        # First pass: compute adjusted quantities per entry.
        adj_qtys: list[int] = []
        for i, (_key, data) in enumerate(entries):
            entry_qty = data.get("net_qty", 0)
            if ckpt_total == target_qty:
                adj_qtys.append(entry_qty)
            elif ckpt_total != 0:
                adj_qtys.append(round(entry_qty * target_qty / ckpt_total))
            else:
                adj_qtys.append(target_qty if i == 0 else 0)
        # Remainder correction: ensure sum equals target_qty exactly.
        remainder = target_qty - sum(adj_qtys)
        if remainder != 0:
            adj_qtys[0] += remainder
        # Second pass: write into merged dict.
        for idx, (key, data) in enumerate(entries):
            ckpt_acct, ckpt_strat, sym = self._parse_composite_key(key)
            merged[key] = {
                "symbol": data.get("symbol", sym),
                "net_qty": adj_qtys[idx],
                "avg_price_scaled": data.get("avg_price_scaled", 0),
                "realized_pnl_scaled": data.get("realized_pnl_scaled", 0),
                "fees_scaled": data.get("fees_scaled", 0),
                "account_id": ckpt_acct or account_id,
                "strategy_id": ckpt_strat,
            }

    def _recover_broker_only(self, broker_map: Dict[str, int], account_id: str) -> RecoveryResult:
        """Use broker positions only (no valid checkpoint)."""
        merged: Dict[str, Dict[str, Any]] = {}
        for symbol, qty in broker_map.items():
            if qty != 0:
                # avg_price_scaled=0 causes massive fake PnL on first close.
                # Use a sentinel -1 so downstream can detect "unknown cost basis"
                # and avoid treating first close as profit from zero.
                merged[symbol] = {
                    "symbol": symbol,
                    "net_qty": qty,
                    "avg_price_scaled": -1,
                    "realized_pnl_scaled": 0,
                    "fees_scaled": 0,
                }
        loaded = self._write_to_store(merged, account_id)
        startup_recon_status.set(1)
        startup_recon_positions_loaded.set(loaded)
        return RecoveryResult(source="broker_only", positions_loaded=loaded)

    def _recover_checkpoint_only(
        self,
        ckpt_positions: Dict[str, Dict[str, Any]],
        account_id: str,
    ) -> RecoveryResult:
        """Use checkpoint positions only (broker unavailable).

        Preserves per-strategy granularity from checkpoint composite keys.
        """
        merged: Dict[str, Dict[str, Any]] = {}
        for _key, pos_data in ckpt_positions.items():
            ckpt_acct, ckpt_strat, sym = self._parse_composite_key(_key)
            qty = pos_data.get("net_qty", 0)
            if qty != 0:
                merged[_key] = {
                    "symbol": pos_data.get("symbol", sym),
                    "net_qty": qty,
                    "avg_price_scaled": pos_data.get("avg_price_scaled", 0),
                    "realized_pnl_scaled": pos_data.get("realized_pnl_scaled", 0),
                    "fees_scaled": pos_data.get("fees_scaled", 0),
                    "account_id": ckpt_acct or account_id,
                    "strategy_id": ckpt_strat,
                }
        loaded = self._write_to_store(merged, account_id)
        startup_recon_status.set(1)
        startup_recon_positions_loaded.set(loaded)
        return RecoveryResult(source="checkpoint_only", positions_loaded=loaded)

    def _classify_discrepancy(self, symbol: str, ckpt_qty: int, broker_qty: int) -> str:
        """Returns 'match', 'minor', or 'critical'."""
        if ckpt_qty == broker_qty:
            return "match"
        diff = abs(ckpt_qty - broker_qty)
        # Side mismatch (long vs short) is always critical
        if (ckpt_qty > 0 and broker_qty < 0) or (ckpt_qty < 0 and broker_qty > 0):
            return "critical"
        threshold = self._futures_qty_threshold if self._is_futures(symbol) else self._qty_threshold
        return "minor" if diff <= threshold else "critical"

    @staticmethod
    def _is_futures(symbol: str) -> bool:
        """Heuristic: futures symbols contain common TAIFEX prefixes."""
        return any(c in symbol.upper() for c in ("FD", "FX", "TX", "MX", "TE", "TF"))

    def _write_to_store(self, positions: Dict[str, Dict[str, Any]], account_id: str) -> int:
        """Write recovered positions into PositionStore via load_recovery.

        Positions are stored as pending recovery entries.  When the entry
        includes a ``strategy_id`` the key is ``account:strategy:symbol``
        so each strategy receives its own recovered position on first fill.
        """
        count = 0
        for _key, data in positions.items():
            self.store.load_recovery(
                account_id=data.get("account_id", account_id),
                symbol=data.get("symbol", _key.split(":")[-1]),
                net_qty=data["net_qty"],
                avg_price_scaled=data.get("avg_price_scaled", 0),
                realized_pnl_scaled=data.get("realized_pnl_scaled", 0),
                fees_scaled=data.get("fees_scaled", 0),
                strategy_id=data.get("strategy_id", ""),
            )
            count += 1
        return count
