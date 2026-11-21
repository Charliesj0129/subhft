"""Fubon source: 1-minute K bars from rest_client.futopt.historical.candles (continuous month "1!").

Credentials come from the environment and mirror feed_adapter/fubon/session_runtime.py:
sdk.apikey_login(HFT_FUBON_PERSONAL_ID, HFT_FUBON_API_KEY, HFT_FUBON_CERT_PATH[, HFT_FUBON_CERT_PASSWORD]),
then sdk.init_realtime(). A relative cert path is resolved against the --env-file directory. The SDK is imported lazily, so this module loads without fubon_neo.

Both sessions ("regular", "afterhours") are fetched per window of at most 180 calendar days. The response
shape (a ``data`` list of dicts with date/open/high/low/close/volume, an optional delivery-month ``symbol``) is
UNVERIFIED without a live login, so the converter is strict about the fields it needs. Without a delivery-month
code in the response, contract_code is "" and query_code "1!". Timestamp labels: see model.py.
"""

from __future__ import annotations

import datetime as dt
import os
import re
import time
from collections import Counter
from collections.abc import Callable, Sequence
from typing import Any

import structlog

from .model import KbarRow, SourceError, classify_session, iso_to_utc_ns, scale_price
from .ratelimit import RateLimiter
from .store import HistStore, WindowResult, run_kbar_windows

log = structlog.get_logger("hist_ingest.fubon")

SOURCE = "fubon"
QUERY_CODE = "1!"
CHUNK_DAYS = 180  # the API wants a range shorter than one year
SESSIONS = ("regular", "afterhours")
MAX_RETRIES = 3
RETRY_SLEEP_S = 61.0


def _get(obj: Any, key: str) -> Any:
    return obj.get(key) if isinstance(obj, dict) else getattr(obj, key, None)


def _logout(sdk: Any) -> None:
    try:
        sdk.logout()
    except Exception as exc:
        log.warning("fubon_logout_failed", error=type(exc).__name__)


class FubonSource:
    """A logged-in Fubon SDK and its futopt REST client; at most 50 calls per 60 s."""

    def __init__(
        self, sdk: Any, rest: Any, *, limiter: RateLimiter | None = None, sleep: Callable[[float], None] = time.sleep
    ) -> None:
        self.sdk, self.rest, self.limiter, self._sleep = sdk, rest, limiter or RateLimiter(50, 60.0), sleep

    @classmethod
    def connect(cls) -> FubonSource:
        person = os.getenv("HFT_FUBON_PERSONAL_ID") or os.getenv("HFT_FUBON_ACCOUNT", "")
        key, password, cert = (
            os.getenv(n, "") for n in ("HFT_FUBON_API_KEY", "HFT_FUBON_CERT_PASSWORD", "HFT_FUBON_CERT_PATH")
        )
        if not person or not key or not cert:
            raise SourceError("HFT_FUBON_PERSONAL_ID, HFT_FUBON_API_KEY and HFT_FUBON_CERT_PATH must be set")
        try:
            from fubon_neo.sdk import FubonSDK
        except ImportError as exc:
            raise SourceError("fubon_neo is not installed") from exc
        sdk = FubonSDK()
        try:
            accounts = _apikey_login(sdk, person, key, cert, password)
            if accounts and getattr(accounts, "data", None):
                sdk.init_realtime()
                return cls(sdk, sdk.marketdata.rest_client.futopt)
        except Exception as exc:
            _logout(sdk)
            raise SourceError(f"fubon login failed: {type(exc).__name__}") from exc
        _logout(sdk)
        raise SourceError("fubon login returned no accounts")

    def close(self) -> None:
        _logout(self.sdk)

    def candles(self, product: str, session: str, start: dt.date, end: dt.date) -> Any:
        """One candles call; on HTTP 429 sleep 61 s and retry up to 3 times, then raise SourceError."""
        params = {"product": product, "contractMonth": QUERY_CODE, "timeframe": "1", "session": session}
        params |= {"from": start.isoformat(), "to": end.isoformat()}
        for attempt in range(MAX_RETRIES + 1):
            self.limiter.acquire()
            try:
                resp = self.rest.historical.candles(**params)
            except Exception as exc:
                if getattr(exc, "status_code", None) != 429:
                    raise SourceError(f"fubon candles failed: {type(exc).__name__}") from exc
            else:
                status = _get(resp, "statusCode")
                if status is None or 200 <= int(status) < 300:
                    return resp
                if int(status) != 429:
                    raise SourceError(f"fubon candles returned statusCode {status}")
            if attempt < MAX_RETRIES:
                log.warning("fubon_rate_limited", attempt=attempt + 1, sleep_s=RETRY_SLEEP_S)
                self._sleep(RETRY_SLEEP_S)
        raise SourceError(f"fubon rate limited (429) after {MAX_RETRIES} retries")


def convert_candles(resp: Any, *, product: str, ingest_ts: int) -> tuple[str, list[KbarRow]]:
    """(contract_code, rows). contract_code is the response symbol only if it is a delivery-month code, else ""."""
    symbol = str(_get(resp, "symbol") or "")
    code = symbol if re.fullmatch(rf"{re.escape(product)}[A-L]\d", symbol) else ""
    try:
        rows = []
        data = _get(resp, "data")
        if data is None:
            raise SourceError("fubon candles reply has no data field")
        for c in data:
            ts = iso_to_utc_ns(_get(c, "date"))
            prices = (scale_price(_get(c, k)) for k in ("open", "high", "low", "close"))
            rows.append(
                KbarRow(
                    SOURCE,
                    product,
                    QUERY_CODE,
                    code,
                    1,
                    classify_session(ts),
                    ts,
                    *prices,
                    int(_get(c, "volume")),
                    ingest_ts,
                )
            )
    except (ArithmeticError, ValueError, TypeError) as exc:
        raise SourceError(f"unusable fubon candle: {type(exc).__name__}") from exc
    return code, rows


def ingest_kbars(
    src: FubonSource, store: HistStore, *, products: Sequence[str], since: dt.date, until: dt.date, force: bool
) -> Counter[str]:
    """1-minute K bars per product in windows of at most 180 days from the oldest pending weekday."""
    totals: Counter[str] = Counter()
    for product in products:

        def fetch(start: dt.date, end: dt.date, product: str = product) -> WindowResult:
            rows: list[KbarRow] = []
            code = ""
            for session in SESSIONS:
                found, part = convert_candles(
                    src.candles(product, session, start, end), product=product, ingest_ts=store.clock_ns()
                )
                code, rows = code or found, rows + part
            return WindowResult(rows, code)

        totals.update(
            run_kbar_windows(
                store,
                source=SOURCE,
                product=product,
                since=since,
                until=until,
                chunk_days=CHUNK_DAYS,
                force=force,
                fetch=fetch,
            )
        )
    return totals


def _apikey_login(sdk: Any, person: str, key: str, cert: str, cert_pass: str) -> Any:
    """fubon_neo 2.5 apikey_login(personal_id, api_key, cert_path, cert_pass); without HFT_FUBON_CERT_PASSWORD the
    SDK default (personal_id) is used."""
    return sdk.apikey_login(person, key, cert, cert_pass) if cert_pass else sdk.apikey_login(person, key, cert)
