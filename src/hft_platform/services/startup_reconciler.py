"""Bug #32 structural fix: backfill broker-side fills missed during restart.

When the engine restarts, in-flight Shioaji fill callbacks die with the
process. Any broker fill that arrives during/after the restart window is
silently lost — `hft.fills` ends the day with fewer rows than the broker's
P&L summary, and platform position drifts from broker truth.

This service runs once at bootstrap (after broker login, before strategies
start). It pulls today's fills from the broker, diffs against `hft.fills`,
and inserts any missing rows tagged `strategy_id='UNKNOWN'` so forensic
queries see the full picture.

Failure mode: any broker query exception is fail-soft (logged + metered;
bootstrap continues so a temporarily-down broker query cannot strand the
engine on restart).
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from datetime import date
from typing import Any, Protocol

from structlog import get_logger

from hft_platform.infra.ch_client import get_ch_client

logger = get_logger("services.startup_reconciler")


class BrokerAccountQuery(Protocol):
    """Subset of AccountGateway used by the reconciler. Both Shioaji and
    Fubon expose these methods; the protocol keeps the reconciler broker-
    agnostic per multi-broker rules MB-01/MB-02."""

    def list_profit_loss(
        self, account: Any = None, begin_date: str | None = None, end_date: str | None = None
    ) -> Any: ...

    def list_profit_loss_detail(
        self, account: Any = None, detail_id: int = 0, unit: str | None = None
    ) -> Any: ...

    def list_position_detail(self, account: Any = None) -> Any: ...


class ChFillsQuery(Protocol):
    """ClickHouse `hft.fills` reader/writer surface used by the reconciler."""

    async def fetch_existing_fill_keys(self, trading_date: date) -> set[tuple[str, str]]: ...

    async def insert_fill(self, row: dict[str, Any]) -> None: ...


class ClickHouseFillsQueryClient:
    """Async wrapper around the shared ClickHouse client for `hft.fills`."""

    __slots__ = ("_client",)

    def __init__(self, client: Any | None = None) -> None:
        self._client = client

    def _get_client(self) -> Any:
        if self._client is None:
            self._client = get_ch_client()
        return self._client

    async def fetch_existing_fill_keys(self, trading_date: date) -> set[tuple[str, str]]:
        return await asyncio.to_thread(self._fetch_existing_fill_keys_sync, trading_date)

    def _fetch_existing_fill_keys_sync(self, trading_date: date) -> set[tuple[str, str]]:
        query = (
            "SELECT broker_order_id, fill_id "
            "FROM hft.fills "
            "WHERE toDate(toDateTime(ts_exchange / 1000000000)) = {date:String}"
        )
        result = self._get_client().query(query, parameters={"date": trading_date.isoformat()})
        rows = getattr(result, "result_rows", None) or []
        keys: set[tuple[str, str]] = set()
        for row in rows:
            if len(row) < 2 or not row[0] or not row[1]:
                continue
            keys.add((str(row[0]), str(row[1])))
        return keys

    async def insert_fill(self, row: dict[str, Any]) -> None:
        await asyncio.to_thread(self._insert_fill_sync, row)

    def _insert_fill_sync(self, row: dict[str, Any]) -> None:
        columns = list(row.keys())
        values = [[row[column] for column in columns]]
        self._get_client().insert("hft.fills", values, column_names=columns)


@dataclass(slots=True)
class _NormalizedFill:
    broker_order_id: str
    fill_id: str
    symbol: str
    side: str
    qty: int
    price_scaled: int
    ts_ns: int


@dataclass(slots=True)
class ReconcileResult:
    broker_fills: int = 0
    platform_fills: int = 0
    inserted: int = 0
    broker_query_error: bool = False
    elapsed_seconds: float = 0.0
    errors: list[str] = field(default_factory=list)


_PRICE_SCALE = 1_000_000  # ClickHouse hft.fills.price_scaled = x1M (matches recorder/mapper.py)


def _normalize_action(raw: Any) -> str:
    s = str(raw or "").strip().lower()
    if s in ("buy", "b", "0"):
        return "BUY"
    if s in ("sell", "s", "1"):
        return "SELL"
    return s.upper() or "UNKNOWN"


def _normalize_one(row: Any) -> _NormalizedFill | None:
    """Best-effort extraction from a Shioaji P&L detail or position detail row."""
    fill_id = getattr(row, "id", None) or getattr(row, "fill_id", None)
    broker_order_id = (
        getattr(row, "order_id", None)
        or getattr(row, "seqno", None)
        or getattr(row, "ordno", None)
    )
    if not fill_id or not broker_order_id:
        return None

    symbol = getattr(row, "code", None) or getattr(row, "symbol", "") or ""
    side = _normalize_action(getattr(row, "action", None))
    qty = int(getattr(row, "quantity", 0) or 0)
    price = float(getattr(row, "price", 0) or 0)
    ts_ns = int(getattr(row, "ts", 0) or 0)

    return _NormalizedFill(
        broker_order_id=str(broker_order_id),
        fill_id=str(fill_id),
        symbol=str(symbol),
        side=side,
        qty=qty,
        price_scaled=int(round(price * _PRICE_SCALE)),
        ts_ns=ts_ns,
    )


class StartupReconciler:
    """Backfill broker-side fills missed during engine restart."""

    __slots__ = ("_broker", "_ch", "_metrics", "_today", "_broker_account")

    def __init__(
        self,
        broker_account_query: BrokerAccountQuery,
        ch_fills_query: ChFillsQuery,
        metrics: Any,
        today: date,
        broker_account: Any | None = None,
    ) -> None:
        self._broker = broker_account_query
        self._ch = ch_fills_query
        self._metrics = metrics
        self._today = today
        self._broker_account = broker_account

    async def run(self) -> ReconcileResult:
        start = time.perf_counter()
        result = ReconcileResult()
        try:
            broker_fills = self._collect_broker_fills(result)
        except Exception as exc:  # noqa: BLE001 - fail-soft per Q5
            logger.error(
                "startup_reconciler.broker_query_failed",
                error_type=type(exc).__name__,
                error=str(exc),
            )
            result.broker_query_error = True
            result.errors.append(f"{type(exc).__name__}: {exc}")
            self._metrics.startup_reconciler_missing_fills_total.labels(result="error").inc()
            result.elapsed_seconds = time.perf_counter() - start
            self._metrics.startup_reconciler_run_seconds.observe(result.elapsed_seconds)
            return result

        result.broker_fills = len(broker_fills)
        try:
            existing_keys = await self._ch.fetch_existing_fill_keys(self._today)
        except Exception as exc:  # noqa: BLE001 - fail-soft per Q5
            logger.error(
                "startup_reconciler.ch_query_failed",
                error_type=type(exc).__name__,
                error=str(exc),
            )
            result.broker_query_error = True
            result.errors.append(f"ch:{type(exc).__name__}: {exc}")
            self._metrics.startup_reconciler_missing_fills_total.labels(result="error").inc()
            result.elapsed_seconds = time.perf_counter() - start
            self._metrics.startup_reconciler_run_seconds.observe(result.elapsed_seconds)
            return result

        result.platform_fills = len(existing_keys)

        for fill in broker_fills:
            key = (fill.broker_order_id, fill.fill_id)
            if key in existing_keys:
                continue
            row = self._fill_to_row(fill)
            try:
                await self._ch.insert_fill(row)
                result.inserted += 1
                existing_keys.add(key)  # local dedup against intra-batch dup
                self._metrics.startup_reconciler_missing_fills_total.labels(
                    result="inserted"
                ).inc()
            except Exception as exc:  # noqa: BLE001
                logger.error(
                    "startup_reconciler.insert_failed",
                    error_type=type(exc).__name__,
                    error=str(exc),
                    broker_order_id=fill.broker_order_id,
                    fill_id=fill.fill_id,
                )
                result.errors.append(f"insert:{type(exc).__name__}: {exc}")
                self._metrics.startup_reconciler_missing_fills_total.labels(
                    result="error"
                ).inc()

        result.elapsed_seconds = time.perf_counter() - start
        self._metrics.startup_reconciler_run_seconds.observe(result.elapsed_seconds)

        logger.info(
            "startup_reconciler.complete",
            broker_fills=result.broker_fills,
            platform_fills=result.platform_fills,
            inserted=result.inserted,
            elapsed_seconds=round(result.elapsed_seconds, 3),
        )
        return result

    def _collect_broker_fills(self, result: ReconcileResult) -> list[_NormalizedFill]:
        """Pull (closed P&L details ∪ open position details), deduped by
        (broker_order_id, fill_id). Raises on broker query failure (caller
        catches and reports fail-soft)."""
        today_iso = self._today.strftime("%Y-%m-%d")

        seen: set[tuple[str, str]] = set()
        out: list[_NormalizedFill] = []

        # Closed round-trips: list_profit_loss → list_profit_loss_detail per pnl_id
        pnl_summaries = self._broker.list_profit_loss(
            account=self._broker_account, begin_date=today_iso, end_date=today_iso
        ) or []
        for summary in pnl_summaries:
            pnl_id = getattr(summary, "id", None)
            if pnl_id is None:
                continue
            try:
                detail_rows = self._broker.list_profit_loss_detail(
                    account=self._broker_account, detail_id=int(pnl_id)
                ) or []
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "startup_reconciler.pnl_detail_failed",
                    pnl_id=pnl_id,
                    error_type=type(exc).__name__,
                    error=str(exc),
                )
                result.errors.append(f"pnl_detail:{pnl_id}:{type(exc).__name__}: {exc}")
                continue
            for raw in detail_rows:
                fill = _normalize_one(raw)
                if fill is None:
                    continue
                key = (fill.broker_order_id, fill.fill_id)
                if key in seen:
                    continue
                seen.add(key)
                out.append(fill)

        # Open positions: list_position_detail (each FIFO entry is a fill)
        try:
            open_rows = self._broker.list_position_detail(account=self._broker_account) or []
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "startup_reconciler.position_detail_failed",
                error_type=type(exc).__name__,
                error=str(exc),
            )
            result.errors.append(f"position_detail:{type(exc).__name__}: {exc}")
            open_rows = []
        for raw in open_rows:
            fill = _normalize_one(raw)
            if fill is None:
                continue
            key = (fill.broker_order_id, fill.fill_id)
            if key in seen:
                continue
            seen.add(key)
            out.append(fill)

        return out

    def _fill_to_row(self, fill: _NormalizedFill) -> dict[str, Any]:
        """Map normalized broker fill → hft.fills row dict."""
        return {
            "ts_exchange": fill.ts_ns,
            "ts_local": fill.ts_ns,
            "client_order_id": "",
            "broker_order_id": fill.broker_order_id,
            "fill_id": fill.fill_id,
            "strategy_id": "UNKNOWN",
            "symbol": fill.symbol,
            "side": fill.side,
            "qty": fill.qty,
            "price_scaled": fill.price_scaled,
            "fee_scaled": 0,
            "tax_scaled": 0,
            "decision_price": 0,
            "arrival_price": 0,
            "instrument_type": "",
            "oc_type": "",
            "source": "startup_reconciler",
        }
