import dataclasses
import importlib
import os
import threading
from dataclasses import dataclass
from typing import Any, Dict

from structlog import get_logger

from hft_platform.contracts.execution import FillEvent, PositionDelta, Side
from hft_platform.core.pricing import PriceCodec, SymbolMetadataPriceScaleProvider
from hft_platform.feed_adapter.normalizer import SymbolMetadata
from hft_platform.observability.metrics import MetricsRegistry

logger = get_logger("positions")

_RUST_POSITIONS = os.getenv("HFT_RUST_POSITIONS", "1").lower() not in {
    "0",
    "false",
    "no",
    "off",
}

_RustPositionTracker = None
if _RUST_POSITIONS:
    _rust_mod: Any = None
    try:
        _rust_mod = importlib.import_module("hft_platform.rust_core")
    except ImportError as exc:
        logger.debug("operation_fallback", error=str(exc))
        try:
            _rust_mod = importlib.import_module("rust_core")
        except ImportError as exc:
            logger.debug("operation_fallback", error=str(exc))
            _rust_mod = None
    if _rust_mod is not None:
        _RustPositionTracker = getattr(_rust_mod, "RustPositionTracker", None)


_MIN_PEAK_SCALED: int = int(os.getenv("HFT_DRAWDOWN_MIN_PEAK_SCALED", "100000000"))


@dataclass(slots=True)
class Position:
    """Position state using integer fixed-point arithmetic (no float for financial calc).

    All price/pnl/fee values are stored as scaled integers to comply with Precision Law.
    Use descaled_* properties for display purposes only.
    """

    account_id: str
    strategy_id: str
    symbol: str

    net_qty: int = 0
    avg_price_scaled: int = 0  # Fixed-point integer (scaled by price scale)

    realized_pnl_scaled: int = 0  # Fixed-point integer
    fees_scaled: int = 0  # Fixed-point integer

    last_update_ts: int = 0

    # Properties for backward compatibility and display (descaled to human-readable)
    @property
    def avg_price(self) -> int:
        """Return scaled avg_price for internal use (backward compat)."""
        return self.avg_price_scaled

    @property
    def realized_pnl(self) -> int:
        """Return scaled realized_pnl for internal use (backward compat)."""
        return self.realized_pnl_scaled

    @property
    def fees(self) -> int:
        """Return scaled fees for internal use (backward compat)."""
        return self.fees_scaled

    def descaled_avg_price(self, scale: int) -> float:
        """Descale avg_price for display purposes only."""
        return self.avg_price_scaled / scale if scale else 0.0

    def descaled_realized_pnl(self, scale: int) -> float:
        """Descale realized_pnl for display purposes only."""
        return self.realized_pnl_scaled / scale if scale else 0.0

    def descaled_fees(self, scale: int) -> float:
        """Descale fees for display purposes only."""
        return self.fees_scaled / scale if scale else 0.0

    def update(self, fill: FillEvent, scale: int = 1, contract_multiplier: int = 1) -> None:
        """Update position with fill using integer-only arithmetic.

        Args:
            fill: The fill event with price already in scaled integer form.
            scale: Price scale factor (kept for API compat, but fill.price is already scaled).
            contract_multiplier: Contract point value multiplier. Stocks=1, futures=point_value.
                For TMF (微台指) = 10, MXF (小台指) = 50, TXF (台指期) = 200.
        """
        # fill.price is already in fixed-point scaled integer
        fill_qty = fill.qty
        fill_price_scaled = fill.price  # Already scaled integer from FillEvent

        is_buy = fill.side == Side.BUY
        signed_fill_qty = fill_qty if is_buy else -fill_qty

        # Accumulate fees (already scaled)
        self.fees_scaled += fill.fee + fill.tax

        # Check if closing: signs are different
        current_sign = 1 if self.net_qty > 0 else -1 if self.net_qty < 0 else 0
        fill_sign = 1 if is_buy else -1

        closing = current_sign != 0 and fill_sign != current_sign

        if closing:
            # qty to close is min(abs(net), abs(fill_qty))
            close_qty = min(abs(self.net_qty), fill_qty)

            # PnL calculation using integer arithmetic
            # PnL = (Exit Price - Entry Price) * Qty * ContractMultiplier for LONG
            # PnL = (Entry Price - Exit Price) * Qty * ContractMultiplier for SHORT
            # Stocks: multiplier=1, Futures: multiplier=point_value (e.g. TMF=10, MXF=50, TXF=200)
            if is_buy:  # Covering a SHORT
                pnl = (self.avg_price_scaled - fill_price_scaled) * close_qty * contract_multiplier
            else:  # Selling a LONG
                pnl = (fill_price_scaled - self.avg_price_scaled) * close_qty * contract_multiplier

            self.realized_pnl_scaled += pnl

            # Update Net Qty
            self.net_qty += signed_fill_qty

            # If we flipped position side, remaining qty starts new avg price
            if (current_sign > 0 and self.net_qty < 0) or (current_sign < 0 and self.net_qty > 0):
                self.avg_price_scaled = fill_price_scaled
            elif self.net_qty == 0:
                self.avg_price_scaled = 0

        else:
            # Increasing position or flat -> open
            # Weighted avg: (OldNet * OldAvg + FillQty * FillPrice) / NewNet
            # Integer division (truncation is acceptable for HFT)

            if self.net_qty == 0:
                self.avg_price_scaled = fill_price_scaled
                self.net_qty += signed_fill_qty
            else:
                # Integer arithmetic: multiply first, divide last.
                # Use round() to prevent systematic truncation drift from //.
                total_val = (self.net_qty * self.avg_price_scaled) + (signed_fill_qty * fill_price_scaled)
                self.net_qty += signed_fill_qty
                if self.net_qty != 0:
                    # Integer-only division with rounding to nearest (Python ints: no overflow)
                    self.avg_price_scaled = (2 * total_val + self.net_qty) // (2 * self.net_qty)

        self.last_update_ts = fill.match_ts_ns


class PositionStore:
    __slots__ = (
        "positions",
        "_positions_max_size",
        "metrics",
        "metadata",
        "price_codec",
        "_rust_tracker",
        "_log_fills",
        "_fill_lock",
        "_peak_equity_scaled",
        "_total_realized_pnl_scaled",
        "_evicted_realized_pnl_scaled",
        "_recovery_positions",
        "_recovery_rpnl_offsets",
        "_recovery_fees_offsets",
        "__dict__",  # needed for test monkey-patching
    )

    def __init__(self) -> None:
        # map: f"{account}:{strategy}:{symbol}" -> Position
        self.positions: Dict[str, Position] = {}
        self._positions_max_size = int(os.getenv("HFT_POSITIONS_MAX_SIZE", "10000"))
        self.metrics = MetricsRegistry.get()
        self.metadata = SymbolMetadata()
        self.price_codec = PriceCodec(SymbolMetadataPriceScaleProvider(self.metadata))
        self._rust_tracker = _RustPositionTracker() if _RustPositionTracker is not None else None
        self._log_fills = os.getenv("HFT_LOG_FILLS", "0") == "1"
        # Lock for atomic Rust/Python tracker access to prevent race conditions.
        # NOTE: This is intentionally a threading.Lock (not asyncio.Lock) because:
        # 1. The Rust FFI calls are synchronous and cannot be awaited
        # 2. The critical section is very short (microseconds for Rust path)
        # 3. For async contexts, use on_fill_async() which runs this in a thread pool
        self._fill_lock = threading.Lock()
        # Recovery positions from crash recovery (keyed by "account:symbol")
        self._recovery_positions: Dict[str, Dict[str, Any]] = {}
        # Offsets to add to Rust-returned rpnl/fees (keyed by position key "acc:strat:sym")
        self._recovery_rpnl_offsets: Dict[str, int] = {}
        self._recovery_fees_offsets: Dict[str, int] = {}
        # Portfolio-level tracking for StormGuard drawdown
        self._peak_equity_scaled: int = 0  # High watermark of total realized PnL
        self._total_realized_pnl_scaled: int = 0  # Sum across all positions
        self._evicted_realized_pnl_scaled: int = 0  # Accumulated PnL from evicted flat positions

    @property
    def total_pnl(self) -> int:
        """Total realized PnL across all positions (scaled int)."""
        return self._total_realized_pnl_scaled

    def get_drawdown_pct(self) -> float:
        """Portfolio drawdown from peak equity as a fraction (0.0 to 1.0).

        Returns 0.0 when peak equity has not reached the minimum threshold
        (cold-start guard) — without this, even tiny fee-induced losses
        produce 100% drawdown vs the just-crossed-zero peak.

        Threshold is read once at module import from
        ``HFT_DRAWDOWN_MIN_PEAK_SCALED`` (default 100_000_000 = 10,000 NTD =
        1000 pts on TMFD6). Bug 10 (2026-04-16) used 2_000_000 (200 NTD)
        which was too tight for HFT-scale low-volume strategies; Bug B
        incident 2026-04-20T01:42 UTC: R47 morning +24 pts → afternoon
        -22 pts pullback computed 92.9% drawdown vs intraday peak,
        triggering false HALT. Operators tune via the env var.
        """
        # Wave 3 (2026-04-25): snapshot the (peak, current) pair under
        # _fill_lock so a concurrent writer cannot update one field
        # between our two reads, producing an internally inconsistent
        # drawdown that doesn't correspond to any pair the writer ever
        # set atomically.
        with self._fill_lock:
            peak = self._peak_equity_scaled
            current = self._total_realized_pnl_scaled
        if peak < _MIN_PEAK_SCALED:
            return 0.0
        if current >= peak:
            return 0.0
        return (peak - current) / peak

    def net_qty_for_symbol(self, symbol: str, strategy_id: str | None = None) -> int:
        """Return aggregate net_qty for *symbol*, optionally filtered by strategy.

        When *strategy_id* is ``None``, all strategies AND recovery positions
        are included (aggregate view).  When *strategy_id* is provided, only
        positions belonging to that strategy are summed — recovery positions
        are included only if their ``strategy_id`` matches the filter (or if
        they have no ``strategy_id`` and no filter is applied).
        """
        total = 0
        for _key, pos in self.positions.items():
            if getattr(pos, "symbol", None) != symbol:
                continue
            if strategy_id is not None and getattr(pos, "strategy_id", None) != strategy_id:
                continue
            total += int(getattr(pos, "net_qty", 0) or 0)
        for rkey, rdata in self._recovery_positions.items():
            if not isinstance(rdata, dict):
                continue
            rsym = rdata.get("symbol", rkey.rsplit(":", 1)[-1])
            if rsym != symbol:
                continue
            rstrat = rdata.get("strategy_id", "")
            if strategy_id is not None and rstrat and rstrat != strategy_id:
                continue
            if strategy_id is not None and not rstrat:
                # Legacy recovery with no strategy_id: exclude from filtered queries
                continue
            total += int(rdata.get("net_qty", 0))
        return total

    def _update_portfolio_aggregates(self, pnl_delta: int = 0) -> None:
        """Update portfolio-level PnL totals and high-watermark.

        Uses O(1) running delta when *pnl_delta* is provided (normal fill path).
        Falls back to O(n) full recompute when called without delta (e.g., manual
        reconciliation) or when pnl_delta is 0.
        """
        if pnl_delta != 0:
            self._total_realized_pnl_scaled += pnl_delta
        else:
            self._total_realized_pnl_scaled = (
                sum(p.realized_pnl_scaled for p in self.positions.values()) + self._evicted_realized_pnl_scaled
            )
        if self._total_realized_pnl_scaled > self._peak_equity_scaled:
            self._peak_equity_scaled = self._total_realized_pnl_scaled

    def load_recovery(
        self,
        account_id: str,
        symbol: str,
        net_qty: int,
        avg_price_scaled: int,
        realized_pnl_scaled: int = 0,
        fees_scaled: int = 0,
        strategy_id: str = "",
    ) -> None:
        """Store a recovery position to be merged on first fill.

        When *strategy_id* is provided the recovery entry is keyed by
        ``account:strategy:symbol`` so the correct strategy receives its
        recovered position on first fill.  Without *strategy_id* the key
        is ``account:symbol`` (legacy behaviour, matched by symbol-suffix
        fallback).
        """
        if net_qty == 0:
            return
        if strategy_id:
            rkey = f"{account_id}:{strategy_id}:{symbol}"
        else:
            rkey = f"{account_id}:{symbol}"
        self._recovery_positions[rkey] = {
            "account_id": account_id,
            "strategy_id": strategy_id,
            "symbol": symbol,
            "net_qty": net_qty,
            "avg_price_scaled": avg_price_scaled,
            "realized_pnl_scaled": realized_pnl_scaled,
            "fees_scaled": fees_scaled,
        }
        logger.info(
            "recovery_position_loaded",
            symbol=symbol,
            strategy_id=strategy_id or "(none)",
            net_qty=net_qty,
            avg_price_scaled=avg_price_scaled,
        )

    def _seed_from_recovery(self, key: str, fill: FillEvent, recovery: Dict[str, Any]) -> None:
        """Pre-seed position state from recovery data before processing first fill."""
        net_qty = recovery["net_qty"]
        avg_price = recovery["avg_price_scaled"]
        rpnl = recovery["realized_pnl_scaled"]
        fees = recovery["fees_scaled"]

        # Sentinel -1 means broker-only recovery with unknown cost basis.
        # Use the first fill price as a proxy to avoid fake PnL from zero basis.
        if avg_price < 0:
            avg_price = fill.price
            logger.warning(
                "recovery_unknown_cost_basis_using_fill_price",
                symbol=fill.symbol,
                fill_price=fill.price,
                net_qty=net_qty,
            )

        # Seed Rust tracker with synthetic fill to establish net_qty + avg_price
        if self._rust_tracker is not None:
            side = 0 if net_qty > 0 else 1  # BUY=0, SELL=1
            multiplier = self.metadata.contract_multiplier(fill.symbol)
            self._rust_tracker.update(key, side, abs(net_qty), avg_price, 0, 0, 0, multiplier)
            # Rust tracker now has correct net_qty and avg_price but zero rpnl/fees.
            # Store offsets so _on_fill_rust can add historical recovery rpnl/fees.
            self._recovery_rpnl_offsets[key] = rpnl
            self._recovery_fees_offsets[key] = fees

        # Create Python Position with full recovery data
        pos = Position(
            account_id=recovery["account_id"],
            strategy_id=fill.strategy_id,
            symbol=recovery["symbol"],
            net_qty=net_qty,
            avg_price_scaled=avg_price,
            realized_pnl_scaled=rpnl,
            fees_scaled=fees,
        )
        self.positions[key] = pos
        # Warn if recovery had no strategy (broker-only) and qty suggests multi-strategy risk
        recovery_strategy = recovery.get("strategy_id", "")
        if not recovery_strategy and abs(net_qty) > abs(fill.qty):
            logger.warning(
                "recovery_multi_strategy_risk",
                key=key,
                recovery_qty=net_qty,
                fill_qty=fill.qty,
                fill_strategy=fill.strategy_id,
                msg="Broker-only recovery assigned all qty to first fill's strategy. "
                "If multiple strategies trade this symbol, other strategies won't see recovered qty.",
            )
        logger.info("recovery_position_merged", key=key, net_qty=net_qty, avg_price=avg_price)

    def on_fill(self, fill: FillEvent) -> PositionDelta:
        """Process fill with atomic tracker access.

        NOTE: This method acquires a threading.Lock. In async contexts, prefer
        on_fill_async() to avoid blocking the event loop.
        """
        key = self._key(fill.account_id, fill.strategy_id, fill.symbol)

        # Use lock to ensure atomic check-and-call for tracker selection
        with self._fill_lock:
            # Merge recovery position on first fill for this key.
            # Priority: account:strategy:symbol → account:symbol → suffix search.
            recovery = self._recovery_positions.pop(
                f"{fill.account_id}:{fill.strategy_id}:{fill.symbol}",
                None,
            )
            if recovery is None:
                recovery = self._recovery_positions.pop(
                    f"{fill.account_id}:{fill.symbol}",
                    None,
                )
            # Fallback: recovery may have been stored with a different account_id
            # domain (e.g., broker_id "shioaji" vs actual account "F123456").
            # Search by symbol suffix when exact key misses.
            if recovery is None and self._recovery_positions:
                suffix = f":{fill.symbol}"
                for k in list(self._recovery_positions):
                    if k.endswith(suffix):
                        recovery = self._recovery_positions.pop(k)
                        logger.info(
                            "recovery_position_matched_by_symbol",
                            recovery_key=k,
                            fill_account=fill.account_id,
                            symbol=fill.symbol,
                        )
                        break
            if recovery is not None and key not in self.positions:
                self._seed_from_recovery(key, fill, recovery)

            if self._rust_tracker is not None:
                return self._on_fill_rust(fill, key)

            return self._on_fill_python(fill, key)

    async def on_fill_async(self, fill: FillEvent) -> PositionDelta:
        """Async-friendly version that runs fill processing in a thread pool.

        Use this in async contexts to avoid blocking the event loop.
        """
        import asyncio

        return await asyncio.to_thread(self.on_fill, fill)

    def _on_fill_rust(self, fill: FillEvent, key: str) -> PositionDelta:
        tracker = self._rust_tracker
        if tracker is None:
            raise RuntimeError("Rust position tracker unavailable")
        multiplier = self.metadata.contract_multiplier(fill.symbol)
        net_qty, avg_price_scaled, realized_pnl_scaled, fees_scaled = tracker.update(
            key,
            int(fill.side),
            fill.qty,
            fill.price,
            fill.fee,
            fill.tax,
            fill.match_ts_ns,
            multiplier,
        )

        # Keep Python-visible cache in sync for tests/debugging/metrics parity.
        # All values stored as scaled integers (no float conversion).
        pos = self.positions.get(key)
        _prev_pnl = pos.realized_pnl_scaled if pos is not None else 0
        if pos is None:
            pos = Position(fill.account_id, fill.strategy_id, fill.symbol)
            self.positions[key] = pos
        pos.net_qty = int(net_qty)
        pos.avg_price_scaled = int(avg_price_scaled)
        # Add recovery offsets (non-zero only for keys that had recovery seeding)
        rpnl_offset = self._recovery_rpnl_offsets.get(key, 0)
        fees_offset = self._recovery_fees_offsets.get(key, 0)
        pos.realized_pnl_scaled = int(realized_pnl_scaled) + rpnl_offset
        pos.fees_scaled = int(fees_scaled) + fees_offset
        pos.last_update_ts = fill.match_ts_ns

        if self._log_fills:
            logger.info(
                "Fill processed",
                key=key,
                net_qty=pos.net_qty,
                pnl=pos.realized_pnl_scaled,
                rust=True,
            )

        # Update portfolio-level aggregates with O(1) delta
        _pnl_delta = pos.realized_pnl_scaled - _prev_pnl
        self._update_portfolio_aggregates(_pnl_delta)

        if self.metrics:
            _sym = self.metrics.cap_symbol(fill.symbol)
            self.metrics.position_pnl_realized.labels(strategy=fill.strategy_id, symbol=_sym).set(
                pos.realized_pnl_scaled
            )
            if hasattr(self.metrics, "portfolio_total_pnl"):
                self.metrics.portfolio_total_pnl.set(self._total_realized_pnl_scaled)
            if hasattr(self.metrics, "portfolio_drawdown_pct"):
                self.metrics.portfolio_drawdown_pct.set(self.get_drawdown_pct())

        return PositionDelta(
            account_id=fill.account_id,
            strategy_id=fill.strategy_id,
            symbol=fill.symbol,
            net_qty=pos.net_qty,
            avg_price=pos.avg_price_scaled,
            realized_pnl=pos.realized_pnl_scaled,
            unrealized_pnl=0,
            delta_source="FILL",
        )

    def _on_fill_python(self, fill: FillEvent, key: str) -> PositionDelta:
        if key not in self.positions:
            # Evict flat positions if at limit
            if len(self.positions) >= self._positions_max_size:
                self._evict_flat_positions()
            self.positions[key] = Position(fill.account_id, fill.strategy_id, fill.symbol)

        pos = self.positions[key]
        _prev_pnl = pos.realized_pnl_scaled
        # Pass contract_multiplier for futures PnL: stocks=1, futures=point_value
        multiplier = self.metadata.contract_multiplier(fill.symbol)
        pos.update(fill, contract_multiplier=multiplier)
        if self._log_fills:
            logger.info("Fill processed", key=key, net_qty=pos.net_qty, pnl=pos.realized_pnl_scaled)

        # Update portfolio-level aggregates with O(1) delta
        self._update_portfolio_aggregates(pos.realized_pnl_scaled - _prev_pnl)

        # Emit delta / Update PnL Gauge (all values are already scaled integers)
        if self.metrics:
            _sym = self.metrics.cap_symbol(pos.symbol)
            self.metrics.position_pnl_realized.labels(strategy=pos.strategy_id, symbol=_sym).set(
                pos.realized_pnl_scaled
            )
            if hasattr(self.metrics, "portfolio_total_pnl"):
                self.metrics.portfolio_total_pnl.set(self._total_realized_pnl_scaled)
            if hasattr(self.metrics, "portfolio_drawdown_pct"):
                self.metrics.portfolio_drawdown_pct.set(self.get_drawdown_pct())

        # Emit delta (all values are already in scaled fixed-point form)
        return PositionDelta(
            account_id=pos.account_id,
            strategy_id=pos.strategy_id,
            symbol=pos.symbol,
            net_qty=pos.net_qty,
            avg_price=pos.avg_price_scaled,
            realized_pnl=pos.realized_pnl_scaled,
            unrealized_pnl=0,
            delta_source="FILL",
        )

    def _key(self, acc: str, strat: str, sym: str) -> str:
        return f"{acc}:{strat}:{sym}"

    def mark_to_market(self, mid_prices: dict[str, int]) -> int:
        """Compute total unrealized PnL across all open positions.

        Args:
            mid_prices: Map of symbol → current mid_price (scaled int x10000).

        Returns:
            Total unrealized PnL in scaled int x10000.
            Symbols without a mid_price entry contribute 0.
        """
        total = 0
        accounted_keys: set[str] = set()
        for key, pos in self.positions.items():
            if pos.net_qty == 0:
                continue
            accounted_keys.add(key)
            # Extract symbol from key "{account}:{strategy}:{symbol}"
            symbol = key.rsplit(":", 1)[-1]
            mid = mid_prices.get(symbol)
            if mid is None:
                continue
            # Skip positions with unknown cost basis sentinel (-1)
            if pos.avg_price_scaled < 0:
                continue
            multiplier = self.metadata.contract_multiplier(symbol)
            total += (mid - pos.avg_price_scaled) * pos.net_qty * multiplier

        # Include recovery positions not yet merged into self.positions
        _recovery = getattr(self, "_recovery_positions", None)
        for rkey, rec in (_recovery or {}).items():
            if rkey in accounted_keys:
                continue
            net_qty = rec["net_qty"]
            if net_qty == 0:
                continue
            avg_price = rec["avg_price_scaled"]
            # Sentinel -1 means unknown cost basis (broker-only recovery).
            # Skip MtM for this position to avoid astronomical fake PnL.
            if avg_price < 0:
                continue
            symbol = rec["symbol"]
            mid = mid_prices.get(symbol)
            if mid is None:
                continue
            multiplier = self.metadata.contract_multiplier(symbol)
            total += (mid - avg_price) * net_qty * multiplier
        return total

    def snapshot_positions(self) -> dict:
        """Return a consistent deep copy of positions under fill lock.

        Position objects are mutable dataclasses mutated in-place by on_fill().
        A shallow copy would share Position references, allowing a concurrent
        fill (from broker thread) to mutate fields while the caller (e.g.
        checkpoint writer) reads them — producing torn/inconsistent state.
        dataclasses.replace() copies all fields (all ints/strings, no nested
        mutables) so the snapshot is fully isolated.
        """
        with self._fill_lock:
            return {k: dataclasses.replace(v) for k, v in self.positions.items()}

    def reset(self) -> int:
        """Clear all positions, recovery state, and portfolio aggregates.

        Returns the number of positions cleared. For operational reset scenarios
        where checkpoint + positions need to be zeroed without manual file deletion.
        """
        with self._fill_lock:
            count = len(self.positions)
            self.positions.clear()
            self._recovery_positions.clear()
            self._recovery_rpnl_offsets.clear()
            self._recovery_fees_offsets.clear()
            self._peak_equity_scaled = 0
            self._total_realized_pnl_scaled = 0
            self._evicted_realized_pnl_scaled = 0
        logger.warning("position_store_reset", cleared_positions=count)
        return count

    def clear_symbol_positions(
        self,
        symbol: str,
        strategy_id: str | None = None,
    ) -> int:
        """Remove position entries for *symbol* from store and recovery.

        Used by reconciliation auto-correct when broker reports 0 for a
        symbol that still has a phantom local position (e.g. expired option,
        manual broker-side close).

        When *strategy_id* is provided, only entries matching that
        ``strategy_id`` are removed. This is important for MANUAL drift
        auto-correct (Bug 14): clearing *all* entries for a symbol would
        also wipe active strategy positions that happen to share the
        symbol, which would silently create a new drift.

        Returns the number of position entries removed.
        """
        with self._fill_lock:

            def _pos_matches(pos: Position) -> bool:
                if pos.symbol != symbol:
                    return False
                if strategy_id is not None and pos.strategy_id != strategy_id:
                    return False
                return True

            def _recovery_matches(rd: Dict[str, Any]) -> bool:
                if rd.get("symbol") != symbol:
                    return False
                if strategy_id is not None and rd.get("strategy_id", "") != strategy_id:
                    return False
                return True

            keys_to_remove = [k for k, pos in self.positions.items() if _pos_matches(pos)]
            for k in keys_to_remove:
                self._evicted_realized_pnl_scaled += self.positions[k].realized_pnl_scaled
                del self.positions[k]
            rkeys_to_remove = [rk for rk, rd in self._recovery_positions.items() if _recovery_matches(rd)]
            for rk in rkeys_to_remove:
                del self._recovery_positions[rk]
        if keys_to_remove or rkeys_to_remove:
            logger.info(
                "symbol_positions_cleared",
                symbol=symbol,
                strategy_id=strategy_id,
                positions_removed=len(keys_to_remove),
                recovery_removed=len(rkeys_to_remove),
            )
        return len(keys_to_remove)

    def _evict_flat_positions(self) -> None:
        """Evict positions with net_qty=0 to free memory."""
        flat_keys = [k for k, pos in self.positions.items() if pos.net_qty == 0]
        if flat_keys:
            # Sort by last_update_ts and remove oldest flat positions
            flat_keys.sort(key=lambda k: self.positions[k].last_update_ts)
            evict_count = min(len(flat_keys), max(1, len(self.positions) // 10))
            for k in flat_keys[:evict_count]:
                # Preserve realized PnL from evicted positions for portfolio aggregates
                self._evicted_realized_pnl_scaled += self.positions[k].realized_pnl_scaled
                del self.positions[k]
            logger.info("Evicted flat positions", count=evict_count, remaining=len(self.positions))
