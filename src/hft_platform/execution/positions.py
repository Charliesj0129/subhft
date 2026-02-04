import os
from dataclasses import dataclass
from typing import Dict

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
    try:
        try:
            from hft_platform.rust_core import RustPositionTracker as _RustPositionTracker  # type: ignore[attr-defined]
        except Exception:
            from rust_core import RustPositionTracker as _RustPositionTracker  # type: ignore[assignment]
    except Exception:
        _RustPositionTracker = None


@dataclass
class Position:
    account_id: str
    strategy_id: str
    symbol: str

    net_qty: int = 0
    avg_price: float = 0.0  # Using float for internal calc, but might want fixed-point

    realized_pnl: float = 0.0
    fees: float = 0.0

    last_update_ts: int = 0

    def update(self, fill: FillEvent, scale: float = 1.0):
        # Weighted Average Price logic
        # If open position increases: update avg_price
        # If position closes/reduces: realize PnL

        fill_qty = fill.qty
        fill_price = float(fill.price) / scale

        is_buy = fill.side == Side.BUY
        signed_fill_qty = fill_qty if is_buy else -fill_qty

        # Check if closing
        # Closing if signs are different
        current_sign = 1 if self.net_qty > 0 else -1 if self.net_qty < 0 else 0
        fill_sign = 1 if is_buy else -1

        closing = False
        if current_sign != 0 and fill_sign != current_sign:
            closing = True

        if closing:
            # We are closing some amount
            # qty to close is min(abs(net), abs(signed_fill))
            close_qty = min(abs(self.net_qty), abs(signed_fill_qty))

            # PnL = (Exit Price - Entry Price) * Qty * Sign
            # If LONG, Sell at X. PnL = (X - Avg) * Qty
            # If SHORT, Buy at X. PnL = (Avg - X) * Qty = (Exit - Entry) * Qty * (-1)? No.
            # Realized PnL generic: (SellPrice - BuyPrice) * Qty

            pnl = 0.0
            if is_buy:  # Covering a SHORT
                # Entry was avg_price, Exit is fill_price. We are BUYING to close.
                # PnL = (Entry - Exit) * Qty
                pnl = (self.avg_price - fill_price) * close_qty
            else:  # Selling a LONG
                # Entry was avg_price, Exit is fill_price.
                # PnL = (Exit - Entry) * Qty
                pnl = (fill_price - self.avg_price) * close_qty

            self.realized_pnl += pnl

            # Update Net Qty
            self.net_qty += signed_fill_qty

            # If we flipped position side (e.g. Long 10, Sell 20 -> Short 10)
            # The remaining 10 starts new avg price
            if (current_sign > 0 and self.net_qty < 0) or (current_sign < 0 and self.net_qty > 0):
                self.avg_price = fill_price

        else:
            # Increasing position or flat -> open
            # New Avg = (OldNet*OldAvg + FillQty*FillPrice) / NewNet

            if self.net_qty == 0:
                self.avg_price = fill_price
                self.net_qty += signed_fill_qty
            else:
                total_val = (self.net_qty * self.avg_price) + (signed_fill_qty * fill_price)
                self.net_qty += signed_fill_qty
                self.avg_price = total_val / self.net_qty

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
        pos = self.positions.get(key)
        if pos is None:
            pos = Position(fill.account_id, fill.strategy_id, fill.symbol)
            self.positions[key] = pos
        scale = self.price_codec.scale_factor(fill.symbol)
        pos.net_qty = int(net_qty)
        pos.avg_price = float(avg_price_scaled) / scale if scale else 0.0
        pos.realized_pnl = float(realized_pnl_scaled) / scale if scale else 0.0
        pos.fees = float(fees_scaled) / scale if scale else 0.0
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
        scale = self.price_codec.scale_factor(fill.symbol)
        pos.update(fill, scale=scale)
        if self._log_fills:
            logger.info("Fill processed", key=key, net_qty=pos.net_qty, pnl=pos.realized_pnl)

        # Emit delta / Update PnL Gauge
        if self.metrics:
            self.metrics.position_pnl_realized.labels(strategy=pos.strategy_id, symbol=pos.symbol).set(pos.realized_pnl)

        # Emit delta (Re-scale avg_price to Fixed Point for consistency)
        return PositionDelta(
            account_id=pos.account_id,
            strategy_id=pos.strategy_id,
            symbol=pos.symbol,
            net_qty=pos.net_qty,
            avg_price=self.price_codec.scale(fill.symbol, pos.avg_price),
            realized_pnl=self.price_codec.scale(fill.symbol, pos.realized_pnl),  # Re-scale PnL to system scale
            unrealized_pnl=0,
            delta_source="FILL",
        )

    def _key(self, acc, strat, sym):
        return f"{acc}:{strat}:{sym}"
