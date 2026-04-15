"""FeeCalculator — pure integer arithmetic fee computation for live execution path.

Loads fee schedules from YAML and computes per-fill commission + tax.
All arithmetic in compute() is pure integer (Precision Law compliance).
"""

from __future__ import annotations

from typing import Any

from structlog import get_logger

from hft_platform.tca.types import FeeBreakdown

logger = get_logger("tca.fee_calculator")

_ZERO = FeeBreakdown(commission=0, tax=0, total=0)


class FeeCalculator:
    """Compute per-fill fees using pure integer arithmetic.

    Constructor args:
        schedules: product_code -> fee config dict
        symbol_to_product: broker symbol -> product code mapping
    """

    __slots__ = ("_schedules", "_symbol_to_product")

    def __init__(
        self,
        schedules: dict[str, dict[str, Any]],
        symbol_to_product: dict[str, str] | None = None,
    ) -> None:
        self._schedules = schedules
        self._symbol_to_product = symbol_to_product or {}

    @classmethod
    def from_yaml(cls, path: str) -> FeeCalculator:
        """Load fee schedules from a YAML file.

        Converts float fields to integer representations:
        - tax_rate_bps (float) -> tax_rate_bps_x100 (int, multiply by 100)
        - tick_size (float) -> tick_size_x100 (int, multiply by 100)
        """
        import yaml  # cold-path only

        with open(path) as f:
            data = yaml.safe_load(f)

        raw_futures = data.get("futures", {})
        symbol_map = data.get("symbol_map", {})

        schedules: dict[str, dict[str, Any]] = {}
        for product_code, cfg in raw_futures.items():
            if product_code in ("overrides",):
                continue
            if not isinstance(cfg, dict):
                continue
            # Convert float fields to integer for pure-int arithmetic
            tax_rate_bps = cfg.get("tax_rate_bps", 0)
            tick_size = cfg.get("tick_size", 1)

            schedules[product_code] = {
                "commission_per_contract": int(cfg.get("commission_per_contract", 0)),
                "tax_rate_bps_x100": int(round(float(tax_rate_bps) * 100)),
                "tax_per_contract": int(cfg.get("tax_per_contract", 0)),
                "tax_side": str(cfg.get("tax_side", "sell")),
                "tick_size_x100": int(round(float(tick_size) * 100)),
                "point_value": int(cfg.get("point_value", 1)),
            }

        logger.info(
            "fee_schedules_loaded",
            products=list(schedules.keys()),
            symbol_map_count=len(symbol_map),
        )
        return cls(schedules, symbol_to_product=symbol_map)

    def _resolve_product(self, symbol: str) -> str | None:
        """Resolve broker symbol to product code.

        Lookup order:
        1. Exact match in symbol_map (e.g. TXFD6 → TX)
        2. Symbol itself is a product code (e.g. TXF → TXF schedule)
        3. Root prefix match: strip 2-char month suffix (e.g. TMFE6 → TMF → XMT)
        """
        product = self._symbol_to_product.get(symbol)
        if product is not None:
            return product
        # Fallback: check if symbol itself is a product code
        if symbol in self._schedules:
            return symbol
        # Root-prefix fallback: strip trailing month code (e.g. TMFE6 → TMF)
        # TAIFEX futures: root (3 chars) + month letter + year digit = 5+ chars
        if len(symbol) >= 5:
            root = symbol[:-2]  # strip month letter + year digit
            product = self._symbol_to_product.get(root)
            if product is not None:
                return product
            if root in self._schedules:
                return root
        return None

    def compute(self, symbol: str, side: str, qty: int, price_scaled: int) -> FeeBreakdown:
        """Compute fees for a fill. All arithmetic is pure integer.

        Args:
            symbol: Broker symbol (e.g. "TXF", "MXF", "TXFL5")
            side: "BUY" or "SELL"
            qty: Number of contracts (signed or unsigned, abs taken)
            price_scaled: Price in scaled x10000 format

        Returns:
            FeeBreakdown with commission, tax, total (all scaled x10000)
        """
        product = self._resolve_product(symbol)
        if product is None:
            logger.warning("unknown_symbol_no_fees", symbol=symbol)
            return _ZERO

        schedule = self._schedules.get(product)
        if schedule is None:
            return _ZERO

        abs_qty = abs(qty)
        if abs_qty == 0:
            return _ZERO

        comm_per_contract: int = schedule["commission_per_contract"]
        tax_rate_x100: int = schedule["tax_rate_bps_x100"]
        tax_per_contract: int = schedule["tax_per_contract"]
        tax_side: str = schedule["tax_side"]
        tick_size_x100: int = schedule["tick_size_x100"]
        point_value: int = schedule["point_value"]

        # Commission: comm_per_contract * abs_qty, scaled x10000
        commission = comm_per_contract * abs_qty * 10000

        # Tax: apply based on tax_side (sell, buy, or both), pure integer arithmetic
        tax = 0
        apply_tax = (
            tax_side == "both" or (tax_side == "sell" and side == "SELL") or (tax_side == "buy" and side == "BUY")
        )
        if apply_tax:
            if tax_per_contract > 0:
                # Flat per-contract tax: tax_per_contract * abs_qty, scaled x10000
                tax = tax_per_contract * abs_qty * 10000
            else:
                # Percentage-based tax: rate in bps
                # notional_x100 = price_scaled * abs_qty * point_value * 100 // tick_size_x100
                # This gives notional in NTD * 100 (extra factor for precision)
                notional_x100 = price_scaled * abs_qty * point_value * 100 // tick_size_x100
                # tax in NTD scaled x10000:
                # tax_rate is in bps*100 (e.g. 2.0 bps -> 200)
                # 1 bps = 1/10000, so tax_rate_x100 / 100 bps = tax_rate_x100 / (100 * 10000) of notional
                # tax = notional * tax_rate_x100 / (100 * 10000), then scale x10000
                # tax_scaled = notional_x100 * tax_rate_x100 // (10000 * 100)
                tax = notional_x100 * tax_rate_x100 // (10000 * 100)

        total = commission + tax
        return FeeBreakdown(commission=commission, tax=tax, total=total)
