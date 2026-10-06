"""Official TAIFEX / TWSE reference data: a polite fetcher, a manifest, and parsers.

Why this exists: the locally recorded ``hft.market_data`` covers ~130 trading days and
cannot be reconciled against anything. TAIFEX and TWSE publish daily volumes, settlement
prices, open interest and institutional positions for free, back to 2015 and beyond, which
is the only way to enlarge the evidence budget for daily-frequency research and to audit
the recorder's volumes.

Fetch policy (encoded here and covered by tests; it is the user's explicit condition for
automating this at all):

* single-threaded, with a minimum spacing per source (``SOURCES``);
* an honest ``User-Agent``; ``robots.txt`` is read first and obeyed;
* **any sign of a block stops the whole run**: HTTP 403 / 429 / 503, or an HTML page where
  data was expected (a challenge page). The block is written to the manifest, the run exits
  non-zero, and nothing is retried, re-headered or re-routed. Resuming is a human decision;
* a hard cap on requests per run;
* every response is stored byte-for-byte with its URL, time and SHA-256 in a JSONL manifest,
  and a re-run skips what the manifest already holds, so the job can be resumed and audited.

Nothing here talks to ClickHouse or to the platform.
"""

from __future__ import annotations

import csv
import hashlib
import io
import json
import os
import time
import urllib.error
import urllib.request
import zipfile
from collections.abc import Callable, Iterable, Iterator, Mapping, Sequence
from dataclasses import asdict, dataclass
from datetime import UTC, date, datetime, timedelta
from html.parser import HTMLParser
from pathlib import Path, PurePosixPath
from typing import Any, Literal
from urllib import robotparser
from urllib.parse import urlencode, urlsplit
from zoneinfo import ZoneInfo

DEFAULT_ROOT = Path("research/data/official")
MANIFEST_NAME = "manifest.jsonl"
DEFAULT_MAX_REQUESTS = 200
DEFAULT_TIMEOUT_S = 120.0
USER_AGENT = "subhft-research-fetcher/1.0 (personal research; rate-limited; stops on any block)"

# HTTP statuses that mean "the site is telling us to stop". 404 is not one of them: it is the
# normal answer for a date with no trading.
BLOCK_STATUSES = frozenset({403, 429, 503})
REDIRECT_STATUSES = frozenset({301, 302, 303, 307, 308})
CHALLENGE_MARKERS = (
    b"cf-chl",
    b"challenge-platform",
    b"just a moment",
    b"attention required",
    b"cf-browser-verification",
    b"captcha",
)

Expect = Literal["json", "csv", "zip", "html", "text"]
BLOCKED_STATUS = "BLOCKED"
MAX_BLOCKED_BODY_BYTES = 65_536


@dataclass(frozen=True, slots=True)
class SourcePolicy:
    """One origin and the minimum gap between two requests to it."""

    name: str
    host: str
    min_interval_s: float


SOURCES: dict[str, SourcePolicy] = {
    "taifex_web": SourcePolicy("taifex_web", "www.taifex.com.tw", 60.0),
    "taifex_openapi": SourcePolicy("taifex_openapi", "openapi.taifex.com.tw", 10.0),
    "twse": SourcePolicy("twse", "www.twse.com.tw", 5.0),
}


class OfficialDataError(Exception):
    """Base class; everything raised on purpose by this module derives from it."""


class Blocked(OfficialDataError):
    """The site refused or challenged us. The run must stop; nothing may be retried."""

    def __init__(self, source: str, url: str, reason: str) -> None:
        super().__init__(f"BLOCKED by {source} at {url}: {reason}")
        self.source = source
        self.url = url
        self.reason = reason


class RequestBudgetExhausted(OfficialDataError):
    """The per-run request cap was reached; the run stops cleanly and can be resumed."""


class RobotsDisallowed(OfficialDataError):
    """robots.txt forbids this path for our user agent."""


@dataclass(frozen=True, slots=True)
class Response:
    status: int
    content_type: str
    body: bytes
    location: str = ""


Transport = Callable[[str, Mapping[str, str], bytes | None, float], Response]


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """Surface redirects as responses instead of following them to some other host."""

    def redirect_request(self, *args: Any, **kwargs: Any) -> None:  # noqa: D102 - stdlib signature
        return None


def urllib_transport(url: str, headers: Mapping[str, str], data: bytes | None, timeout_s: float) -> Response:
    """Default transport. HTTP errors are returned as responses so the caller can inspect them."""
    request = urllib.request.Request(url, data=data, headers=dict(headers))  # noqa: S310 - https URLs only, see fetch()
    opener = urllib.request.build_opener(_NoRedirect)
    try:
        with opener.open(request, timeout=timeout_s) as handle:  # noqa: S310
            return Response(handle.status, handle.headers.get("Content-Type", ""), handle.read())
    except urllib.error.HTTPError as exc:
        headers_ = exc.headers
        return Response(
            exc.code,
            headers_.get("Content-Type", "") if headers_ else "",
            exc.read() or b"",
            headers_.get("Location", "") if headers_ else "",
        )


def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


@dataclass(frozen=True, slots=True)
class ManifestEntry:
    key: str
    source: str
    url: str
    fetched_at: str
    status: str
    http_status: int
    sha256: str
    size_bytes: int
    path: str
    content_type: str
    note: str = ""


class Manifest:
    """Append-only JSONL record of every request made. Never rewritten."""

    def __init__(self, path: Path) -> None:
        self.path = path

    def entries(self) -> Iterator[ManifestEntry]:
        if not self.path.exists():
            return
        with self.path.open(encoding="utf-8") as handle:
            for line in handle:
                if line.strip():
                    yield ManifestEntry(**json.loads(line))

    def append(self, entry: ManifestEntry) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(asdict(entry), sort_keys=True, ensure_ascii=False) + "\n")

    def latest_ok(self, key: str) -> ManifestEntry | None:
        found: ManifestEntry | None = None
        for entry in self.entries():
            if entry.key == key and entry.status == "OK":
                found = entry
        return found

    def latest_missing(self, key: str) -> ManifestEntry | None:
        """Last 404 for ``key`` -- the normal answer for a date with no trading."""
        found: ManifestEntry | None = None
        for entry in self.entries():
            if entry.key == key and entry.status == "NOT_FOUND":
                found = entry
        return found

    def verify(self, root: Path) -> list[str]:
        """Re-hash every stored file. Returns one problem string per mismatch or missing file."""
        problems: list[str] = []
        for entry in self.entries():
            if entry.status != "OK":
                continue
            target = root / entry.path
            if not target.exists():
                problems.append(f"missing: {entry.path}")
            elif sha256_hex(target.read_bytes()) != entry.sha256:
                problems.append(f"sha256 mismatch: {entry.path}")
        return problems


def request_key(url: str, data: bytes | None = None, suffix: str = "") -> str:
    """Identity of a request: its URL, a digest of the POST body, and an optional snapshot tag.

    The tag exists for pages whose content changes daily (listings, "latest day" endpoints):
    without it the manifest would call tomorrow's fetch a cache hit.
    """
    key = url if data is None else f"{url}#{sha256_hex(data)[:16]}"
    return f"{key}@{suffix}" if suffix else key


def detect_block(response: Response, expect: Expect) -> str | None:
    """Return why a response is a block, or ``None``.

    A challenge page arrives as HTTP 200 HTML, so the status alone is not enough: when data
    (JSON / CSV / ZIP) was requested, an HTML body is a block.
    """
    if response.status in BLOCK_STATUSES:
        return f"HTTP {response.status}"
    head = response.body[:4096].lstrip().lower()
    looks_html = head.startswith((b"<!doctype html", b"<html")) or "text/html" in response.content_type.lower()
    if expect != "html" and response.status == 200 and looks_html:
        return "HTML page where data was expected (challenge page?)"
    if response.status == 200:
        lowered = response.body[:20_000].lower()
        if looks_html and any(marker in lowered for marker in CHALLENGE_MARKERS):
            return "challenge markers in the page"
    return None


def _safe_relpath(relpath: str) -> PurePosixPath:
    path = PurePosixPath(relpath)
    if path.is_absolute() or ".." in path.parts or not path.parts:
        raise ValueError(f"unsafe relative path: {relpath!r}")
    return path


@dataclass(frozen=True, slots=True)
class FetchResult:
    entry: ManifestEntry
    path: Path
    from_cache: bool


class Fetcher:
    """Sequential, rate-limited, block-aware downloader. One instance = one run."""

    def __init__(
        self,
        root: Path = DEFAULT_ROOT,
        *,
        transport: Transport = urllib_transport,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
        now: Callable[[], datetime] = lambda: datetime.now(UTC),
        max_requests: int = DEFAULT_MAX_REQUESTS,
        user_agent: str = USER_AGENT,
        timeout_s: float = DEFAULT_TIMEOUT_S,
        sources: Mapping[str, SourcePolicy] | None = None,
    ) -> None:
        self.root = root
        self.manifest = Manifest(root / MANIFEST_NAME)
        self._transport = transport
        self._clock = clock
        self._sleep = sleep
        self._now = now
        self.max_requests = max_requests
        self.user_agent = user_agent
        self.timeout_s = timeout_s
        self.sources = dict(sources or SOURCES)
        self.requests_made = 0
        self._last_request_at: dict[str, float] = {}
        self._robots: dict[str, robotparser.RobotFileParser | None] = {}

    # ------------------------------------------------------------------ robots

    def allowed_by_robots(self, source: str, url: str) -> bool:
        """Read ``/robots.txt`` once per source (as a normal, spaced, recorded request)."""
        if source not in self._robots:
            policy = self.sources[source]
            robots_url = f"https://{policy.host}/robots.txt"
            parser: robotparser.RobotFileParser | None = robotparser.RobotFileParser()
            try:
                result = self._request(source, robots_url, f"{source}/robots.txt", "html", None)
                text = result.path.read_text(encoding="utf-8", errors="replace")
                # A site without a robots.txt often answers 200 with an HTML error page: no rules.
                parser = None if text.lstrip().lower().startswith(("<!doctype", "<html")) else parser
                if parser is not None:
                    parser.parse(text.splitlines())
            except (_NotFound, _Redirected):
                parser = None  # no robots.txt (or the site redirects it elsewhere): no rules to obey
            self._robots[source] = parser
        parser = self._robots[source]
        return True if parser is None else parser.can_fetch(self.user_agent, url)

    # ------------------------------------------------------------------ fetch

    def fetch(
        self,
        source: str,
        url: str,
        relpath: str,
        expect: Expect,
        *,
        data: bytes | None = None,
        final_404: bool = False,
        key_suffix: str = "",
    ) -> FetchResult | None:
        """Download ``url`` into ``root/relpath`` unless the manifest already holds it.

        Returns ``None`` for a 404 (recorded; no file). ``final_404`` marks a 404 as the
        permanent answer, so a later run does not ask again (use it for past non-trading
        dates, never for a date that may simply not be published yet).
        """
        if source not in self.sources:
            raise ValueError(f"unknown source {source!r}")
        if not url.startswith("https://") or urlsplit(url).hostname != self.sources[source].host:
            raise ValueError(f"{url!r} is not an https URL on {self.sources[source].host}")
        key = request_key(url, data, key_suffix)
        cached = self.manifest.latest_ok(key)
        if cached is not None and (self.root / cached.path).exists():
            return FetchResult(cached, self.root / cached.path, True)
        if final_404 and self.manifest.latest_missing(key) is not None:
            return None
        if not self.allowed_by_robots(source, url):
            raise RobotsDisallowed(f"robots.txt of {self.sources[source].host} forbids {url}")
        try:
            return self._request(source, url, relpath, expect, data, key)
        except _NotFound:
            return None
        except _Redirected as exc:
            # A data URL that redirects is not what we asked for; challenge pages commonly arrive
            # this way, and telling them apart is not ours to guess. Stop.
            raise Blocked(source, url, f"unexpected redirect to {exc.location!r}") from exc

    # ------------------------------------------------------------------ internals

    def _request(
        self,
        source: str,
        url: str,
        relpath: str,
        expect: Expect,
        data: bytes | None,
        key: str | None = None,
    ) -> FetchResult:
        target = _safe_relpath(relpath)
        if self.requests_made >= self.max_requests:
            raise RequestBudgetExhausted(f"{self.max_requests} requests made; resume the run to continue")
        self._wait_turn(source)
        headers = {"User-Agent": self.user_agent, "Accept": "*/*"}
        response = self._transport(url, headers, data, self.timeout_s)
        self._last_request_at[source] = self._clock()
        self.requests_made += 1
        key = key if key is not None else request_key(url, data)
        stamp = self._now().isoformat()

        def record(status: str, *, path: str = "", sha: str = "", note: str = "") -> ManifestEntry:
            entry = ManifestEntry(
                key=key,
                source=source,
                url=url,
                fetched_at=stamp,
                status=status,
                http_status=response.status,
                sha256=sha,
                size_bytes=len(response.body),
                path=path,
                content_type=response.content_type,
                note=note,
            )
            self.manifest.append(entry)
            return entry

        reason = detect_block(response, expect)
        if reason is not None:
            record(BLOCKED_STATUS, note=f"{reason}; body kept at {self._keep_blocked_body(response)}")
            raise Blocked(source, url, reason)
        if response.status in REDIRECT_STATUSES:
            record("REDIRECT", note=f"-> {response.location}")
            raise _Redirected(url, response.location)
        if response.status == 404:
            record("NOT_FOUND")
            raise _NotFound(url)
        if response.status != 200:
            record("ERROR", note=f"unexpected HTTP {response.status}")
            raise OfficialDataError(f"HTTP {response.status} from {url}")

        if expect == "zip" and not response.body.startswith(b"PK"):
            record("ERROR", note="body is not a zip archive")
            raise OfficialDataError(f"{url} did not return a zip archive")

        destination = self.root / target
        destination.parent.mkdir(parents=True, exist_ok=True)
        partial = destination.with_name(destination.name + ".part")
        partial.write_bytes(response.body)
        os.replace(partial, destination)
        entry = record("OK", path=str(target), sha=sha256_hex(response.body))
        return FetchResult(entry, destination, False)

    def _keep_blocked_body(self, response: Response) -> str:
        """Save (the start of) a blocked response so a person can tell a challenge from an error page."""
        body = response.body[:MAX_BLOCKED_BODY_BYTES]
        relpath = f"blocked/{sha256_hex(body)[:16]}.html"
        destination = self.root / relpath
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(body)
        return relpath

    def _wait_turn(self, source: str) -> None:
        last = self._last_request_at.get(source)
        if last is None:
            return
        remaining = self.sources[source].min_interval_s - (self._clock() - last)
        if remaining > 0:
            self._sleep(remaining)


class _NotFound(OfficialDataError):
    """Internal: a 404 that has already been written to the manifest."""


class _Redirected(OfficialDataError):
    """Internal: a 3xx that has already been written to the manifest."""

    def __init__(self, url: str, location: str) -> None:
        super().__init__(f"{url} redirects to {location}")
        self.location = location


def decode_text(body: bytes, encodings: Sequence[str] = ("utf-8-sig", "cp950")) -> str:
    """Decode strictly, trying UTF-8 then Big5/CP950. Never substitutes replacement characters."""
    for encoding in encodings:
        try:
            return body.decode(encoding)
        except UnicodeDecodeError:
            continue
    raise OfficialDataError(f"body is not valid {'/'.join(encodings)}")


# ---------------------------------------------------------------------------
# Probe: the smallest request set that shows what each source serves today.
# ---------------------------------------------------------------------------

PROBE_REQUESTS: tuple[tuple[str, str, str, Expect], ...] = (
    ("taifex_openapi", "https://openapi.taifex.com.tw/swagger.json", "probe/taifex_openapi_swagger.json", "json"),
    (
        "taifex_openapi",
        "https://openapi.taifex.com.tw/v1/DailyMarketReportFut",
        "probe/taifex_openapi_DailyMarketReportFut.json",
        "json",
    ),
    (
        "taifex_web",
        "https://www.taifex.com.tw/cht/3/futPrevious30DaysSalesData",
        "probe/taifex_futPrevious30DaysSalesData.html",
        "html",
    ),
    (
        "taifex_web",
        "https://www.taifex.com.tw/cht/3/optPrevious30DaysSalesData",
        "probe/taifex_optPrevious30DaysSalesData.html",
        "html",
    ),
    (
        "taifex_web",
        "https://www.taifex.com.tw/cht/3/dlFutDailyMarketView",
        "probe/taifex_dlFutDailyMarketView.html",
        "html",
    ),
    (
        "taifex_web",
        "https://www.taifex.com.tw/cht/3/dlOptDailyMarketView",
        "probe/taifex_dlOptDailyMarketView.html",
        "html",
    ),
    (
        "twse",
        "https://www.twse.com.tw/rwd/en/afterTrading/STOCK_DAY?date=20260901&stockNo=2330&response=json",
        "probe/twse_STOCK_DAY_2330_202609.json",
        "json",
    ),
)


def probe(fetcher: Fetcher) -> list[dict[str, Any]]:
    """Fetch ``robots.txt`` plus one sample per source and report what came back.

    Stops at the first block (the exception propagates).
    """
    report: list[dict[str, Any]] = []
    for source, url, relpath, expect in PROBE_REQUESTS:
        if not fetcher.allowed_by_robots(source, url):
            report.append({"source": source, "url": url, "result": "robots.txt disallows"})
            continue
        result = fetcher.fetch(source, url, relpath, expect)
        report.append(
            {
                "source": source,
                "url": url,
                "result": "not found" if result is None else "ok",
                "bytes": 0 if result is None else result.entry.size_bytes,
                "sha256": "" if result is None else result.entry.sha256,
                "path": "" if result is None else str(result.path),
            }
        )
    return report


# ---------------------------------------------------------------------------
# Parsers. Pure functions over bytes; they never touch the network or the manifest.
# ---------------------------------------------------------------------------

_NULLS = frozenset({"", "-", "--", "---", "NULL", "null", "N/A", "X"})
SESSION_NAMES = {"一般": "regular", "盤後": "after_hours"}
RIGHT_NAMES = {"買權": "C", "賣權": "P", "Call": "C", "Put": "P", "call": "C", "put": "P"}


def parse_number(text: Any) -> float | None:
    """``'1,234.5'`` / ``'+35.00'`` -> float; the exchange's placeholders -> ``None``."""
    if text is None:
        return None
    raw = str(text).strip().replace(",", "")
    if raw in _NULLS:
        return None
    try:
        return float(raw.rstrip("%"))
    except ValueError:
        return None


def parse_int(text: Any) -> int | None:
    value = parse_number(text)
    return None if value is None else int(round(value))


def iso_date(text: str) -> str:
    """``'20261005'`` or ``'2026/10/05'`` -> ``'2026-10-05'``; anything else is an error."""
    digits = text.strip().replace("/", "").replace("-", "")
    if len(digits) != 8 or not digits.isdigit():
        raise OfficialDataError(f"not a date: {text!r}")
    return f"{digits[0:4]}-{digits[4:6]}-{digits[6:8]}"


def _json_list(body: bytes) -> list[dict[str, Any]]:
    payload = json.loads(decode_text(body))
    if not isinstance(payload, list) or not all(isinstance(item, dict) for item in payload):
        raise OfficialDataError("expected a JSON array of objects")
    return payload


def parse_openapi_daily_market(body: bytes, *, options: bool) -> list[dict[str, Any]]:
    """``/DailyMarketReportFut`` and ``/DailyMarketReportOpt``: one row per contract-month (-strike) and session.

    The endpoint takes no date parameter and serves only the latest trading day, so history
    has to come from the CSV downloads or from snapshotting every day. The ``after_hours``
    rows are the night session that *belongs to* this trading date.
    """
    rows: list[dict[str, Any]] = []
    for item in _json_list(body):
        session = SESSION_NAMES.get(str(item.get("TradingSession", "")).strip())
        if session is None:
            raise OfficialDataError(f"unknown TradingSession {item.get('TradingSession')!r}")
        row: dict[str, Any] = {
            "date": iso_date(str(item["Date"])),
            "contract": str(item["Contract"]).strip(),
            "expiry": str(item["ContractMonth(Week)"]).strip(),
            "session": session,
            "open": parse_number(item.get("Open")),
            "high": parse_number(item.get("High")),
            "low": parse_number(item.get("Low")),
            "last": parse_number(item.get("Close" if options else "Last")),
            "volume": parse_int(item.get("Volume")),
            "settlement": parse_number(item.get("SettlementPrice")),
            "open_interest": parse_int(item.get("OpenInterest")),
            "best_bid": parse_number(item.get("BestBid")),
            "best_ask": parse_number(item.get("BestAsk")),
        }
        if options:
            right = RIGHT_NAMES.get(str(item.get("CallPut", "")).strip())
            if right is None:
                raise OfficialDataError(f"unknown CallPut {item.get('CallPut')!r}")
            row["strike"] = parse_number(item.get("StrikePrice"))
            row["right"] = right
        else:
            row["change"] = parse_number(item.get("Change"))
        rows.append(row)
    return rows


INSTITUTIONAL_FIELDS = {
    "TradingVolume(Long)": "volume_long",
    "TradingVolume(Short)": "volume_short",
    "TradingVolume(Net)": "volume_net",
    "OpenInterest(Long)": "oi_long",
    "OpenInterest(Short)": "oi_short",
    "OpenInterest(Net)": "oi_net",
}


def parse_openapi_institutional(body: bytes) -> list[dict[str, Any]]:
    """三大法人 per contract (futures or options): volumes and open interest by trader type."""
    rows: list[dict[str, Any]] = []
    for item in _json_list(body):
        row: dict[str, Any] = {
            "date": iso_date(str(item["Date"])),
            "contract": str(item["ContractCode"]).strip(),
            "trader": str(item["Item"]).strip(),
        }
        for source, name in INSTITUTIONAL_FIELDS.items():
            row[name] = parse_int(item.get(source))
        rows.append(row)
    return rows


STOCK_DAY_COLUMNS = [
    "Date",
    "Trade Volume",
    "Trade Value",
    "Opening Price",
    "Highest Price",
    "Lowest Price",
    "Closing Price",
    "Change",
    "Transaction",
]


def parse_twse_stock_day(body: bytes) -> list[dict[str, Any]]:
    """TWSE ``STOCK_DAY`` (one stock, one month, English endpoint): daily OHLC and turnover."""
    payload = json.loads(decode_text(body))
    if not isinstance(payload, dict) or payload.get("stat") != "OK":
        stat = payload.get("stat") if isinstance(payload, dict) else payload
        raise OfficialDataError(f"TWSE did not return stat OK: {stat!r}")
    fields = list(payload.get("fields") or ())
    if fields[: len(STOCK_DAY_COLUMNS)] != STOCK_DAY_COLUMNS:
        raise OfficialDataError(f"unexpected STOCK_DAY columns: {fields}")
    rows = []
    for record in payload.get("data") or ():
        rows.append(
            {
                "date": iso_date(str(record[0])),
                "volume": parse_int(record[1]),
                "value": parse_int(record[2]),
                "open": parse_number(record[3]),
                "high": parse_number(record[4]),
                "low": parse_number(record[5]),
                "close": parse_number(record[6]),
                "change": parse_number(str(record[7]).lstrip("X")),
                "transactions": parse_int(record[8]),
            }
        )
    return rows


# ---------------------------------------------------------------------------
# TAIFEX file datasets
# ---------------------------------------------------------------------------

TAIPEI = ZoneInfo("Asia/Taipei")

# TAIFEX publishes the last 30 trading days of every trade as a daily zip, linked from a
# listing page that changes every day. Older days are not on the free site (they are sold).
TICK_LISTINGS: dict[str, str] = {
    "fut_ticks": "https://www.taifex.com.tw/cht/3/futPrevious30DaysSalesData",
    "opt_ticks": "https://www.taifex.com.tw/cht/3/optPrevious30DaysSalesData",
}


def today_taipei(now: Callable[[], datetime] = lambda: datetime.now(UTC)) -> str:
    return now().astimezone(TAIPEI).date().isoformat()


class _ClickTargets(HTMLParser):
    """Collect the ``onclick`` attribute of every ``<input>``: the listing page's download buttons."""

    def __init__(self) -> None:
        super().__init__()
        self.targets: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag == "input":
            for name, value in attrs:
                if name == "onclick" and value:
                    self.targets.append(value)


def tick_links(listing_html: bytes) -> dict[str, str]:
    """Trading date (ISO) -> URL of that day's CSV zip, from a 30-day listing page."""
    collector = _ClickTargets()
    collector.feed(decode_text(listing_html))
    links: dict[str, str] = {}
    for target in collector.targets:
        parts = target.split("'")
        if len(parts) < 3 or not parts[1].lower().endswith(".zip") or "CSV" not in parts[1]:
            continue
        stem = PurePosixPath(urlsplit(parts[1]).path).stem  # Daily_2026_10_06 / OptionsDaily_2026_10_06
        day = "-".join(stem.split("_")[-3:])
        links[iso_date(day)] = parts[1]
    return links


def fetch_ticks(
    fetcher: Fetcher,
    dataset: str,
    date_from: str,
    date_to: str,
    *,
    today: str,
) -> list[FetchResult]:
    """Download the CSV zips of every listed trading day in ``[date_from, date_to]``."""
    listing_url = TICK_LISTINGS[dataset]
    listing = fetcher.fetch(
        "taifex_web", listing_url, f"taifex/listings/{dataset}_{today}.html", "html", key_suffix=today
    )
    if listing is None:
        raise OfficialDataError(f"listing page {listing_url} not found")
    links = tick_links(listing.path.read_bytes())
    results: list[FetchResult] = []
    for day in sorted(links):
        if date_from <= day <= date_to:
            result = fetcher.fetch("taifex_web", links[day], f"taifex/{dataset}/{day}.zip", "zip")
            if result is not None:
                results.append(result)
    return results


# TAIFEX "daily market" history. The download page posts a form: ``down_type=2`` returns one
# zip for a whole year (every commodity), ``down_type=1`` a CSV for a date range of at most
# one calendar month (the page's own script enforces the same limit).
HISTORY_ENDPOINTS: dict[str, str] = {
    "fut_daily": "https://www.taifex.com.tw/cht/3/futDataDown",
    "opt_daily": "https://www.taifex.com.tw/cht/3/optDataDown",
}
HISTORY_FIRST_YEAR = {"fut_daily": 1998, "opt_daily": 2001}


def _post_body(fields: Mapping[str, str]) -> bytes:
    return urlencode(fields).encode("ascii")


def fetch_history_year(fetcher: Fetcher, dataset: str, year: int) -> FetchResult | None:
    """One zip holding every commodity's daily rows for ``year`` (complete years only)."""
    if year < HISTORY_FIRST_YEAR[dataset]:
        raise ValueError(f"{dataset} history starts in {HISTORY_FIRST_YEAR[dataset]}")
    body = _post_body({"down_type": "2", "his_year": str(year)})
    return fetcher.fetch(
        "taifex_web", HISTORY_ENDPOINTS[dataset], f"taifex/{dataset}/year_{year}.zip", "zip", data=body
    )


def month_windows(date_from: str, date_to: str) -> list[tuple[str, str]]:
    """Split ``[date_from, date_to]`` into calendar-month windows: the site's one-month limit."""
    start = date.fromisoformat(date_from)
    end = date.fromisoformat(date_to)
    windows: list[tuple[str, str]] = []
    while start <= end:
        next_month = (start.replace(day=1) + timedelta(days=32)).replace(day=1)
        stop = min(end, next_month - timedelta(days=1))
        windows.append((start.isoformat(), stop.isoformat()))
        start = next_month
    return windows


def fetch_history_range(
    fetcher: Fetcher,
    dataset: str,
    date_from: str,
    date_to: str,
    *,
    commodity: str = "all",
    today: str,
) -> list[FetchResult]:
    """CSV of daily rows for ``commodity`` (``all`` = every commodity), one request per month.

    A window that reaches ``today`` is not final (the day may not be published yet), so its
    manifest key carries ``today`` and is fetched again on a later day.
    """
    results: list[FetchResult] = []
    for first, last in month_windows(date_from, date_to):
        body = _post_body(
            {
                "down_type": "1",
                "commodity_id": commodity,
                "commodity_id2": "",
                "queryStartDate": first.replace("-", "/"),
                "queryEndDate": last.replace("-", "/"),
            }
        )
        result = fetcher.fetch(
            "taifex_web",
            HISTORY_ENDPOINTS[dataset],
            f"taifex/{dataset}_range/{commodity}_{first}_{last}.csv",
            "csv",
            data=body,
            key_suffix=today if last >= today else "",
        )
        if result is not None:
            results.append(result)
    return results


# ---------------------------------------------------------------------------
# Tick-by-tick files (last 30 trading days)
# ---------------------------------------------------------------------------

FUT_TICK_HEADER = [
    "成交日期",
    "商品代號",
    "到期月份(週別)",
    "成交時間",
    "成交價格",
    "成交數量(B+S)",
    "近月價格",
    "遠月價格",
    "開盤集合競價",
]
OPT_TICK_HEADER = [
    "成交日期",
    "商品代號",
    "履約價格",
    "到期月份(週別)",
    "買賣權別",
    "成交時間",
    "成交價格",
    "成交數量(B or S)",
    "開盤集合競價",
]

# Session windows in seconds after midnight. A trade stamped before 05:00 belongs to the night
# session that started the previous evening; the date column is the calendar date of the trade,
# not the trading day, which is what the file name carries.
DAY_OPEN_S = 8 * 3600 + 45 * 60
DAY_CLOSE_S = 13 * 3600 + 45 * 60
NIGHT_OPEN_S = 15 * 3600
NIGHT_CLOSE_S = 5 * 3600


def _seconds(hhmmss: str) -> int:
    text = hhmmss.strip().zfill(6)
    if len(text) != 6 or not text.isdigit():
        raise OfficialDataError(f"not a HHMMSS time: {hhmmss!r}")
    hours, minutes, seconds = int(text[0:2]), int(text[2:4]), int(text[4:6])
    if hours > 23 or minutes > 59 or seconds > 59:
        raise OfficialDataError(f"not a HHMMSS time: {hhmmss!r}")
    return hours * 3600 + minutes * 60 + seconds


def tick_session(trading_day: str, trade_date: str, second_of_day: int) -> str:
    """``day`` / ``night`` / ``other`` for a trade, given the file's trading day (ISO) and the row's date.

    The night session a file carries is the one that *precedes* its trading day: it opens at
    15:00 on an earlier calendar date and runs to 05:00 on a later one, which for a Monday file
    is Friday 15:00 to Saturday 05:00 (two calendar dates, both before the trading day).
    """
    if trade_date == trading_day:
        if DAY_OPEN_S <= second_of_day <= DAY_CLOSE_S:
            return "day"
        if second_of_day <= NIGHT_CLOSE_S:
            return "night"
        return "other"
    if trade_date < trading_day and (second_of_day >= NIGHT_OPEN_S or second_of_day <= NIGHT_CLOSE_S):
        return "night"
    return "other"


def parse_tick_csv(text: str, *, options: bool) -> Iterator[dict[str, Any]]:
    """Rows of a TAIFEX tick file (Big5 CSV already decoded). Raises if the header is not the known one.

    Futures quantities are ``B+S``: each contract traded is counted once for the buyer and once
    for the seller, so contracts = quantity / 2 (an odd value is a format surprise and raises).
    Options quantities are ``B or S`` and are contracts as they stand.
    """
    reader = csv.reader(io.StringIO(text))
    header = [cell.strip() for cell in next(reader, [])]
    expected = OPT_TICK_HEADER if options else FUT_TICK_HEADER
    if header != expected:
        raise OfficialDataError(f"unexpected tick file header: {header}")
    for record in reader:
        cells = [cell.strip() for cell in record]
        if len(cells) < len(expected) or not cells[0] or set(cells[0]) <= {"-"}:
            continue  # blank line or the dashed separator under the header
        if options:
            date_text, product, strike, expiry, right, time_text, price, quantity, auction = cells[:9]
            row: dict[str, Any] = {
                "date": iso_date(date_text),
                "product": product,
                "strike": parse_number(strike),
                "expiry": expiry,
                "right": right,
                "second": _seconds(time_text),
                "price": parse_number(price),
                "contracts": parse_int(quantity),
                "auction": auction == "*",
            }
        else:
            date_text, product, expiry, time_text, price, quantity, near, far, auction = cells[:9]
            both_sides = parse_int(quantity)
            if both_sides is None or both_sides % 2:
                raise OfficialDataError(f"futures B+S quantity must be even, got {quantity!r}")
            row = {
                "date": iso_date(date_text),
                "product": product,
                "expiry": expiry,
                "second": _seconds(time_text),
                "price": parse_number(price),
                "contracts": both_sides // 2,
                "near_price": parse_number(near),
                "far_price": parse_number(far),
                "auction": auction == "*",
                "spread": "/" in expiry,
            }
        yield row


def read_tick_zip(path: Path) -> str:
    """Decode the single CSV inside a tick zip. Refuses archives that are not what we expect."""
    with zipfile.ZipFile(path) as archive:
        members = archive.infolist()
        if len(members) != 1 or members[0].file_size > MAX_TICK_CSV_BYTES:
            raise OfficialDataError(f"{path.name}: expected one CSV under {MAX_TICK_CSV_BYTES} bytes")
        name = PurePosixPath(members[0].filename)
        if name.is_absolute() or ".." in name.parts or name.suffix.lower() != ".csv":
            raise OfficialDataError(f"{path.name}: unexpected member {members[0].filename!r}")
        return decode_text(archive.read(members[0]))


MAX_TICK_CSV_BYTES = 1_000_000_000


def summarize_ticks(rows: Iterable[Mapping[str, Any]], trading_day: str) -> list[dict[str, Any]]:
    """Contracts and trades per (product, expiry, session), the figures the official volume reconciles to."""
    totals: dict[tuple[str, str, str, bool], list[int]] = {}
    for row in rows:
        session = tick_session(trading_day, row["date"], row["second"])
        bucket = totals.setdefault((row["product"], row["expiry"], session, bool(row.get("spread", False))), [0, 0])
        bucket[0] += int(row["contracts"] or 0)
        bucket[1] += 1
    return [
        {"product": product, "expiry": expiry, "session": session, "spread": spread, "contracts": c, "trades": t}
        for (product, expiry, session, spread), (c, t) in sorted(totals.items())
    ]


# ---------------------------------------------------------------------------
# Snapshot and per-stock datasets
# ---------------------------------------------------------------------------

OPENAPI_BASE = "https://openapi.taifex.com.tw/v1"
# Endpoints that serve only the latest trading day: history is whatever we snapshot daily.
OPENAPI_SNAPSHOTS: tuple[str, ...] = (
    "DailyMarketReportFut",
    "DailyMarketReportOpt",
    "MarketDataOfMajorInstitutionalTradersDetailsOfFuturesContractsBytheDate",
    "MarketDataOfMajorInstitutionalTradersDetailsOfOptionsContractsBytheDate",
    "PutCallRatio",
)
TWSE_STOCK_DAY_URL = "https://www.twse.com.tw/rwd/en/afterTrading/STOCK_DAY?date={ym}01&stockNo={stock}&response=json"


def fetch_openapi_snapshots(fetcher: Fetcher, *, today: str) -> list[FetchResult]:
    """Today's copy of every snapshot endpoint, tagged with the fetch date."""
    results: list[FetchResult] = []
    for name in OPENAPI_SNAPSHOTS:
        result = fetcher.fetch(
            "taifex_openapi",
            f"{OPENAPI_BASE}/{name}",
            f"openapi/{name}/{today}.json",
            "json",
            key_suffix=today,
        )
        if result is not None:
            results.append(result)
    return results


def months_between(date_from: str, date_to: str) -> list[str]:
    """``YYYYMM`` for every calendar month touched by ``[date_from, date_to]``."""
    return [first[:7].replace("-", "") for first, _ in month_windows(date_from, date_to)]


def fetch_twse_stock_day(
    fetcher: Fetcher,
    stocks: Sequence[str],
    date_from: str,
    date_to: str,
    *,
    today: str,
) -> list[FetchResult]:
    """One request per (stock, month). The current month is refreshed on later days."""
    results: list[FetchResult] = []
    this_month = today[:7].replace("-", "")
    for stock in stocks:
        if not stock.isdigit():
            raise ValueError(f"not a stock number: {stock!r}")
        for ym in months_between(date_from, date_to):
            result = fetcher.fetch(
                "twse",
                TWSE_STOCK_DAY_URL.format(ym=ym, stock=stock),
                f"twse/stock_day/{stock}/{ym}.json",
                "json",
                key_suffix=today if ym == this_month else "",
            )
            if result is not None:
                results.append(result)
    return results


# ---------------------------------------------------------------------------
# Parsing stored files to Parquet, and reconciling ticks with the official daily volume
# ---------------------------------------------------------------------------

PARSE_DATASETS = ("fut_ticks", "opt_ticks", "openapi", "twse_stock_day")
PARQUET_BATCH_ROWS = 200_000
_OPENAPI_PARSERS: dict[str, Callable[[bytes], list[dict[str, Any]]]] = {
    "DailyMarketReportFut": lambda body: parse_openapi_daily_market(body, options=False),
    "DailyMarketReportOpt": lambda body: parse_openapi_daily_market(body, options=True),
    "MarketDataOfMajorInstitutionalTradersDetailsOfFuturesContractsBytheDate": parse_openapi_institutional,
    "MarketDataOfMajorInstitutionalTradersDetailsOfOptionsContractsBytheDate": parse_openapi_institutional,
}
_OFFICIAL_SESSION_TO_TICK = {"regular": "day", "after_hours": "night"}


def write_parquet(rows: Iterable[Mapping[str, Any]], path: Path, *, batch_rows: int = PARQUET_BATCH_ROWS) -> int:
    """Stream ``rows`` into one Parquet file (atomic rename). Returns the row count; writes nothing if empty."""
    import pyarrow as pa
    import pyarrow.parquet as pq

    path.parent.mkdir(parents=True, exist_ok=True)
    partial = path.with_name(path.name + ".partial")
    writer: Any = None
    count = 0
    batch: list[Mapping[str, Any]] = []

    def flush() -> None:
        nonlocal writer, count
        if not batch:
            return
        table = pa.Table.from_pylist(batch)
        if writer is None:
            writer = pq.ParquetWriter(partial, table.schema)
        writer.write_table(table.cast(writer.schema))
        count += len(batch)
        batch.clear()

    try:
        for row in rows:
            batch.append(row)
            if len(batch) >= batch_rows:
                flush()
        flush()
    finally:
        if writer is not None:
            writer.close()
    if count:
        partial.replace(path)
    else:
        partial.unlink(missing_ok=True)
    return count


def _tick_rows_with_session(rows: Iterable[Mapping[str, Any]], trading_day: str) -> Iterator[dict[str, Any]]:
    for row in rows:
        yield {**row, "trading_day": trading_day, "session": tick_session(trading_day, row["date"], row["second"])}


def parse_stored(root: Path, dataset: str) -> list[Path]:
    """Parse every verified file of ``dataset`` in the manifest into ``<root>/parsed``.

    Only manifest entries with status OK whose sha256 still matches are read, so a file edited
    after download cannot reach the parsed layer.
    """
    manifest = Manifest(root / "manifest.jsonl")
    written: list[Path] = []
    seen: set[str] = set()
    for entry in manifest.entries():
        if entry.status != "OK" or entry.path in seen:
            continue
        pure = PurePosixPath(entry.path)
        source = root / entry.path
        if dataset in TICK_LISTINGS and pure.parts[:2] == ("taifex", dataset) and pure.suffix == ".zip":
            seen.add(entry.path)
            _verify_stored(source, entry)
            options = dataset == "opt_ticks"
            trading_day = pure.stem
            rows = list(parse_tick_csv(read_tick_zip(source), options=options))
            target = root / "parsed" / dataset / f"{trading_day}.parquet"
            write_parquet(_tick_rows_with_session(rows, trading_day), target)
            summary = root / "parsed" / dataset / f"{trading_day}.summary.json"
            summary.write_text(json.dumps(summarize_ticks(rows, trading_day), indent=1, sort_keys=True), "utf-8")
            written.append(target)
        elif dataset == "openapi" and pure.parts[:1] == ("openapi",) and pure.suffix == ".json":
            parser = _OPENAPI_PARSERS.get(pure.parts[1])
            if parser is None:
                continue
            seen.add(entry.path)
            _verify_stored(source, entry)
            target = root / "parsed" / "openapi" / pure.parts[1] / f"{pure.stem}.parquet"
            if write_parquet(parser(source.read_bytes()), target):
                written.append(target)
        elif dataset == "twse_stock_day" and pure.parts[:2] == ("twse", "stock_day") and pure.suffix == ".json":
            seen.add(entry.path)
            _verify_stored(source, entry)
            rows = [{"stock": pure.parts[2], **row} for row in parse_twse_stock_day(source.read_bytes())]
            target = root / "parsed" / "twse_stock_day" / pure.parts[2] / f"{pure.stem}.parquet"
            if write_parquet(rows, target):
                written.append(target)
    return written


def _verify_stored(path: Path, entry: ManifestEntry) -> None:
    if not path.exists() or sha256_hex(path.read_bytes()) != entry.sha256:
        raise OfficialDataError(f"{entry.path}: missing or changed since it was downloaded (sha256 mismatch)")


def reconcile_volume(
    tick_summary: Sequence[Mapping[str, Any]],
    official_rows: Sequence[Mapping[str, Any]],
    *,
    contract: str,
) -> list[dict[str, Any]]:
    """Per (expiry, session): contracts from the tick file against the official daily volume.

    Two conventions of the official figure, both matched to the contract on 2026-10-01 (TX day
    34,458 / night 28,717) and 2026-10-05 (every month and spread, to the lot):

    * a calendar spread (``202610/202611``) is one tick row whose quantity is twice the official
      spread volume, so ``spread contracts = file contracts / 2``;
    * the official volume of a contract month *includes* the legs of spreads, so each spread's
      volume is added to both of its months.

    ``ratio`` is ticks / official.
    """
    ticks: dict[tuple[str, str], float] = {}
    for item in tick_summary:
        if item["product"] != contract:
            continue
        session = str(item["session"])
        contracts = float(item["contracts"])
        if item["spread"]:
            contracts /= 2
            ticks[(str(item["expiry"]), session)] = ticks.get((str(item["expiry"]), session), 0.0) + contracts
            for leg in str(item["expiry"]).split("/"):
                ticks[(leg, session)] = ticks.get((leg, session), 0.0) + contracts
        else:
            key = (str(item["expiry"]), session)
            ticks[key] = ticks.get(key, 0.0) + contracts
    official: dict[tuple[str, str], int] = {}
    for row in official_rows:
        if row["contract"] == contract and row.get("volume") is not None:
            key = (str(row["expiry"]), _OFFICIAL_SESSION_TO_TICK.get(str(row["session"]), str(row["session"])))
            official[key] = official.get(key, 0) + int(row["volume"])
    out = []
    for key in sorted(set(ticks) | set(official)):
        tick_value = ticks.get(key, 0.0)
        tick_value = int(tick_value) if tick_value == int(tick_value) else tick_value
        official_value = official.get(key, 0)
        out.append(
            {
                "expiry": key[0],
                "session": key[1],
                "ticks": tick_value,
                "official": official_value,
                "difference": tick_value - official_value,
                "ratio": (tick_value / official_value) if official_value else None,
            }
        )
    return out


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

EXIT_BLOCKED = 3
EXIT_BUDGET = 4
FETCH_DATASETS = (
    "fut_ticks",
    "opt_ticks",
    "fut_daily_year",
    "opt_daily_year",
    "fut_daily_range",
    "opt_daily_range",
    "openapi",
    "twse_stock_day",
)


def add_arguments(parser: Any) -> None:
    parser.add_argument("action", choices=("probe", "fetch", "parse", "verify", "reconcile"))
    parser.add_argument("--root", default=str(DEFAULT_ROOT))
    parser.add_argument("--max-requests", type=int, default=DEFAULT_MAX_REQUESTS)
    parser.add_argument("--dataset", choices=FETCH_DATASETS, help="fetch / parse: which dataset")
    parser.add_argument("--day", help="reconcile: trading day, YYYY-MM-DD")
    parser.add_argument("--contract", default="TX", help="reconcile: futures contract code")
    parser.add_argument("--official-file", help="reconcile: a stored DailyMarketReportFut JSON for --day")
    parser.add_argument("--date-from", help="fetch: first date, YYYY-MM-DD")
    parser.add_argument("--date-to", help="fetch: last date, YYYY-MM-DD")
    parser.add_argument("--years", help="fetch *_daily_year: a year or a range such as 2015-2025")
    parser.add_argument("--commodity", default="all", help="fetch *_daily_range: commodity id, e.g. TX, or 'all'")
    parser.add_argument("--stocks", help="fetch twse_stock_day: comma-separated stock numbers")


def _year_span(text: str) -> list[int]:
    first, _, last = text.partition("-")
    low, high = int(first), int(last or first)
    if low > high:
        raise ValueError(f"empty year range {text!r}")
    return list(range(low, high + 1))


def _run_fetch(fetcher: Fetcher, args: Any) -> list[FetchResult | None]:
    today = today_taipei()
    dataset = args.dataset
    if dataset is None:
        raise ValueError("fetch needs --dataset")
    if dataset in TICK_LISTINGS:
        date_from = args.date_from or "2000-01-01"
        return list(fetch_ticks(fetcher, dataset, date_from, args.date_to or today, today=today))
    if dataset.endswith("_daily_year"):
        if not args.years:
            raise ValueError("fetch needs --years")
        kind = dataset.removesuffix("_year")
        return [fetch_history_year(fetcher, kind, year) for year in _year_span(args.years)]
    if dataset.endswith("_daily_range"):
        if not (args.date_from and args.date_to):
            raise ValueError("fetch needs --date-from and --date-to")
        kind = dataset.removesuffix("_range")
        return list(
            fetch_history_range(fetcher, kind, args.date_from, args.date_to, commodity=args.commodity, today=today)
        )
    if dataset == "openapi":
        return list(fetch_openapi_snapshots(fetcher, today=today))
    if dataset == "twse_stock_day":
        if not (args.stocks and args.date_from and args.date_to):
            raise ValueError("fetch needs --stocks, --date-from and --date-to")
        stocks = [item.strip() for item in args.stocks.split(",") if item.strip()]
        return list(fetch_twse_stock_day(fetcher, stocks, args.date_from, args.date_to, today=today))
    raise AssertionError(dataset)


def _run_reconcile(root: Path, args: Any) -> dict[str, Any]:
    if not (args.day and args.official_file):
        raise ValueError("reconcile needs --day and --official-file")
    summary_path = root / "parsed" / "fut_ticks" / f"{args.day}.summary.json"
    if not summary_path.exists():
        raise ValueError(f"no parsed ticks for {args.day}; run fetch and parse --dataset fut_ticks first")
    body = Path(args.official_file).read_bytes()
    official = [row for row in parse_openapi_daily_market(body, options=False) if row["date"] == args.day]
    if not official:
        raise ValueError(f"{args.official_file} holds no rows dated {args.day}")
    return {
        "day": args.day,
        "contract": args.contract,
        "official_sha256": sha256_hex(body),
        "rows": reconcile_volume(json.loads(summary_path.read_text("utf-8")), official, contract=args.contract),
    }


def run_cli(args: Any) -> int:
    """Entry point for ``python -m research data-pipeline official <action>``.

    Exit codes: 0 done, 1 failed, 2 bad arguments, 3 BLOCKED (a human must decide what
    happens next), 4 request cap reached (resumable).
    """
    root = Path(args.root)
    fetcher = Fetcher(root, max_requests=args.max_requests)
    try:
        if args.action == "probe":
            print(json.dumps(probe(fetcher), indent=2, sort_keys=True, ensure_ascii=False))
        elif args.action == "verify":
            problems = fetcher.manifest.verify(root)
            print(json.dumps({"problems": problems}, indent=2))
            return 1 if problems else 0
        elif args.action == "parse":
            if args.dataset not in PARSE_DATASETS:
                raise ValueError(f"parse needs --dataset, one of {', '.join(PARSE_DATASETS)}")
            written = parse_stored(root, args.dataset)
            print(
                json.dumps(
                    {"files": len(written), "parsed": [str(item.relative_to(root)) for item in written]}, indent=2
                )
            )
        elif args.action == "reconcile":
            print(json.dumps(_run_reconcile(root, args), indent=2, sort_keys=True))
        elif args.action == "fetch":
            results = _run_fetch(fetcher, args)
            fetched = sum(1 for item in results if item is not None and not item.from_cache)
            print(
                json.dumps(
                    {"requests": fetcher.requests_made, "fetched": fetched, "files": len(results)},
                    indent=2,
                    sort_keys=True,
                )
            )
        else:
            raise AssertionError(args.action)
    except ValueError as exc:
        print(f"BAD ARGUMENTS: {exc}")
        return 2
    except Blocked as exc:
        print(f"BLOCKED: {exc}. Stopping; nothing was retried. Resume manually once the block is understood.")
        return EXIT_BLOCKED
    except RequestBudgetExhausted as exc:
        print(f"STOPPED: {exc}")
        return EXIT_BUDGET
    except OfficialDataError as exc:
        print(f"FAILED: {exc}")
        return 1
    return 0
