"""Stage Shioaji historical ticks beside the archive. Never writes into it.

``scripts/backfill_historical_ticks.py`` already inserts ``api.ticks()`` rows straight into
``hft.market_data``. That is the wrong default for filling a research archive:

* it writes to whatever ClickHouse ``HFT_CLICKHOUSE_HOST`` names (``clickhouse`` inside the compose
  network), with no check of what the partition already holds, so a day that is only *partly*
  recorded would have its recorded ticks duplicated;
* it fills ``trade_direction`` from the broker's ``tick_type``, while the recorded rows carry the
  EMO classifier's inference (``trade_classifier.py``): one column, two definitions;
* it sets ``ingest_ts = exch_ts``, which erases the receive latency;
* ``int(price * 1_000_000)`` truncates: ``int(2.01 * 1_000_000)`` is 2009999;
* ``ch_insert`` failures are swallowed and the caller still prints the ticks as inserted.

This module keeps the useful part (one call per symbol and day, rate-limited) and changes what is
done with the answer: each response is converted once, exactly, and staged as one Parquet file per
symbol and day under a directory the caller names. Merging staged rows into the archive (and
deduplicating against a partly recorded day) is a separate decision this module does not make.

It imports neither the broker SDK nor a ClickHouse client. The caller supplies a :class:`TickSource`
after logging in itself; this module never logs in.

Assumption NOT VERIFIED here: Shioaji's historical ``ts`` is Taipei wall-clock time written as epoch
nanoseconds, so ``exch_ts = ts - 8h``. That is what the existing script does and what the +8h
``exch_ts`` repair of early 2026 implies, but no call was made. The raw value is staged as
``ts_local_ns`` so the assumption can be undone, and the first real day should be compared with the
archive before any merge.
"""

from __future__ import annotations

import math
import operator
import os
import re
import time
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field
from datetime import date
from decimal import ROUND_HALF_EVEN, Decimal
from pathlib import Path
from typing import Any, Literal, Protocol

PRICE_SCALE = 1_000_000
LOCAL_TO_UTC_NS = 8 * 3600 * 1_000_000_000
SOURCE_LABEL = "api_ticks_backfill"
DEFAULT_MIN_INTERVAL_S = 1.0
DEFAULT_MAX_CONSECUTIVE_ERRORS = 3

_SYMBOL = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]*")
STAGED_COLUMNS = (
    "symbol",
    "exchange",
    "instrument_type",
    "source",
    "seq",
    "ts_local_ns",
    "exch_ts",
    "price_scaled",
    "volume",
    "tick_type",
    "bid_price_scaled",
    "ask_price_scaled",
    "bid_volume",
    "ask_volume",
)
_TEXT_COLUMNS = frozenset({"symbol", "exchange", "instrument_type", "source"})
_OPTIONAL_PRICES = ("bid_price", "ask_price")
_OPTIONAL_VOLUMES = ("bid_volume", "ask_volume")

Status = Literal["staged", "skipped_existing", "empty", "error", "blocked"]


class BadTicks(ValueError):
    """The broker's answer cannot be converted exactly; nothing is staged."""


class Blocked(Exception):
    """Raised by a :class:`TickSource` when the run must stop (rate limit, session lost)."""


class TicksLike(Protocol):
    ts: Sequence[int]
    close: Sequence[float]
    volume: Sequence[int]


@dataclass(frozen=True, slots=True)
class BackfillRequest:
    symbol: str
    exchange: str
    instrument_type: str
    day: str  # YYYY-MM-DD, the date handed to ``api.ticks``


class TickSource(Protocol):
    def fetch(self, request: BackfillRequest) -> TicksLike | None: ...


@dataclass(frozen=True, slots=True)
class Outcome:
    request: BackfillRequest
    status: Status
    rows: int = 0
    detail: str = ""


@dataclass(slots=True)
class BackfillReport:
    outcomes: list[Outcome] = field(default_factory=list)
    stopped_by: str | None = None

    @property
    def failed(self) -> bool:
        return self.stopped_by is not None or any(item.status in ("error", "blocked") for item in self.outcomes)

    def counts(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for item in self.outcomes:
            counts[item.status] = counts.get(item.status, 0) + 1
        return counts


def scale_price(value: Any) -> int:
    """``value`` x 1,000,000 as an int, rounded half-even on its shortest decimal form."""
    if value is None or isinstance(value, bool):
        raise BadTicks(f"price is not a number: {value!r}")
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise BadTicks(f"price is not a number: {value!r}") from exc
    if not math.isfinite(number):
        raise BadTicks(f"price is not finite: {value!r}")
    return int((Decimal(str(number)) * PRICE_SCALE).to_integral_value(ROUND_HALF_EVEN))


def _int(value: Any, what: str) -> int:
    """An exact int. Integers never pass through a float: a nanosecond epoch is beyond 2**53."""
    if value is None or isinstance(value, bool):
        raise BadTicks(f"{what} is not a number: {value!r}")
    try:
        return operator.index(value)
    except TypeError:
        pass
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise BadTicks(f"{what} is not a number: {value!r}") from exc
    if not math.isfinite(number) or number != math.floor(number):
        raise BadTicks(f"{what} is not a whole number: {value!r}")
    return int(number)


def _sequence(ticks: Any, name: str, length: int | None) -> Sequence[Any] | None:
    values = getattr(ticks, name, None)
    if values is None:
        return None
    if length is not None and len(values) != length:
        raise BadTicks(f"{name} has {len(values)} entries, ts has {length}")
    return values


def convert_ticks(request: BackfillRequest, ticks: TicksLike) -> dict[str, list[Any]]:
    """Columns for one symbol and day. ``tick_type`` is kept raw and ``trade_direction`` is not written."""
    ts = getattr(ticks, "ts", None)
    if ts is None:
        raise BadTicks("response has no ts")
    count = len(ts)
    close = _sequence(ticks, "close", count)
    volume = _sequence(ticks, "volume", count)
    if close is None or volume is None:
        raise BadTicks("response has no close or no volume")
    tick_type = _sequence(ticks, "tick_type", count)
    prices = {name: _sequence(ticks, name, count) for name in _OPTIONAL_PRICES}
    volumes = {name: _sequence(ticks, name, count) for name in _OPTIONAL_VOLUMES}

    columns: dict[str, list[Any]] = {
        "symbol": [request.symbol] * count,
        "exchange": [request.exchange] * count,
        "instrument_type": [request.instrument_type] * count,
        "source": [SOURCE_LABEL] * count,
        "seq": list(range(count)),
        "ts_local_ns": [_int(value, "ts") for value in ts],
        "price_scaled": [scale_price(value) for value in close],
        "volume": [_int(value, "volume") for value in volume],
        "tick_type": [None] * count if tick_type is None else [_int(value, "tick_type") for value in tick_type],
    }
    columns["exch_ts"] = [value - LOCAL_TO_UTC_NS for value in columns["ts_local_ns"]]
    for name, values in prices.items():
        columns[f"{name}_scaled"] = [None] * count if values is None else [_nullable_price(v) for v in values]
    for name, values in volumes.items():
        columns[name] = [None] * count if values is None else [_nullable_int(v, name) for v in values]
    return columns


def _nullable_price(value: Any) -> int | None:
    return None if value is None else scale_price(value)


def _nullable_int(value: Any, what: str) -> int | None:
    return None if value is None else _int(value, what)


def staged_path(out_dir: Path, request: BackfillRequest) -> Path:
    """``<out_dir>/<day>/<symbol>.parquet``; a day or symbol that is not a plain name is refused."""
    # fromisoformat also takes 20260406 (basic format), which would stage the same day under a second name.
    try:
        canonical = date.fromisoformat(request.day).isoformat()
    except ValueError as exc:
        raise ValueError(f"day must be YYYY-MM-DD: {request.day!r}") from exc
    if canonical != request.day:
        raise ValueError(f"day must be YYYY-MM-DD: {request.day!r}")
    if not _SYMBOL.fullmatch(request.symbol):
        raise ValueError(f"symbol is not a plain name: {request.symbol!r}")
    return out_dir / request.day / f"{request.symbol}.parquet"


def write_staged(path: Path, columns: dict[str, list[Any]]) -> None:
    """Write through a temporary name so a crash never leaves a half file under the final one."""
    import pyarrow as pa
    import pyarrow.parquet as pq

    if set(columns) != set(STAGED_COLUMNS):
        raise ValueError(f"columns differ from STAGED_COLUMNS: {sorted(set(columns) ^ set(STAGED_COLUMNS))}")
    # Fixed types: an all-null optional column must not become a null-typed column that one day's file
    # has and the next day's does not, or the files stop being readable as one dataset.
    schema = pa.schema([(name, pa.string() if name in _TEXT_COLUMNS else pa.int64()) for name in STAGED_COLUMNS])
    table = pa.table({name: columns[name] for name in STAGED_COLUMNS}, schema=schema).replace_schema_metadata(
        {
            "source": SOURCE_LABEL,
            "price_scale": str(PRICE_SCALE),
            "exch_ts": f"ts_local_ns - {LOCAL_TO_UTC_NS} ns (Taipei wall clock to UTC; NOT VERIFIED against the SDK)",
        }
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    partial = path.with_name(path.name + ".partial")
    pq.write_table(table, partial)
    os.replace(partial, path)


def plan_requests(
    out_dir: Path, days: Iterable[str], symbols: Iterable[str], *, exchange: str, instrument_type: str
) -> list[tuple[BackfillRequest, bool]]:
    """Every (day, symbol) request and whether it is already staged, in a stable order."""
    plan: list[tuple[BackfillRequest, bool]] = []
    for day in days:
        for symbol in symbols:
            request = BackfillRequest(symbol, exchange, instrument_type, day)
            plan.append((request, staged_path(out_dir, request).exists()))
    return plan


def run_backfill(
    source: TickSource,
    requests: Iterable[BackfillRequest],
    out_dir: Path,
    *,
    min_interval_s: float = DEFAULT_MIN_INTERVAL_S,
    max_consecutive_errors: int = DEFAULT_MAX_CONSECUTIVE_ERRORS,
    clock: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
) -> BackfillReport:
    """Fetch and stage each request once. Staged files are never overwritten.

    A :class:`Blocked` from the source stops the run; so do ``max_consecutive_errors`` errors in a
    row. Anything not attempted after a stop is absent from the report, not marked done.
    """
    report = BackfillReport()
    last_fetch: float | None = None
    consecutive_errors = 0
    # Every request is checked before the first call, so a bad name cannot stop a run half way.
    checked = [(request, staged_path(out_dir, request)) for request in requests]
    for request, path in checked:
        if path.exists():
            report.outcomes.append(Outcome(request, "skipped_existing"))
            continue
        if last_fetch is not None:
            wait = min_interval_s - (clock() - last_fetch)
            if wait > 0:
                sleep(wait)
        last_fetch = clock()
        try:
            ticks = source.fetch(request)
            columns = None if ticks is None else convert_ticks(request, ticks)
            if columns is None or not columns["seq"]:
                report.outcomes.append(Outcome(request, "empty"))
                consecutive_errors = 0
                continue
            write_staged(path, columns)
        except Blocked as exc:
            report.outcomes.append(Outcome(request, "blocked", detail=str(exc)[:200]))
            report.stopped_by = "blocked"
            return report
        except Exception as exc:  # one bad request must not end the run; it is recorded
            report.outcomes.append(Outcome(request, "error", detail=f"{type(exc).__name__}: {str(exc)[:200]}"))
            consecutive_errors += 1
            if consecutive_errors >= max_consecutive_errors:
                report.stopped_by = "consecutive_errors"
                return report
            continue
        consecutive_errors = 0
        report.outcomes.append(Outcome(request, "staged", rows=len(columns["seq"])))
    return report
