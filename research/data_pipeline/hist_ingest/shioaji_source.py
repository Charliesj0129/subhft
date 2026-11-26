"""Shioaji source: historical ticks (api.ticks) and 1-minute K bars (api.kbars) of the continuous contract <product>R1.

Credentials come from the environment (SHIOAJI_API_KEY / SHIOAJI_SECRET_KEY, as in
feed_adapter/shioaji/session_runtime.py); the session is real, not simulation, like
scripts/backfill_historical_ticks.py. The SDK is imported lazily.

Ticks ``contract_code`` is the front month by the third-Wednesday rule for that trading day (holiday-shifted
settlements are not modelled); a mismatch with today's ``target_code`` is logged. Shioaji kbars keep
``target_code`` at fetch time, so for windows before the latest roll it names today's front month. Timestamp labels: see
model.py. Whether kbars ``end`` is inclusive and whether a kbars day holds the previous night session are unverified.
"""

from __future__ import annotations

import datetime as dt
import functools
import os
from collections import Counter
from collections.abc import Callable, Mapping, Sequence
from typing import Any

import structlog

from .model import (
    FAILED,
    PENDING_QUOTA,
    KbarRow,
    SourceError,
    TickRow,
    SHIOAJI_KBAR_NS,
    TAIPEI,
    classify_session,
    front_month_code,
    local_ns_to_utc_ns,
    scale_price,
    weekdays,
)
from .ratelimit import RateLimiter
from .store import HistStore, WindowResult, run_kbar_windows

log = structlog.get_logger("hist_ingest.shioaji")

SOURCE = "shioaji"
KBAR_CHUNK_DAYS = 27  # plus NIGHT_LOOKBACK_DAYS stays within the 30-day kbars limit
MAX_FAILURES = 3


class QuotaError(SourceError):
    """Shioaji refused a query because the data-usage quota is exhausted."""


class ShioajiSource:
    """A logged-in Shioaji ``api`` and one rate limiter (40 calls / 10 s) for ticks and kbars; usage is not limited."""

    def __init__(self, api: Any, *, limiter: RateLimiter | None = None, all_day: Any = None) -> None:
        self.api, self.limiter, self._all_day = api, limiter or RateLimiter(40, 10.0), all_day

    @classmethod
    def connect(cls) -> ShioajiSource:
        key, secret = os.getenv("SHIOAJI_API_KEY"), os.getenv("SHIOAJI_SECRET_KEY")
        if not key or not secret:
            raise SourceError("SHIOAJI_API_KEY and SHIOAJI_SECRET_KEY must be set")
        import shioaji as sj

        api = sj.Shioaji()
        try:
            api.login(api_key=key, secret_key=secret, subscribe_trade=False)
        except Exception as exc:
            raise SourceError(f"shioaji login failed: {type(exc).__name__}") from exc
        return cls(api, all_day=(getattr(sj, "TicksQueryType", None) or sj.constant.TicksQueryType).AllDay)

    def close(self) -> None:
        """Log out: Shioaji allows only 5 concurrent sessions per account."""
        try:
            self.api.logout()
        except Exception as exc:
            log.warning("shioaji_logout_failed", error=type(exc).__name__)

    def remaining_bytes(self) -> int:
        try:
            return int(self.api.usage().remaining_bytes)
        except Exception as exc:
            raise SourceError(f"shioaji usage query failed: {type(exc).__name__}") from exc

    def contract(self, product: str) -> tuple[Any, str, str]:
        """(contract, query_code, contract_code) for <product>R1; contract_code falls back to the query code."""
        query_code = f"{product}R1"
        contracts = getattr(self.api, "Contracts", None) or getattr(self.api, "contracts", None)
        try:
            contract = contracts.Futures[query_code]
        except (AttributeError, KeyError, IndexError, TypeError) as exc:
            raise SourceError(f"contract {query_code} not found: {type(exc).__name__}") from exc
        return contract, query_code, str(getattr(contract, "target_code", "") or query_code)

    def ticks(self, contract: Any, day: dt.date) -> Any:
        kwargs = {"contract": contract, "date": day.isoformat()}
        if self._all_day is not None:
            kwargs["query_type"] = self._all_day
        return self._query("ticks", self.api.ticks, kwargs)

    def kbars(self, contract: Any, start: dt.date, end: dt.date) -> Any:
        return self._query(
            "kbars", self.api.kbars, {"contract": contract, "start": start.isoformat(), "end": end.isoformat()}
        )

    def _query(self, name: str, fn: Callable[..., Any], kwargs: dict[str, Any]) -> Any:
        self.limiter.acquire()
        try:
            return fn(**kwargs)
        except Exception as exc:
            kind = QuotaError if "UsageLimit" in type(exc).__name__ else SourceError
            raise kind(f"shioaji {name} failed: {type(exc).__name__}") from exc


def _columns(raw: Any, names: Sequence[str], what: str) -> list[list[Any]]:
    """The ``ts`` column followed by ``names``; every column must be as long as ``ts``."""
    cols = [raw.get(n) if isinstance(raw, dict) else getattr(raw, n, None) for n in ("ts", *names)]
    if any(c is None or isinstance(c, (Mapping, str, bytes, bytearray, set, frozenset)) for c in cols):
        raise SourceError(f"{what} reply has missing or unusable columns")
    try:
        columns: list[list[Any]] = [list(c) for c in cols]
    except TypeError as exc:
        raise SourceError(f"{what} reply has non-iterable columns") from exc
    if any(len(c) != len(columns[0]) for c in columns):
        raise SourceError(f"{what} columns differ in length: {[len(c) for c in columns]}")
    return columns


def convert_ticks(
    raw: Any, product: str, query_code: str, contract_code: str, day: dt.date, ingest_ts: int
) -> list[TickRow]:
    """Shioaji Ticks to rows; seq is the position among ticks sharing one exch_ts, so a refetch that gains or loses a
    tick elsewhere in the day does not shift the keys of the others."""
    cols = _columns(raw, ("close", "volume", "bid_price", "ask_price", "tick_type"), "ticks")
    head = (SOURCE, product, query_code, contract_code)
    try:
        rows = []
        same_ts: Counter[int] = Counter()
        for ts, close, volume, bid, ask, tick_type in zip(*cols, strict=True):
            seq = same_ts[ts]
            same_ts[ts] += 1
            utc = local_ns_to_utc_ns(ts)
            prices = (scale_price(close), int(volume), scale_price(bid), scale_price(ask), int(tick_type))
            rows.append(TickRow(*head, day, classify_session(utc), utc, seq, *prices, ingest_ts))
    except (ArithmeticError, ValueError, TypeError) as exc:
        raise SourceError(f"unusable tick data: {type(exc).__name__}") from exc
    return rows


def convert_kbars(raw: Any, product: str, query_code: str, contract_code: str, ingest_ts: int) -> list[KbarRow]:
    cols = _columns(raw, ("Open", "High", "Low", "Close", "Volume"), "kbars")
    head = (SOURCE, product, query_code, contract_code)
    try:
        rows = []
        for ts, *ohlc, volume in zip(*cols, strict=True):
            utc = local_ns_to_utc_ns(ts) - SHIOAJI_KBAR_NS
            prices = [scale_price(p) for p in ohlc]
            rows.append(KbarRow(*head, 1, classify_session(utc), utc, *prices, int(volume), ingest_ts))
    except (ArithmeticError, ValueError, TypeError) as exc:
        raise SourceError(f"unusable kbar data: {type(exc).__name__}") from exc
    return rows


def ingest_ticks(
    src: ShioajiSource,
    store: HistStore,
    *,
    products: Sequence[str],
    since: dt.date,
    until: dt.date,
    min_remaining_bytes: int,
    max_calls: int,
    force: bool,
) -> Counter[str]:
    """Whole-day ticks per product, newest day first, until done or the call cap, quota floor or failures stop it."""
    stats: Counter[str] = Counter()
    for product in products:
        try:
            contract, query_code, code = src.contract(product)
        except SourceError as exc:
            log.error("contract_lookup_failed", product=product, error=str(exc))
            stats["failed_products"] += 1
            continue
        today_code = front_month_code(product, dt.datetime.now(TAIPEI).date())
        if code != query_code and code != today_code:
            log.warning("roll_rule_disagrees_with_target_code", product=product, target_code=code, rule=today_code)
        for day in store.pending_days(SOURCE, product, "ticks", weekdays(since, until)[::-1], force=force):
            code = front_month_code(product, day)
            mark = functools.partial(store.mark_days, SOURCE, product, "ticks", [day], contract_code=code)
            if stats["calls"] >= max_calls:
                log.warning("tick_call_cap_reached", cap=max_calls)
                return stats
            remaining = src.remaining_bytes()
            if remaining < min_remaining_bytes:
                mark(PENDING_QUOTA, usage_bytes_after=remaining, note="usage below floor before the call")
                stats[PENDING_QUOTA] += 1
                return stats
            stats["calls"] += 1
            try:
                rows = convert_ticks(src.ticks(contract, day), product, query_code, code, day, store.clock_ns())
            except QuotaError as exc:
                mark(PENDING_QUOTA, usage_bytes_after=remaining, note=str(exc))
                stats[PENDING_QUOTA] += 1
                return stats
            except SourceError as exc:
                mark(FAILED, note=str(exc)[:200])
                stats[FAILED] += 1
                if stats[FAILED] >= MAX_FAILURES:
                    return stats
                continue
            after = src.remaining_bytes()
            if not rows and after < min_remaining_bytes:
                mark(PENDING_QUOTA, usage_bytes_after=after, note="empty result with usage below floor")
                stats[PENDING_QUOTA] += 1
                return stats
            try:
                stats["rows"] += store.replace_days(
                    SOURCE, product, "ticks", [day], rows, contract_code=code, usage_bytes_after=after
                )
            except SourceError as exc:
                mark(FAILED, note=str(exc)[:200])
                stats[FAILED] += 1
                if stats[FAILED] >= MAX_FAILURES:
                    return stats
                continue
            stats["done" if rows else "empty"] += 1
    return stats


def _kbar_fetcher(
    src: ShioajiSource, product: str, floor: int, store: HistStore
) -> Callable[[dt.date, dt.date], WindowResult] | None:
    try:
        contract, query_code, code = src.contract(product)
    except SourceError as exc:
        log.error("contract_lookup_failed", product=product, error=str(exc))
        return None

    def fetch(start: dt.date, end: dt.date) -> WindowResult:
        remaining = src.remaining_bytes()
        if remaining < floor:
            return WindowResult([], code, PENDING_QUOTA, remaining, "usage below floor before the call", True)
        try:
            rows = convert_kbars(src.kbars(contract, start, end), product, query_code, code, store.clock_ns())
        except QuotaError as exc:
            return WindowResult([], code, PENDING_QUOTA, remaining, str(exc), True)
        after = src.remaining_bytes()
        if not rows and after < floor:
            return WindowResult([], code, PENDING_QUOTA, after, "empty result with usage below floor", True)
        return WindowResult(rows, code, None, after)

    return fetch


def ingest_kbars(
    src: ShioajiSource,
    store: HistStore,
    *,
    products: Sequence[str],
    since: dt.date,
    until: dt.date,
    min_remaining_bytes: int,
    force: bool,
) -> Counter[str]:
    """1-minute K bars per product in windows from the oldest pending weekday; quota trouble stops every product."""
    totals: Counter[str] = Counter()
    for product in products:
        fetch = _kbar_fetcher(src, product, min_remaining_bytes, store)
        if fetch is None:
            totals["failed_products"] += 1
            continue
        totals.update(
            run_kbar_windows(
                store,
                source=SOURCE,
                product=product,
                since=since,
                until=until,
                chunk_days=KBAR_CHUNK_DAYS,
                force=force,
                fetch=fetch,
            )
        )
        if totals["stopped"]:
            break
    return totals
