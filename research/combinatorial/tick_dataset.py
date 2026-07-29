"""Governed TAIFEX tick-aggregate bar dataset for the tick mining family.

Structural sibling of ``research/combinatorial/smma_dataset.py`` — same
governance shape (guarded read-only query, atomic write, hash-fingerprinted
sidecar, frozen date/root/timeframe scope) but a fully independent schema and
export path. This module MUST NOT import or mutate ``smma_dataset.py``'s
``BarDataset``/schema: other already-launched SMMA mining runs depend on that
contract's exact fingerprint. Every field here is a per-bar aggregate of
intrabar ``Tick``/``BidAsk`` rows (trade/quote counts, aggressor split) —
Phase 1 only: pure ``countIf``/``sumIf`` aggregates. A realized-vol feature
requiring a window-function subquery is deferred to a later phase.
"""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Mapping, Sequence

import numpy as np

from hft_platform.infra.ch_client import get_ch_client
from research.combinatorial.taifex_trading_dates import (
    FullSessionEligibility,
    TradingDateWindow,
    build_trading_date_window,
    clickhouse_trading_day_expression,
    full_session_eligibility,
)

CH_PRICE_SCALE = 1_000_000.0
TICK_DATASET_SCHEMA = "tick_taifex_bars.v2"
LEGACY_TICK_DATASET_SCHEMAS: frozenset[str] = frozenset({"tick_taifex_bars.v1"})
TICK_DATE_FROM = "2026-04-07"
TICK_DATE_TO = "2026-07-24"
TICK_TIMEFRAMES_MINUTES: tuple[int, ...] = (60, 120, 240, 1440)
TICK_SUPPORTED_TIMEFRAMES_MINUTES: tuple[int, ...] = (2, *TICK_TIMEFRAMES_MINUTES)
TICK_ROOTS: tuple[str, ...] = ("TXF", "TMF")
MAX_DAILY_UNKNOWN_AGGRESSOR_RATIO = 0.05


class TickDatasetGovernanceError(RuntimeError):
    """Raised when a query, dataset, or sidecar violates the frozen tick contract."""


@dataclass(frozen=True, slots=True)
class TickBarDataset:
    root: np.ndarray
    timeframe_min: np.ndarray
    contract: np.ndarray
    trading_day: np.ndarray
    session: np.ndarray
    ts_ns: np.ndarray
    open: np.ndarray
    high: np.ndarray
    low: np.ndarray
    close: np.ndarray
    bid_open: np.ndarray
    ask_open: np.ndarray
    bid_close: np.ndarray
    ask_close: np.ndarray
    trade_tick_count: np.ndarray
    quote_update_count: np.ndarray
    buy_tick_count: np.ndarray
    sell_tick_count: np.ndarray
    unknown_tick_count: np.ndarray
    buy_tick_volume: np.ndarray
    sell_tick_volume: np.ndarray
    reset: np.ndarray
    session_close: np.ndarray

    def __len__(self) -> int:
        return int(self.ts_ns.size)

    def validate(self) -> None:
        arrays = {field: getattr(self, field) for field in self.__dataclass_fields__}
        lengths = {np.asarray(values).reshape(-1).size for values in arrays.values()}
        if len(lengths) != 1:
            raise TickDatasetGovernanceError(f"tick bar columns have mismatched lengths: {sorted(lengths)}")
        if len(self) == 0:
            raise TickDatasetGovernanceError("tick bar dataset is empty")
        if not set(np.unique(self.root)).issubset(set(TICK_ROOTS)):
            raise TickDatasetGovernanceError("tick bar dataset contains an unsupported root")
        if not set(int(value) for value in np.unique(self.timeframe_min)).issubset(
            set(TICK_SUPPORTED_TIMEFRAMES_MINUTES)
        ):
            raise TickDatasetGovernanceError("tick bar dataset contains an unsupported timeframe")
        ohlc = np.column_stack((self.open, self.high, self.low, self.close))
        if not np.all(np.isfinite(ohlc)) or np.any(ohlc <= 0.0):
            raise TickDatasetGovernanceError("trade OHLC must be positive and finite")
        counts = np.column_stack(
            (
                self.trade_tick_count,
                self.quote_update_count,
                self.buy_tick_count,
                self.sell_tick_count,
                self.unknown_tick_count,
                self.buy_tick_volume,
                self.sell_tick_volume,
            )
        )
        if not np.all(np.isfinite(counts)) or np.any(counts < 0.0):
            raise TickDatasetGovernanceError("tick aggregate counts must be finite and non-negative")
        if np.any(self.trade_tick_count < 1):
            raise TickDatasetGovernanceError("every bar must contain at least one trade tick")
        aggressor_sum = self.buy_tick_count + self.sell_tick_count + self.unknown_tick_count
        if not np.allclose(aggressor_sum, self.trade_tick_count):
            raise TickDatasetGovernanceError("buy+sell+unknown tick counts must equal trade_tick_count")
        offenders: list[str] = []
        groups = sorted(
            {
                (str(root), int(timeframe), str(day))
                for root, timeframe, day in zip(
                    self.root,
                    self.timeframe_min,
                    self.trading_day,
                    strict=True,
                )
            }
        )
        for root, timeframe, day in groups:
            mask = (self.root == root) & (self.timeframe_min == timeframe) & (self.trading_day == day)
            total = float(np.sum(self.trade_tick_count[mask]))
            unknown = float(np.sum(self.unknown_tick_count[mask]))
            ratio = unknown / total if total > 0.0 else 1.0
            if ratio > MAX_DAILY_UNKNOWN_AGGRESSOR_RATIO:
                offenders.append(f"{root}/{timeframe}m/{day}={ratio:.2%}")
        if offenders:
            preview = ", ".join(offenders[:10])
            suffix = f" (+{len(offenders) - 10} more)" if len(offenders) > 10 else ""
            raise TickDatasetGovernanceError(
                f"unknown aggressor ratio exceeds {MAX_DAILY_UNKNOWN_AGGRESSOR_RATIO:.0%}: {preview}{suffix}"
            )
        keys = {
            (str(root), int(timeframe), int(timestamp))
            for root, timeframe, timestamp in zip(self.root, self.timeframe_min, self.ts_ns, strict=True)
        }
        if len(keys) != len(self):
            raise TickDatasetGovernanceError("duplicate root/timeframe/timestamp tick bars")

    def group(self, root: str, timeframe_min: int) -> "TickBarDataset":
        mask = (self.root == root) & (self.timeframe_min == int(timeframe_min))
        indices = np.flatnonzero(mask)
        if indices.size == 0:
            raise TickDatasetGovernanceError(f"no tick bars for {root}/{timeframe_min}m")
        return TickBarDataset(
            **{field: np.asarray(getattr(self, field))[indices] for field in self.__dataclass_fields__}
        )


def _query_hash(query: str) -> str:
    return hashlib.sha256(query.encode("utf-8")).hexdigest()


def _guard_query(query: str) -> dict[str, Any]:
    """Run the repository query guard before any ClickHouse call."""
    from scripts.ch_query_guard import _evaluate_sql_guard

    result = _evaluate_sql_guard(query, allow_full_scan=False)
    if result.get("overall") == "fail":
        raise TickDatasetGovernanceError(f"ClickHouse query guard rejected tick export: {result.get('checks')}")
    return {
        "query_sha256": _query_hash(query),
        "guard_overall": result.get("overall"),
        "guard_checks": result.get("checks"),
    }


def _tick_bar_query(
    timeframe_min: int,
    *,
    date_from: str,
    date_to: str,
    trading_window: TradingDateWindow | None = None,
) -> str:
    if timeframe_min not in TICK_SUPPORTED_TIMEFRAMES_MINUTES:
        raise ValueError(f"unsupported timeframe: {timeframe_min}")
    row_limit = _query_row_limit(timeframe_min)
    window = trading_window or build_trading_date_window(date_from, date_to)
    event_time_expression = "fromUnixTimestamp64Nano(exch_ts, 'Asia/Taipei')"
    trading_day_expression = clickhouse_trading_day_expression(event_time_expression, window)
    night_session_predicate = f"toHour({event_time_expression}) >= 15 OR toHour({event_time_expression}) < 6"
    session_expression = (
        "'full'"
        if timeframe_min == 1440
        else """if(
                {night_session_predicate},
                'night',
                'day'
            )""".format(night_session_predicate=night_session_predicate)
    )
    bucket_expression = (
        "toStartOfDay(toDateTime(trading_day, 'Asia/Taipei'))"
        if timeframe_min == 1440
        else f"""if(
            {night_session_predicate},
            toStartOfInterval(
                {event_time_expression},
                INTERVAL {timeframe_min} MINUTE,
                toDateTime64('1970-01-01 15:00:00', 9, 'Asia/Taipei')
            ),
            toStartOfInterval(
                {event_time_expression},
                INTERVAL {timeframe_min} MINUTE,
                toDateTime64('1970-01-01 08:45:00', 9, 'Asia/Taipei')
            )
            )"""
    )
    return f"""
WITH
    toDateTime64('{window.query_wall_time_from}', 9, 'Asia/Taipei') AS range_start,
    toDateTime64('{window.query_wall_time_to}', 9, 'Asia/Taipei') AS range_end,
    base AS (
        SELECT
            multiIf(match(symbol, '^TXF[A-L][0-9]$'), 'TXF', 'TMF') AS root,
            symbol AS contract,
            {trading_day_expression} AS trading_day,
            {session_expression} AS session,
            {bucket_expression} AS bucket,
            type,
            exch_ts,
            ingest_ts,
            seq_no,
            price_scaled,
            volume,
            trade_direction,
            if(length(bids_price) > 0, bids_price[1], 0) AS bid_scaled,
            if(length(asks_price) > 0, asks_price[1], 0) AS ask_scaled,
            if(length(bids_vol) > 0, bids_vol[1], 0) AS bid_qty,
            if(length(asks_vol) > 0, asks_vol[1], 0) AS ask_qty
        FROM hft.market_data
        WHERE ingest_ts >= toUnixTimestamp64Nano(range_start)
          AND ingest_ts < toUnixTimestamp64Nano(range_end)
          AND (
            match(symbol, '^TXF[A-L][0-9]$')
            OR match(symbol, '^TMF[A-L][0-9]$')
          )
          AND type IN ('Tick', 'BidAsk')
    )
SELECT
    root,
    contract,
    toString(trading_day),
    session,
    toUnixTimestamp64Nano(toDateTime64(bucket, 9, 'Asia/Taipei')) AS bucket_ns,
    argMinIf(price_scaled, tuple(exch_ts, ingest_ts, seq_no), type = 'Tick') / 1000000.0 AS open,
    maxIf(price_scaled, type = 'Tick') / 1000000.0 AS high,
    minIf(price_scaled, type = 'Tick') / 1000000.0 AS low,
    argMaxIf(price_scaled, tuple(exch_ts, ingest_ts, seq_no), type = 'Tick') / 1000000.0 AS close,
    argMinIf(
        bid_scaled,
        tuple(exch_ts, ingest_ts, seq_no),
        type = 'BidAsk' AND bid_scaled > 0 AND ask_scaled >= bid_scaled AND bid_qty > 0 AND ask_qty > 0
    ) / 1000000.0 AS bid_open,
    argMinIf(
        ask_scaled,
        tuple(exch_ts, ingest_ts, seq_no),
        type = 'BidAsk' AND bid_scaled > 0 AND ask_scaled >= bid_scaled AND bid_qty > 0 AND ask_qty > 0
    ) / 1000000.0 AS ask_open,
    argMaxIf(
        bid_scaled,
        tuple(exch_ts, ingest_ts, seq_no),
        type = 'BidAsk' AND bid_scaled > 0 AND ask_scaled >= bid_scaled AND bid_qty > 0 AND ask_qty > 0
    ) / 1000000.0 AS bid_close,
    argMaxIf(
        ask_scaled,
        tuple(exch_ts, ingest_ts, seq_no),
        type = 'BidAsk' AND bid_scaled > 0 AND ask_scaled >= bid_scaled AND bid_qty > 0 AND ask_qty > 0
    ) / 1000000.0 AS ask_close,
    countIf(type = 'Tick' AND price_scaled > 0) AS trade_tick_count,
    countIf(type = 'BidAsk') AS quote_update_count,
    countIf(type = 'Tick' AND price_scaled > 0 AND trade_direction = 1) AS buy_tick_count,
    countIf(type = 'Tick' AND price_scaled > 0 AND trade_direction = -1) AS sell_tick_count,
    countIf(type = 'Tick' AND price_scaled > 0 AND trade_direction = 0) AS unknown_tick_count,
    sumIf(volume, type = 'Tick' AND price_scaled > 0 AND trade_direction = 1) AS buy_tick_volume,
    sumIf(volume, type = 'Tick' AND price_scaled > 0 AND trade_direction = -1) AS sell_tick_volume
FROM base
WHERE trading_day != toDate('1970-01-01')
GROUP BY root, contract, trading_day, session, bucket
HAVING countIf(type = 'Tick' AND price_scaled > 0) > 0
   AND countIf(
       type = 'BidAsk' AND bid_scaled > 0 AND ask_scaled >= bid_scaled AND bid_qty > 0 AND ask_qty > 0
   ) > 0
ORDER BY root, trading_day, session, bucket, contract
    LIMIT {row_limit}
""".strip()


def _query_row_limit(timeframe_min: int) -> int:
    """Bound result cardinality without truncating the governed 2-minute lane."""
    return 1_000_000 if int(timeframe_min) == 2 else 100_000


def _causal_front_contracts(rows: Sequence[Sequence[Any]]) -> dict[tuple[str, str], str]:
    """Select each day from the previous observed trading day's trade-tick count."""
    counts: dict[tuple[str, str, str], int] = {}
    days_by_root: dict[str, set[str]] = {}
    for row in rows:
        root, contract, trading_day = str(row[0]), str(row[1]), str(row[2])
        bar_trade_ticks = int(row[13])
        key = (root, trading_day, contract)
        counts[key] = counts.get(key, 0) + bar_trade_ticks
        days_by_root.setdefault(root, set()).add(trading_day)

    selected: dict[tuple[str, str], str] = {}
    for root, day_values in days_by_root.items():
        ordered_days = sorted(day_values)
        for previous_day, current_day in zip(ordered_days, ordered_days[1:], strict=False):
            prior = [
                (count, contract)
                for (row_root, day, contract), count in counts.items()
                if row_root == root and day == previous_day
            ]
            if prior:
                _count, contract = max(prior, key=lambda item: (item[0], item[1]))
                selected[(root, current_day)] = contract
    return selected


def _align_common_trading_days(dataset: TickBarDataset) -> TickBarDataset:
    """Restrict every present root/timeframe group to a common comparable day set."""
    groups = {(str(root), int(timeframe)) for root, timeframe in zip(dataset.root, dataset.timeframe_min, strict=True)}
    days_by_group = {
        group: {
            str(day) for day in dataset.trading_day[(dataset.root == group[0]) & (dataset.timeframe_min == group[1])]
        }
        for group in groups
    }
    common_days = set.intersection(*days_by_group.values())
    if not common_days:
        raise TickDatasetGovernanceError("root/timeframe groups have no common trading days")
    all_days = set.union(*days_by_group.values())
    excluded_days = all_days - common_days
    indices = np.flatnonzero(np.isin(dataset.trading_day, sorted(common_days)))
    aligned_columns = {field: np.asarray(getattr(dataset, field))[indices] for field in dataset.__dataclass_fields__}
    aligned = TickBarDataset(**aligned_columns)

    resets: np.ndarray = np.zeros(len(aligned), dtype=np.bool_)
    session_closes: np.ndarray = np.zeros(len(aligned), dtype=np.bool_)
    previous: dict[tuple[str, int], tuple[int, str, str, str]] = {}
    for index in range(len(aligned)):
        root = str(aligned.root[index])
        timeframe = int(aligned.timeframe_min[index])
        contract = str(aligned.contract[index])
        trading_day = str(aligned.trading_day[index])
        session = str(aligned.session[index])
        timestamp = int(aligned.ts_ns[index])
        prior = previous.get((root, timeframe))
        gap_multiplier = 4 if timeframe == 1440 else 1
        gap_limit_ns = timeframe * 60 * 1_000_000_000 * gap_multiplier
        crossed_excluded_day = bool(
            prior is not None and any(prior[3] < excluded_day < trading_day for excluded_day in excluded_days)
        )
        resets[index] = bool(
            prior is None
            or prior[1] != contract
            or prior[2] != session
            or timestamp <= prior[0]
            or (timestamp - prior[0]) > gap_limit_ns
            or crossed_excluded_day
        )
        previous[(root, timeframe)] = (timestamp, contract, session, trading_day)

        next_index = index + 1
        session_closes[index] = bool(
            timeframe == 1440
            or next_index == len(aligned)
            or str(aligned.root[next_index]) != root
            or int(aligned.timeframe_min[next_index]) != timeframe
            or str(aligned.session[next_index]) != session
        )
    aligned_columns["reset"] = resets
    aligned_columns["session_close"] = session_closes
    result = TickBarDataset(**aligned_columns)
    result.validate()
    return result


def rows_to_tick_bar_dataset(
    rows_by_timeframe: Mapping[int, Sequence[Sequence[Any]]],
    *,
    align_common: bool = True,
    validate: bool = True,
) -> TickBarDataset:
    """Apply causal front-month selection and discontinuity resets."""
    records: list[tuple[Any, ...]] = []
    for timeframe, rows in rows_by_timeframe.items():
        if timeframe not in TICK_SUPPORTED_TIMEFRAMES_MINUTES:
            raise ValueError(f"unsupported timeframe: {timeframe}")
        selected = _causal_front_contracts(rows)
        filtered = [row for row in rows if selected.get((str(row[0]), str(row[2]))) == str(row[1])]
        filtered.sort(key=lambda row: (str(row[0]), int(row[4])))
        previous: dict[str, tuple[int, str, str]] = {}
        for row in filtered:
            root, contract, trading_day, session = map(str, row[:4])
            ts_ns = int(row[4])
            prior = previous.get(root)
            gap_multiplier = 4 if timeframe == 1440 else 1
            gap_limit_ns = int(timeframe) * 60 * 1_000_000_000 * gap_multiplier
            reset = (
                prior is None
                or prior[1] != contract
                or prior[2] != session
                or ts_ns <= prior[0]
                or (ts_ns - prior[0]) > gap_limit_ns
            )
            records.append(
                (
                    root,
                    int(timeframe),
                    contract,
                    trading_day,
                    session,
                    ts_ns,
                    float(row[5]),
                    float(row[6]),
                    float(row[7]),
                    float(row[8]),
                    float(row[9]),
                    float(row[10]),
                    float(row[11]),
                    float(row[12]),
                    float(row[13]),
                    float(row[14]),
                    float(row[15]),
                    float(row[16]),
                    float(row[17]),
                    float(row[18]),
                    float(row[19]),
                    reset,
                    False,
                )
            )
            previous[root] = (ts_ns, contract, session)

    if not records:
        raise TickDatasetGovernanceError("causal front-month selection produced no tick bars")
    records.sort(key=lambda item: (item[0], item[1], item[5]))
    mutable = [list(record) for record in records]
    for index, record in enumerate(mutable):
        is_last = index == len(mutable) - 1
        next_differs = is_last or (
            mutable[index + 1][0] != record[0]
            or mutable[index + 1][1] != record[1]
            or mutable[index + 1][4] != record[4]
        )
        record[22] = next_differs
    columns = list(zip(*mutable, strict=True))
    dataset = TickBarDataset(
        root=np.asarray(columns[0], dtype="<U3"),
        timeframe_min=np.asarray(columns[1], dtype=np.int16),
        contract=np.asarray(columns[2], dtype="<U8"),
        trading_day=np.asarray(columns[3], dtype="<U10"),
        session=np.asarray(columns[4], dtype="<U5"),
        ts_ns=np.asarray(columns[5], dtype=np.int64),
        open=np.asarray(columns[6], dtype=np.float64),
        high=np.asarray(columns[7], dtype=np.float64),
        low=np.asarray(columns[8], dtype=np.float64),
        close=np.asarray(columns[9], dtype=np.float64),
        bid_open=np.asarray(columns[10], dtype=np.float64),
        ask_open=np.asarray(columns[11], dtype=np.float64),
        bid_close=np.asarray(columns[12], dtype=np.float64),
        ask_close=np.asarray(columns[13], dtype=np.float64),
        trade_tick_count=np.asarray(columns[14], dtype=np.float64),
        quote_update_count=np.asarray(columns[15], dtype=np.float64),
        buy_tick_count=np.asarray(columns[16], dtype=np.float64),
        sell_tick_count=np.asarray(columns[17], dtype=np.float64),
        unknown_tick_count=np.asarray(columns[18], dtype=np.float64),
        buy_tick_volume=np.asarray(columns[19], dtype=np.float64),
        sell_tick_volume=np.asarray(columns[20], dtype=np.float64),
        reset=np.asarray(columns[21], dtype=np.bool_),
        session_close=np.asarray(columns[22], dtype=np.bool_),
    )
    if validate:
        dataset.validate()
    return _align_common_trading_days(dataset) if align_common else dataset


def _restrict_to_full_sessions(
    dataset: TickBarDataset,
    window: TradingDateWindow,
    *,
    expected_timeframes: Sequence[int] | None = None,
) -> tuple[TickBarDataset, FullSessionEligibility]:
    required_timeframes = (
        tuple(int(value) for value in np.unique(dataset.timeframe_min))
        if expected_timeframes is None
        else tuple(int(value) for value in expected_timeframes)
    )
    eligibility = full_session_eligibility(
        root=dataset.root,
        timeframe_min=dataset.timeframe_min,
        trading_day=dataset.trading_day,
        session=dataset.session,
        expected_trading_dates=window.expected_trading_dates,
        roots=TICK_ROOTS,
        required_timeframes=required_timeframes,
    )
    if not eligibility.eligible_trading_dates:
        raise TickDatasetGovernanceError("no requested trading date has complete TXF/TMF day and night sessions")
    indices = np.flatnonzero(np.isin(dataset.trading_day, eligibility.eligible_trading_dates))
    restricted = TickBarDataset(
        **{field: np.asarray(getattr(dataset, field))[indices] for field in dataset.__dataclass_fields__}
    )
    return _align_common_trading_days(restricted), eligibility


def _content_hash(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _metadata_hash(payload: Mapping[str, Any]) -> str:
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def save_governed_tick_dataset(
    path: str | Path,
    dataset: TickBarDataset,
    *,
    query_evidence: Sequence[Mapping[str, Any]],
    code_fingerprint: str,
    requested_date_from: str | None = None,
    requested_date_to: str | None = None,
    trading_window: TradingDateWindow | None = None,
    eligibility: FullSessionEligibility | None = None,
) -> tuple[Path, Path]:
    """Atomically write the NPZ and its complete governance sidecar."""
    dataset.validate()
    output = Path(path)
    output.parent.mkdir(parents=True, exist_ok=True)
    arrays: dict[str, Any] = {field: np.asarray(getattr(dataset, field)) for field in dataset.__dataclass_fields__}
    with tempfile.NamedTemporaryFile(dir=output.parent, suffix=".npz", delete=False) as handle:
        temp_path = Path(handle.name)
        np.savez_compressed(handle, **arrays)
        handle.flush()
        os.fsync(handle.fileno())
    temp_path.replace(output)
    content_hash = _content_hash(output)
    days = [str(value) for value in np.unique(dataset.trading_day)]
    requested_from = requested_date_from or min(days)
    requested_to = requested_date_to or max(days)
    window = trading_window or build_trading_date_window(requested_from, requested_to)
    eligible_dates = tuple(days) if eligibility is None else eligibility.eligible_trading_dates
    missing_dates = () if eligibility is None else eligibility.missing_expected_trading_dates
    excluded_dates = () if eligibility is None else eligibility.excluded_partial_trading_dates
    payload = {
        "schema": TICK_DATASET_SCHEMA,
        "source": "hft.market_data",
        "source_type": "real",
        "generator": "research.combinatorial.tick_dataset",
        "owner": "research",
        "split": "full",
        "created_at": datetime.now(UTC).isoformat(),
        "dataset_id": f"tick_taifex_{min(days)}_{max(days)}",
        "symbols": sorted(str(value) for value in np.unique(dataset.root)),
        "contracts": sorted(str(value) for value in np.unique(dataset.contract)),
        "timeframes_minutes": sorted(int(value) for value in np.unique(dataset.timeframe_min)),
        "row_count": len(dataset),
        "rows": len(dataset),
        "fields": list(dataset.__dataclass_fields__),
        "date_from": min(days),
        "date_to": max(days),
        "requested_date_from": requested_from,
        "requested_date_to": requested_to,
        "requested_trading_date_from": requested_from,
        "requested_trading_date_to": requested_to,
        "calendar_name": window.calendar_name,
        "calendar_package_version": window.calendar_package_version,
        "calendar_mapping_hash": window.calendar_mapping_hash,
        "query_wall_time_from": window.query_wall_time_from,
        "query_wall_time_to": window.query_wall_time_to,
        "warmup_trading_date": window.warmup_trading_date,
        "expected_trading_dates": list(window.expected_trading_dates),
        "eligible_trading_dates": list(eligible_dates),
        "missing_expected_trading_dates": list(missing_dates),
        "excluded_partial_trading_dates": list(excluded_dates),
        "trading_day_count": len(eligible_dates),
        "price_scale_source": 1_000_000,
        "price_scale_output": 1,
        "timezone": "Asia/Taipei",
        "roll_rule": "front contract selected by previous observed trading-day cumulative trade-tick count",
        "gap_rule": "contract/session change or unexpected gap forces reset and flat",
        "ohlc_rule": "Tick trade prints only; never mid",
        "aggregate_rule": (
            "Phase 1: pure countIf/sumIf trade/quote/aggressor aggregates per bar; "
            "realized-vol (window-function) deferred to a later phase"
        ),
        "query_evidence": [dict(item) for item in query_evidence],
        "code_fingerprint": code_fingerprint,
        "content_sha256": content_hash,
        "data_fingerprint": content_hash,
        "data_ul": 5,
        "schema_version": 2,
        "governance_complete": eligibility is not None,
    }
    payload["metadata_hash"] = _metadata_hash(payload)
    sidecar = Path(str(output) + ".meta.json")
    with tempfile.NamedTemporaryFile(
        mode="w",
        dir=sidecar.parent,
        suffix=".json",
        delete=False,
        encoding="utf-8",
    ) as handle:
        json.dump(payload, handle, indent=2, sort_keys=True)
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())
        meta_temp = Path(handle.name)
    meta_temp.replace(sidecar)
    return output, sidecar


def load_governed_tick_dataset(path: str | Path) -> TickBarDataset:
    """Load only when the sidecar and full content hash match."""
    source = Path(path)
    sidecar = Path(str(source) + ".meta.json")
    if not source.exists() or not sidecar.exists():
        raise TickDatasetGovernanceError(f"tick dataset and sidecar are required: {source}")
    payload = json.loads(sidecar.read_text(encoding="utf-8"))
    expected_metadata_hash = str(payload.pop("metadata_hash", ""))
    if not expected_metadata_hash or _metadata_hash(payload) != expected_metadata_hash:
        raise TickDatasetGovernanceError("tick dataset sidecar fingerprint mismatch")
    if payload.get("schema") not in {TICK_DATASET_SCHEMA, *LEGACY_TICK_DATASET_SCHEMAS}:
        raise TickDatasetGovernanceError("tick dataset sidecar schema mismatch")
    actual_hash = _content_hash(source)
    if payload.get("content_sha256") != actual_hash:
        raise TickDatasetGovernanceError("tick dataset content fingerprint mismatch")
    with np.load(source, allow_pickle=False) as data:
        expected = set(TickBarDataset.__dataclass_fields__)
        if set(data.files) != expected:
            raise TickDatasetGovernanceError("tick dataset fields do not match TickBarDataset contract")
        dataset = TickBarDataset(**{field: np.asarray(data[field]) for field in expected})
    dataset.validate()
    if int(payload.get("row_count", -1)) != len(dataset):
        raise TickDatasetGovernanceError("tick dataset row count does not match sidecar")
    return dataset


def export_clickhouse_tick_dataset(
    path: str | Path,
    *,
    code_fingerprint: str,
    client: Any | None = None,
    date_from: str = TICK_DATE_FROM,
    date_to: str = TICK_DATE_TO,
    timeframes_minutes: Sequence[int] = TICK_TIMEFRAMES_MINUTES,
) -> tuple[Path, Path]:
    """Export the frozen date window through guarded, read-only queries."""
    requested_timeframes = tuple(int(value) for value in timeframes_minutes)
    if (
        not requested_timeframes
        or len(set(requested_timeframes)) != len(requested_timeframes)
        or not set(requested_timeframes).issubset(set(TICK_SUPPORTED_TIMEFRAMES_MINUTES))
    ):
        raise ValueError("timeframes_minutes must be distinct supported values")
    if 60 not in requested_timeframes:
        raise ValueError("timeframes_minutes must include 60 for full-session eligibility")
    trading_window = build_trading_date_window(date_from, date_to)
    try:
        query_client = client if client is not None else get_ch_client()
    except Exception as exc:
        raise TickDatasetGovernanceError(
            f"ClickHouse client initialization failed: {type(exc).__name__}: {exc}"
        ) from exc
    rows_by_timeframe: dict[int, Sequence[Sequence[Any]]] = {}
    evidence: list[dict[str, Any]] = []
    for timeframe in requested_timeframes:
        query = _tick_bar_query(
            timeframe,
            date_from=date_from,
            date_to=date_to,
            trading_window=trading_window,
        )
        query_evidence = _guard_query(query)
        row_limit = _query_row_limit(timeframe)
        try:
            result = query_client.query(
                query,
                settings={
                    "readonly": 1,
                    "max_memory_usage": 2_147_483_648,
                    "max_threads": 2,
                    "max_execution_time": 300,
                    "max_result_rows": row_limit,
                    "result_overflow_mode": "throw",
                },
            )
        except Exception as exc:
            raise TickDatasetGovernanceError(
                f"guarded ClickHouse tick export failed for {timeframe}m: {type(exc).__name__}: {exc}"
            ) from exc
        rows = list(result.result_rows)
        if len(rows) >= row_limit:
            raise TickDatasetGovernanceError(
                f"{timeframe}m tick export reached row limit; refusing possible truncation"
            )
        query_evidence.update({"timeframe_min": timeframe, "result_rows": len(rows)})
        evidence.append(query_evidence)
        rows_by_timeframe[timeframe] = rows
    selected = rows_to_tick_bar_dataset(rows_by_timeframe, align_common=False, validate=False)
    dataset, eligibility = _restrict_to_full_sessions(
        selected,
        trading_window,
        expected_timeframes=requested_timeframes,
    )
    return save_governed_tick_dataset(
        path,
        dataset,
        query_evidence=evidence,
        code_fingerprint=code_fingerprint,
        requested_date_from=date_from,
        requested_date_to=date_to,
        trading_window=trading_window,
        eligibility=eligibility,
    )
