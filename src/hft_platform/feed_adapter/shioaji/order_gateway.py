from __future__ import annotations

import time
from typing import TYPE_CHECKING, Any

from structlog import get_logger

from hft_platform.core import timebase

if TYPE_CHECKING:
    from hft_platform.feed_adapter.shioaji_client import ShioajiClient

logger = get_logger("feed_adapter.order_gateway")


class OrderGateway:
    """Dedicated order entry/cancel gateway."""

    __slots__ = ("_client",)

    def __init__(self, client: "ShioajiClient") -> None:
        self._client = client

    @staticmethod
    def _async_kwargs(timeout: int, cb: Any | None) -> dict[str, Any]:
        """Build kwargs for non-blocking API calls.

        When *timeout* is 0 the caller wants async confirmation; pass
        ``timeout=0`` (and optionally ``cb``) through to the Shioaji SDK.
        For the default blocking path (timeout > 0) an empty dict is
        returned so existing behaviour is preserved.
        """
        if timeout != 0:
            return {}
        kw: dict[str, Any] = {"timeout": 0}
        if cb is not None:
            kw["cb"] = cb
        return kw

    @staticmethod
    def _sdk() -> Any | None:
        # Keep test patching backward compatible by reading SDK handle
        # from shioaji_client module-level symbol.
        try:
            from hft_platform.feed_adapter import shioaji_client as client_module

            return getattr(client_module, "sj", None)
        except Exception:
            return None

    def place_order(
        self,
        contract_code: str,
        exchange: str,
        action: str,
        price: float,
        qty: int,
        order_type: str,
        tif: str,
        custom_field: str | None = None,
        product_type: str | None = None,
        order_cond: str | None = None,
        order_lot: str | None = None,
        oc_type: str | None = None,
        account: Any | None = None,
        price_type: str | None = None,
        timeout: int = 5000,
        cb: Any | None = None,
    ) -> Any:
        sdk = self._sdk()
        if not self._client.api:
            logger.warning("Shioaji SDK missing; mock place_order invoked.")
            return {"seq_no": f"sim-{int(timebase.now_s() * 1000)}"}
        if sdk is None:
            raise RuntimeError("Shioaji SDK unavailable")

        contract = self._client._get_contract(exchange, contract_code, product_type=product_type, allow_synthetic=False)
        if not contract:
            raise ValueError(f"Contract {contract_code} not found")
        act = sdk.constant.Action.Buy if action == "Buy" else sdk.constant.Action.Sell

        if product_type:
            return self._place_order_typed(
                contract=contract,
                action=act,
                price=price,
                qty=qty,
                exchange=exchange,
                product_type=product_type,
                tif=tif,
                order_type=order_type,
                price_type=price_type,
                order_cond=order_cond,
                order_lot=order_lot,
                oc_type=oc_type,
                account=account,
                custom_field=custom_field,
                timeout=timeout,
                cb=cb,
            )

        pt = sdk.constant.StockPriceType.LMT
        ot = sdk.constant.OrderType.ROD
        if tif == "IOC":
            ot = sdk.constant.OrderType.IOC
        elif tif == "FOK":
            ot = sdk.constant.OrderType.FOK
        order = sdk.Order(
            price=price,
            quantity=qty,
            action=act,
            price_type=pt,
            order_type=ot,
            custom_field=custom_field,
        )
        start_ns = time.perf_counter_ns()
        try:
            result = self._client.api.place_order(contract, order, **self._async_kwargs(timeout, cb))
            self._client._record_api_latency("place_order", start_ns, ok=True)
            return result
        except Exception:
            self._client._record_api_latency("place_order", start_ns, ok=False)
            raise

    def _place_order_typed(
        self,
        *,
        contract: Any,
        action: Any,
        price: float,
        qty: int,
        exchange: str,
        product_type: str,
        tif: str,
        order_type: str,
        price_type: str | None,
        order_cond: str | None,
        order_lot: str | None,
        oc_type: str | None,
        account: Any | None,
        custom_field: str | None,
        timeout: int = 5000,
        cb: Any | None = None,
    ) -> Any:
        sdk = self._sdk()
        if sdk is None:
            raise RuntimeError("Shioaji SDK unavailable")
        prod = str(product_type or "").strip().lower()
        if not prod:
            prod = "stock" if str(exchange).upper() in {"TSE", "OTC", "OES"} else "future"
        resolved_account = self._resolve_account(prod, account)
        fallback_cls = getattr(sdk, "Order", None)
        if fallback_cls is None:
            raise RuntimeError("Shioaji Order class unavailable")

        if prod in {"stock", "stk"}:
            pt = self._map_stock_price_type(price_type)
            ot = self._map_stock_order_type(tif or order_type)
            cond = self._map_stock_order_cond(order_cond)
            lot = self._map_stock_order_lot(order_lot)
            order_cls = getattr(getattr(sdk, "order", None), "StockOrder", None) or fallback_cls
            if resolved_account is None and order_cls is not fallback_cls:
                order_cls = fallback_cls
            if order_cls is fallback_cls:
                order = order_cls(
                    price=price,
                    quantity=qty,
                    action=action,
                    price_type=pt,
                    order_type=ot,
                    custom_field=custom_field,
                )
            else:
                order = order_cls(
                    price=price,
                    quantity=qty,
                    action=action,
                    price_type=pt,
                    order_type=ot,
                    order_cond=cond,
                    order_lot=lot,
                    account=resolved_account,
                    custom_field=custom_field,
                )
        else:
            pt = self._map_futures_price_type(price_type)
            ot = self._map_futures_order_type(tif or order_type)
            oc = self._map_futures_oc_type(oc_type)
            order_cls = getattr(getattr(sdk, "order", None), "FuturesOrder", None) or fallback_cls
            if resolved_account is None and order_cls is not fallback_cls:
                order_cls = fallback_cls
            if order_cls is fallback_cls:
                order = order_cls(
                    price=price,
                    quantity=qty,
                    action=action,
                    price_type=pt,
                    order_type=ot,
                    custom_field=custom_field,
                )
            else:
                order = order_cls(
                    price=price,
                    quantity=qty,
                    action=action,
                    price_type=pt,
                    order_type=ot,
                    octype=oc,
                    account=resolved_account,
                    custom_field=custom_field,
                )

        start_ns = time.perf_counter_ns()
        try:
            result = self._client.api.place_order(contract, order, **self._async_kwargs(timeout, cb))
            self._client._record_api_latency("place_order", start_ns, ok=True)
            return result
        except Exception:
            self._client._record_api_latency("place_order", start_ns, ok=False)
            raise

    def _resolve_account(self, product_type: str, account: Any | None) -> Any | None:
        if account is not None:
            if isinstance(account, str):
                if account == "stock" and hasattr(self._client.api, "stock_account"):
                    return self._client.api.stock_account
                if account in {"futopt", "future", "option"} and hasattr(self._client.api, "futopt_account"):
                    return self._client.api.futopt_account
            return account
        if not self._client.api:
            return None
        if product_type in {"stock", "stk"} and hasattr(self._client.api, "stock_account"):
            return self._client.api.stock_account
        if product_type in {"future", "futures", "option", "options"} and hasattr(self._client.api, "futopt_account"):
            return self._client.api.futopt_account
        return None

    def _map_stock_price_type(self, price_type: str | None) -> Any:
        sdk = self._sdk()
        if not sdk:
            return None
        key = str(price_type or "LMT").upper()
        return getattr(sdk.constant.StockPriceType, key, sdk.constant.StockPriceType.LMT)

    def _map_stock_order_type(self, order_type: str | None) -> Any:
        sdk = self._sdk()
        if not sdk:
            return None
        key = str(order_type or "ROD").upper()
        return getattr(sdk.constant.OrderType, key, sdk.constant.OrderType.ROD)

    def _map_stock_order_cond(self, order_cond: str | None) -> Any:
        sdk = self._sdk()
        if not sdk:
            return None
        if not order_cond:
            return sdk.constant.StockOrderCond.Cash
        key = str(order_cond).strip().lower().replace("_", "").replace("-", "")
        mapping = {
            "cash": "Cash",
            "margin": "MarginTrading",
            "margintrading": "MarginTrading",
            "short": "ShortSelling",
            "shortselling": "ShortSelling",
        }
        name = mapping.get(key, "Cash")
        return getattr(sdk.constant.StockOrderCond, name, sdk.constant.StockOrderCond.Cash)

    def _map_stock_order_lot(self, order_lot: str | None) -> Any:
        sdk = self._sdk()
        if not sdk:
            return None
        if not order_lot:
            return sdk.constant.StockOrderLot.Common
        key = str(order_lot).strip().lower().replace("_", "").replace("-", "")
        mapping = {
            "common": "Common",
            "fixing": "Fixing",
            "odd": "Odd",
            "intradayodd": "IntradayOdd",
        }
        name = mapping.get(key, "Common")
        return getattr(sdk.constant.StockOrderLot, name, sdk.constant.StockOrderLot.Common)

    def _map_futures_price_type(self, price_type: str | None) -> Any:
        sdk = self._sdk()
        if not sdk:
            return None
        key = str(price_type or "LMT").upper()
        return getattr(sdk.constant.FuturesPriceType, key, sdk.constant.FuturesPriceType.LMT)

    def _map_futures_order_type(self, order_type: str | None) -> Any:
        sdk = self._sdk()
        if not sdk:
            return None
        key = str(order_type or "ROD").upper()
        fut_type = getattr(sdk.constant, "FuturesOrderType", None)
        if fut_type:
            return getattr(fut_type, key, fut_type.ROD)
        return getattr(sdk.constant.OrderType, key, sdk.constant.OrderType.ROD)

    def _map_futures_oc_type(self, oc_type: str | None) -> Any:
        sdk = self._sdk()
        if not sdk:
            return None
        if not oc_type:
            return sdk.constant.FuturesOCType.Auto
        key = str(oc_type).strip().lower().replace("_", "").replace("-", "")
        mapping = {"auto": "Auto", "new": "New", "close": "Close"}
        name = mapping.get(key, "Auto")
        return getattr(sdk.constant.FuturesOCType, name, sdk.constant.FuturesOCType.Auto)

    def cancel_order(
        self,
        trade: Any,
        timeout: int = 5000,
        cb: Any | None = None,
    ) -> Any:
        if not self._client.api:
            logger.warning("Shioaji SDK missing; mock cancel_order invoked.")
            return None
        if not hasattr(self._client.api, "cancel_order"):
            raise RuntimeError("Shioaji API missing cancel_order")
        start_ns = time.perf_counter_ns()
        try:
            result = self._client.api.cancel_order(trade, **self._async_kwargs(timeout, cb))
            self._client._record_api_latency("cancel_order", start_ns, ok=True)
            return result
        except Exception as exc:
            self._client._record_api_latency("cancel_order", start_ns, ok=False)
            logger.error("cancel_order failed", error=str(exc))
            raise

    def update_order(
        self,
        trade: Any,
        price: float | None = None,
        qty: int | None = None,
        timeout: int = 5000,
        cb: Any | None = None,
    ) -> Any:
        if not self._client.api:
            logger.warning("Shioaji SDK missing; mock update_order invoked.")
            return None
        async_kw = self._async_kwargs(timeout, cb)
        if price is not None:
            if hasattr(self._client.api, "update_order"):
                start_ns = time.perf_counter_ns()
                try:
                    result = self._client.api.update_order(trade=trade, price=price, **async_kw)
                    self._client._record_api_latency("update_order", start_ns, ok=True)
                    return result
                except Exception as exc:
                    self._client._record_api_latency("update_order", start_ns, ok=False)
                    logger.error("update_order(price) failed", error=str(exc))
                    raise
            if hasattr(self._client.api, "update_price"):
                start_ns = time.perf_counter_ns()
                try:
                    result = self._client.api.update_price(trade=trade, price=price, **async_kw)
                    self._client._record_api_latency("update_price", start_ns, ok=True)
                    return result
                except Exception as exc:
                    self._client._record_api_latency("update_price", start_ns, ok=False)
                    logger.error("update_price failed", error=str(exc))
                    raise
            raise RuntimeError("Shioaji API missing update_order/update_price")
        if qty is not None:
            if hasattr(self._client.api, "update_order"):
                start_ns = time.perf_counter_ns()
                try:
                    result = self._client.api.update_order(trade=trade, qty=qty, **async_kw)
                    self._client._record_api_latency("update_order", start_ns, ok=True)
                    return result
                except Exception as exc:
                    self._client._record_api_latency("update_order", start_ns, ok=False)
                    logger.error("update_order(qty) failed", error=str(exc))
                    raise
            if hasattr(self._client.api, "update_qty"):
                start_ns = time.perf_counter_ns()
                try:
                    result = self._client.api.update_qty(trade=trade, quantity=qty, **async_kw)
                    self._client._record_api_latency("update_qty", start_ns, ok=True)
                    return result
                except Exception as exc:
                    self._client._record_api_latency("update_qty", start_ns, ok=False)
                    logger.error("update_qty failed", error=str(exc))
                    raise
            raise RuntimeError("Shioaji API missing update_order/update_qty")
        return None
