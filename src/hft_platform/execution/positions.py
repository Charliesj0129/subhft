import importlib
import os
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
    except Exception:
        try:
            _rust_mod = importlib.import_module("rust_core")
        except Exception:
            _rust_mod = None
    if _rust_mod is not None:
        _RustPositionTracker = getattr(_rust_mod, "RustPositionTracker", None)


@dataclass
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

    def update(self, fill: FillEvent, scale: int = 1) -> None:
        """Update position with fill using integer-only arithmetic.

        Args:
            fill: The fill event with price already in scaled integer form.
            scale: Price scale factor (kept for API compat, but fill.price is already scaled).
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
            # PnL = (Exit Price - Entry Price) * Qty for LONG
            # PnL = (Entry Price - Exit Price) * Qty for SHORT
            if is_buy:  # Covering a SHORT
                pnl = (self.avg_price_scaled - fill_price_scaled) * close_qty
            else:  # Selling a LONG
                pnl = (fill_price_scaled - self.avg_price_scaled) * close_qty

            self.realized_pnl_scaled += pnl

            # Update Net Qty
            self.net_qty += signed_fill_qty

            # If we flipped position side, remaining qty starts new avg price
            if (current_sign > 0 and self.net_qty < 0) or (current_sign < 0 and self.net_qty > 0):
                self.avg_price_scaled = fill_price_scaled

        else:
            # Increasing position or flat -> open
            # Weighted avg: (OldNet * OldAvg + FillQty * FillPrice) / NewNet
            # Integer division (truncation is acceptable for HFT)

            if self.net_qty == 0:
                self.avg_price_scaled = fill_price_scaled
                self.net_qty += signed_fill_qty
            else:
                # Integer arithmetic: multiply first, divide last
                total_val = (self.net_qty * self.avg_price_scaled) + (signed_fill_qty * fill_price_scaled)
                self.net_qty += signed_fill_qty
                if self.net_qty != 0:
                    self.avg_price_scaled = total_val // self.net_qty

        self.last_update_ts = fill.match_ts_ns


class PositionStore:
    def __init__(self):
        # map: f"{account}:{strategy}:{symbol}" -> Position
        self.positions: Dict[str, Position] = {}
        self.metrics = MetricsRegistry.get()
        self.metadata = SymbolMetadata()
        self.price_codec = PriceCodec(SymbolMetadataPriceScaleProvider(self.metadata))
        self._rust_tracker = _RustPositionTracker() if _RustPositionTracker is not None else None
        self._log_fills = os.getenv("HFT_LOG_FILLS", "0") == "1"

    def on_fill(self, fill: FillEvent) -> PositionDelta:
        key = self._key(fill.account_id, fill.strategy_id, fill.symbol)

        if self._rust_tracker is not None:
            return self._on_fill_rust(fill, key)

        return self._on_fill_python(fill, key)

    def _on_fill_rust(self, fill: FillEvent, key: str) -> PositionDelta:
        net_qty, avg_price_scaled, realized_pnl_scaled, fees_scaled = self._rust_tracker.update(
            key,
            int(fill.side),
            fill.qty,
            fill.price,
            fill.fee,
            fill.tax,
            fill.match_ts_ns,
        )

        # Keep Python-visible cache in sync for tests/debugging/metrics parity.
        # All values stored as scaled integers (no float conversion).
        pos = self.positions.get(key)
        if pos is None:
            pos = Position(fill.account_id, fill.strategy_id, fill.symbol)
            self.positions[key] = pos
        pos.net_qty = int(net_qty)
        pos.avg_price_scaled = int(avg_price_scaled)
        pos.realized_pnl_scaled = int(realized_pnl_scaled)
        pos.fees_scaled = int(fees_scaled)
        pos.last_update_ts = fill.match_ts_ns

        if self._log_fills:
            logger.info(
                "Fill processed",
                key=key,
                net_qty=net_qty,
                pnl=realized_pnl_scaled,
                rust=True,
            )

        if self.metrics:
            self.metrics.position_pnl_realized.labels(strategy=fill.strategy_id, symbol=fill.symbol).set(
                realized_pnl_scaled
            )

        return PositionDelta(
            account_id=fill.account_id,
            strategy_id=fill.strategy_id,
            symbol=fill.symbol,
            net_qty=net_qty,
            avg_price=avg_price_scaled,
            realized_pnl=realized_pnl_scaled,
            unrealized_pnl=0,
            delta_source="FILL",
        )

    def _on_fill_python(self, fill: FillEvent, key: str) -> PositionDelta:
        if key not in self.positions:
            self.positions[key] = Position(fill.account_id, fill.strategy_id, fill.symbol)

        pos = self.positions[key]
        # Pass scale for API compat, but Position.update() uses fill.price directly (already scaled)
        pos.update(fill)
        if self._log_fills:
            logger.info("Fill processed", key=key, net_qty=pos.net_qty, pnl=pos.realized_pnl_scaled)

        # Emit delta / Update PnL Gauge (all values are already scaled integers)
        if self.metrics:
            self.metrics.position_pnl_realized.labels(strategy=pos.strategy_id, symbol=pos.symbol).set(
                pos.realized_pnl_scaled
            )

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

    def _key(self, acc, strat, sym):
        return f"{acc}:{strat}:{sym}"
