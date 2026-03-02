from __future__ import annotations

import time
from typing import TYPE_CHECKING, Any

from structlog import get_logger

if TYPE_CHECKING:
    from hft_platform.feed_adapter.shioaji_client import ShioajiClient

logger = get_logger("feed_adapter.account_gateway")


class AccountGateway:
    """Dedicated account/usage/snapshot query gateway."""

    __slots__ = ("_client",)

    def __init__(self, client: "ShioajiClient") -> None:
        self._client = client

    def get_usage(self) -> dict[str, Any]:
        cached = self._client._cache_get("usage")
        if cached is not None:
            return cached
        if self._client.api and self._client.logged_in and hasattr(self._client.api, "usage"):
            start_ns = time.perf_counter_ns()
            try:
                if not self._client._rate_limit_api("usage"):
                    return cached or {"subscribed": self._client.subscribed_count, "bytes_used": 0}
                usage = self._client.api.usage()
                self._client._record_api_latency("usage", start_ns, ok=True)
                self._client._cache_set("usage", self._client._usage_cache_ttl_s, usage)
                return usage
            except Exception as exc:
                self._client._record_api_latency("usage", start_ns, ok=False)
                logger.warning("Failed to fetch usage", error=str(exc))
        return {"subscribed": self._client.subscribed_count, "bytes_used": 0}

    def get_positions(self) -> list[Any]:
        if self._client.mode == "simulation":
            return []
        cached = self._client._cache_get("positions")
        if cached is not None:
            return cached
        start_ns = time.perf_counter_ns()
        try:
            if not self._client._rate_limit_api("positions"):
                return cached or []
            positions: list[Any] = []
            if hasattr(self._client.api, "stock_account") and self._client.api.stock_account is not None:
                positions.extend(self._client.api.list_positions(self._client.api.stock_account))
            if hasattr(self._client.api, "futopt_account") and self._client.api.futopt_account is not None:
                positions.extend(self._client.api.list_positions(self._client.api.futopt_account))
            self._client._record_api_latency("positions", start_ns, ok=True)
            self._client._cache_set("positions", self._client._positions_cache_ttl_s, positions)
            return positions
        except Exception:
            self._client._record_api_latency("positions", start_ns, ok=False)
            logger.warning("Failed to fetch positions")
            return cached or []

    def fetch_snapshots(self) -> list[Any]:
        if not self._client.api or not self._client.logged_in:
            logger.info("Simulation mode: skipping snapshot fetch")
            return []
        contracts: list[Any] = []
        for sym in self._client.symbols:
            code = sym.get("code")
            exchange = sym.get("exchange")
            product_type = sym.get("product_type") or sym.get("security_type") or sym.get("type")
            if not code or not exchange:
                continue
            contract = self._client._get_contract(exchange, code, product_type=product_type, allow_synthetic=False)
            if contract:
                contracts.append(contract)
        if not contracts:
            logger.warning("No contracts resolved for snapshots")
            return []
        snapshots: list[Any] = []
        batch_size = 500
        for i in range(0, len(contracts), batch_size):
            batch = contracts[i : i + batch_size]
            logger.info("Requesting snapshots", batch_size=len(batch))
            start_ns = time.perf_counter_ns()
            try:
                results = self._client.api.snapshots(batch)
                self._client._record_api_latency("snapshots", start_ns, ok=True)
                snapshots.extend(results or [])
                time.sleep(0.11)
            except Exception as exc:
                self._client._record_api_latency("snapshots", start_ns, ok=False)
                logger.error("Snapshot fetch failed", error=str(exc))
        return snapshots

    def get_account_balance(self, account: Any = None) -> Any:
        if self._client.mode == "simulation":
            return {}
        cached = self._client._cache_get("account_balance")
        if cached is not None:
            return cached
        start_ns = time.perf_counter_ns()
        try:
            if not self._client._rate_limit_api("account_balance"):
                return cached or {}
            if account is not None:
                result = self._client.api.account_balance(account)
            else:
                result = self._client.api.account_balance()
            self._client._record_api_latency("account_balance", start_ns, ok=True)
            self._client._cache_set("account_balance", self._client._account_cache_ttl_s, result)
            return result
        except Exception as exc:
            self._client._record_api_latency("account_balance", start_ns, ok=False)
            logger.warning("Failed to fetch account balance", error=str(exc))
            return cached or {}

    def get_margin(self, account: Any = None) -> Any:
        if self._client.mode == "simulation":
            return {}
        cached = self._client._cache_get("margin")
        if cached is not None:
            return cached
        start_ns = time.perf_counter_ns()
        try:
            if not self._client._rate_limit_api("margin"):
                return cached or {}
            acct = account
            if acct is None and hasattr(self._client.api, "futopt_account"):
                acct = self._client.api.futopt_account
            result = self._client.api.margin(acct)
            self._client._record_api_latency("margin", start_ns, ok=True)
            self._client._cache_set("margin", self._client._margin_cache_ttl_s, result)
            return result
        except Exception as exc:
            self._client._record_api_latency("margin", start_ns, ok=False)
            logger.warning("Failed to fetch margin", error=str(exc))
            return cached or {}

    def list_position_detail(self, account: Any = None) -> Any:
        if self._client.mode == "simulation":
            return []
        cached = self._client._cache_get("position_detail")
        if cached is not None:
            return cached
        start_ns = time.perf_counter_ns()
        try:
            if not self._client._rate_limit_api("position_detail"):
                return cached or []
            acct = account
            if acct is None and hasattr(self._client.api, "stock_account"):
                acct = self._client.api.stock_account
            if acct is not None:
                result = self._client.api.list_position_detail(acct)
            else:
                result = self._client.api.list_position_detail()
            self._client._record_api_latency("position_detail", start_ns, ok=True)
            self._client._cache_set("position_detail", self._client._positions_detail_cache_ttl_s, result)
            return result
        except Exception as exc:
            self._client._record_api_latency("position_detail", start_ns, ok=False)
            logger.warning("Failed to fetch position detail", error=str(exc))
            return cached or []

    def list_profit_loss(self, account: Any = None, begin_date: str | None = None, end_date: str | None = None) -> Any:
        if self._client.mode == "simulation":
            return []
        cache_key = f"profit_loss:{begin_date}:{end_date}"
        cached = self._client._cache_get(cache_key)
        if cached is not None:
            return cached
        start_ns = time.perf_counter_ns()
        try:
            if not self._client._rate_limit_api("profit_loss"):
                return cached or []
            acct = account
            if acct is None and hasattr(self._client.api, "stock_account"):
                acct = self._client.api.stock_account
            if acct is not None:
                result = self._client.api.list_profit_loss(acct, begin_date=begin_date, end_date=end_date)
            else:
                result = self._client.api.list_profit_loss(begin_date=begin_date, end_date=end_date)
            self._client._record_api_latency("profit_loss", start_ns, ok=True)
            self._client._cache_set(cache_key, self._client._profit_cache_ttl_s, result)
            return result
        except Exception as exc:
            self._client._record_api_latency("profit_loss", start_ns, ok=False)
            logger.warning("Failed to fetch profit/loss", error=str(exc))
            return cached or []
