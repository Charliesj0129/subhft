"""Governed TAIFEX bar dataset for the SMMA mining family.

The export is read-only, time-bounded, price-scale explicit, and records every
query hash in its sidecar. OHLC comes only from ``Tick`` trade prints. Bid/ask
is the first valid top-of-book observation in each bar and is retained for the
next-bar executable-price model.
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
from research.tools.vm_ul import DataUL, audit_claimed_ul
from research.combinatorial.taifex_trading_dates import (
    BarAggregationLayout,
    FullSessionEligibility,
    TradingDateWindow,
    build_trading_date_window,
    clickhouse_taifex_bucket_timestamp,
    clickhouse_taifex_contract_predicate,
    clickhouse_taifex_session_predicates,
    clickhouse_trading_day_expression,
    full_session_eligibility,
    reaggregate_taifex_bar_rows,
)
from research.data_pipeline.quality import load_latest_report, stamp_payload

CH_PRICE_SCALE = 1_000_000.0
DATASET_SCHEMA = "smma_taifex_bars.v2"
LEGACY_DATASET_SCHEMAS: frozenset[str] = frozenset({"smma_taifex_bars.v1"})
DATE_FROM = "2026-03-19"
DATE_TO = "2026-07-24"
TIMEFRAMES_MINUTES: tuple[int, ...] = (60, 120, 240, 1440)
SUPPORTED_TIMEFRAMES_MINUTES: tuple[int, ...] = (2, *TIMEFRAMES_MINUTES)
ROOTS: tuple[str, ...] = ("TXF", "TMF")
_BAR_AGGREGATION_LAYOUT = BarAggregationLayout(
    open_index=5,
    high_index=6,
    low_index=7,
    close_index=8,
    sum_indices=(9,),
    first_indices=(10, 11, 12, 13),
    last_indices=(14, 15, 16, 17),
)


class DatasetGovernanceError(RuntimeError):
    """Raised when a query, dataset, or sidecar violates the frozen contract."""


@dataclass(frozen=True, slots=True)
class BarDataset:
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
    volume: np.ndarray
    bid_open: np.ndarray
    ask_open: np.ndarray
    bid_qty_open: np.ndarray
    ask_qty_open: np.ndarray
    bid_close: np.ndarray
    ask_close: np.ndarray
    bid_qty_close: np.ndarray
    ask_qty_close: np.ndarray
    reset: np.ndarray
    session_close: np.ndarray

    def __len__(self) -> int:
        return int(self.ts_ns.size)

    def validate(self) -> None:
        arrays = {
            "root": self.root,
            "timeframe_min": self.timeframe_min,
            "contract": self.contract,
            "trading_day": self.trading_day,
            "session": self.session,
            "ts_ns": self.ts_ns,
            "open": self.open,
            "high": self.high,
            "low": self.low,
            "close": self.close,
            "volume": self.volume,
            "bid_open": self.bid_open,
            "ask_open": self.ask_open,
            "bid_qty_open": self.bid_qty_open,
            "ask_qty_open": self.ask_qty_open,
            "bid_close": self.bid_close,
            "ask_close": self.ask_close,
            "bid_qty_close": self.bid_qty_close,
            "ask_qty_close": self.ask_qty_close,
            "reset": self.reset,
            "session_close": self.session_close,
        }
        lengths = {np.asarray(values).reshape(-1).size for values in arrays.values()}
        if len(lengths) != 1:
            raise DatasetGovernanceError(f"bar columns have mismatched lengths: {sorted(lengths)}")
        if len(self) == 0:
            raise DatasetGovernanceError("bar dataset is empty")
        if not set(np.unique(self.root)).issubset(set(ROOTS)):
            raise DatasetGovernanceError("bar dataset contains an unsupported root")
        if not set(int(value) for value in np.unique(self.timeframe_min)).issubset(set(SUPPORTED_TIMEFRAMES_MINUTES)):
            raise DatasetGovernanceError("bar dataset contains an unsupported timeframe")
        ohlc = np.column_stack((self.open, self.high, self.low, self.close))
        if not np.all(np.isfinite(ohlc)) or np.any(ohlc <= 0.0):
            raise DatasetGovernanceError("trade OHLC must be positive and finite")
        if np.any(self.high < np.maximum(self.open, self.close)) or np.any(
            self.low > np.minimum(self.open, self.close)
        ):
            raise DatasetGovernanceError("OHLC ordering is invalid")
        quote_valid = np.isfinite(self.bid_open) & np.isfinite(self.ask_open)
        quote_valid &= (self.bid_open > 0.0) & (self.ask_open >= self.bid_open)
        quote_valid &= np.isfinite(self.bid_close) & np.isfinite(self.ask_close)
        quote_valid &= (self.bid_close > 0.0) & (self.ask_close >= self.bid_close)
        if not np.all(quote_valid):
            raise DatasetGovernanceError("every bar must retain a valid executable bid/ask")
        quote_quantity_valid = np.isfinite(self.bid_qty_open) & np.isfinite(self.ask_qty_open)
        quote_quantity_valid &= (self.bid_qty_open > 0.0) & (self.ask_qty_open > 0.0)
        quote_quantity_valid &= np.isfinite(self.bid_qty_close) & np.isfinite(self.ask_qty_close)
        quote_quantity_valid &= (self.bid_qty_close > 0.0) & (self.ask_qty_close > 0.0)
        if not np.all(quote_quantity_valid):
            raise DatasetGovernanceError("every bar must retain positive bid/ask quantity")
        keys = {
            (str(root), int(timeframe), int(timestamp))
            for root, timeframe, timestamp in zip(
                self.root,
                self.timeframe_min,
                self.ts_ns,
                strict=True,
            )
        }
        if len(keys) != len(self):
            raise DatasetGovernanceError("duplicate root/timeframe/timestamp bars")

    def group(self, root: str, timeframe_min: int) -> "BarDataset":
        mask = (self.root == root) & (self.timeframe_min == int(timeframe_min))
        indices = np.flatnonzero(mask)
        if indices.size == 0:
            raise DatasetGovernanceError(f"no bars for {root}/{timeframe_min}m")
        return BarDataset(**{field: np.asarray(getattr(self, field))[indices] for field in self.__dataclass_fields__})


def _query_hash(query: str) -> str:
    return hashlib.sha256(query.encode("utf-8")).hexdigest()


def _guard_query(query: str) -> dict[str, Any]:
    """Run the repository query guard before any ClickHouse call."""
    from scripts.ch_query_guard import _evaluate_sql_guard

    result = _evaluate_sql_guard(query, allow_full_scan=False)
    if result.get("overall") == "fail":
        raise DatasetGovernanceError(f"ClickHouse query guard rejected SMMA export: {result.get('checks')}")
    return {
        "query_sha256": _query_hash(query),
        "guard_overall": result.get("overall"),
        "guard_checks": result.get("checks"),
    }


def _bar_query(
    timeframe_min: int,
    *,
    date_from: str,
    date_to: str,
    trading_window: TradingDateWindow | None = None,
) -> str:
    if timeframe_min not in SUPPORTED_TIMEFRAMES_MINUTES:
        raise ValueError(f"unsupported timeframe: {timeframe_min}")
    row_limit = _query_row_limit(timeframe_min)
    window = trading_window or build_trading_date_window(date_from, date_to)
    event_time_expression = "fromUnixTimestamp64Nano(exch_ts, 'Asia/Taipei')"
    trading_day_expression = clickhouse_trading_day_expression(event_time_expression, window)
    _day_session_predicate, night_session_predicate = clickhouse_taifex_session_predicates(event_time_expression)
    bucket_timestamp = clickhouse_taifex_bucket_timestamp(event_time_expression)
    contract_predicate = clickhouse_taifex_contract_predicate()
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
                {bucket_timestamp},
                INTERVAL {timeframe_min} MINUTE,
                toDateTime64('1970-01-01 15:00:00', 9, 'Asia/Taipei')
            ),
            toStartOfInterval(
                {bucket_timestamp},
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
            if(startsWith(symbol, 'TXF'), 'TXF', 'TMF') AS root,
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
            if(length(bids_price) > 0, bids_price[1], 0) AS bid_scaled,
            if(length(asks_price) > 0, asks_price[1], 0) AS ask_scaled,
            if(length(bids_vol) > 0, bids_vol[1], 0) AS bid_qty,
            if(length(asks_vol) > 0, asks_vol[1], 0) AS ask_qty
        FROM hft.market_data
        PREWHERE {contract_predicate}
          AND exch_ts >= toUnixTimestamp64Nano(range_start)
          AND exch_ts < toUnixTimestamp64Nano(range_end)
        WHERE ingest_ts >= toUnixTimestamp64Nano(range_start)
          AND ingest_ts < toUnixTimestamp64Nano(range_end)
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
    sumIf(volume, type = 'Tick') AS bar_trade_volume,
    argMinIf(
        bid_scaled,
        tuple(exch_ts, ingest_ts, seq_no),
        type = 'BidAsk' AND bid_scaled > 0 AND ask_scaled >= bid_scaled
            AND bid_qty > 0 AND ask_qty > 0
    ) / 1000000.0 AS bid_open,
    argMinIf(
        ask_scaled,
        tuple(exch_ts, ingest_ts, seq_no),
        type = 'BidAsk' AND bid_scaled > 0 AND ask_scaled >= bid_scaled
            AND bid_qty > 0 AND ask_qty > 0
    ) / 1000000.0 AS ask_open,
    argMinIf(
        bid_qty,
        tuple(exch_ts, ingest_ts, seq_no),
        type = 'BidAsk' AND bid_scaled > 0 AND ask_scaled >= bid_scaled
            AND bid_qty > 0 AND ask_qty > 0
    ) AS bid_qty_open,
    argMinIf(
        ask_qty,
        tuple(exch_ts, ingest_ts, seq_no),
        type = 'BidAsk' AND bid_scaled > 0 AND ask_scaled >= bid_scaled
            AND bid_qty > 0 AND ask_qty > 0
    ) AS ask_qty_open,
    argMaxIf(
        bid_scaled,
        tuple(exch_ts, ingest_ts, seq_no),
        type = 'BidAsk' AND bid_scaled > 0 AND ask_scaled >= bid_scaled
            AND bid_qty > 0 AND ask_qty > 0
    ) / 1000000.0 AS bid_close,
    argMaxIf(
        ask_scaled,
        tuple(exch_ts, ingest_ts, seq_no),
        type = 'BidAsk' AND bid_scaled > 0 AND ask_scaled >= bid_scaled
            AND bid_qty > 0 AND ask_qty > 0
    ) / 1000000.0 AS ask_close,
    argMaxIf(
        bid_qty,
        tuple(exch_ts, ingest_ts, seq_no),
        type = 'BidAsk' AND bid_scaled > 0 AND ask_scaled >= bid_scaled
            AND bid_qty > 0 AND ask_qty > 0
    ) AS bid_qty_close,
    argMaxIf(
        ask_qty,
        tuple(exch_ts, ingest_ts, seq_no),
        type = 'BidAsk' AND bid_scaled > 0 AND ask_scaled >= bid_scaled
            AND bid_qty > 0 AND ask_qty > 0
    ) AS ask_qty_close
FROM base
WHERE trading_day != toDate('1970-01-01')
GROUP BY root, contract, trading_day, session, bucket
HAVING countIf(type = 'Tick' AND price_scaled > 0) > 0
   AND countIf(
       type = 'BidAsk' AND bid_scaled > 0 AND ask_scaled >= bid_scaled
           AND bid_qty > 0 AND ask_qty > 0
   ) > 0
ORDER BY root, trading_day, session, bucket, contract
    LIMIT {row_limit}
""".strip()


def _query_row_limit(timeframe_min: int) -> int:
    """Bound result cardinality without truncating the governed 2-minute lane."""
    return 1_000_000 if int(timeframe_min) == 2 else 100_000


def _causal_front_contracts(
    rows: Sequence[Sequence[Any]],
) -> dict[tuple[str, str], str]:
    """Select each day from the previous observed trading day's volume."""
    volumes: dict[tuple[str, str, str], int] = {}
    days_by_root: dict[str, set[str]] = {}
    for row in rows:
        root, contract, trading_day = str(row[0]), str(row[1]), str(row[2])
        bar_volume = int(row[9])
        key = (root, trading_day, contract)
        volumes[key] = volumes.get(key, 0) + bar_volume
        days_by_root.setdefault(root, set()).add(trading_day)

    selected: dict[tuple[str, str], str] = {}
    for root, day_values in days_by_root.items():
        ordered_days = sorted(day_values)
        for previous_day, current_day in zip(ordered_days, ordered_days[1:], strict=False):
            prior = [
                (volume, contract)
                for (row_root, day, contract), volume in volumes.items()
                if row_root == root and day == previous_day
            ]
            if prior:
                _volume, contract = max(prior, key=lambda item: (item[0], item[1]))
                selected[(root, current_day)] = contract
    return selected


def _align_common_trading_days(dataset: BarDataset) -> BarDataset:
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
        raise DatasetGovernanceError("root/timeframe groups have no common trading days")
    all_days = set.union(*days_by_group.values())
    excluded_days = all_days - common_days
    indices = np.flatnonzero(np.isin(dataset.trading_day, sorted(common_days)))
    aligned_columns = {field: np.asarray(getattr(dataset, field))[indices] for field in dataset.__dataclass_fields__}
    aligned = BarDataset(**aligned_columns)

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
    result = BarDataset(**aligned_columns)
    result.validate()
    return result


def rows_to_bar_dataset(
    rows_by_timeframe: Mapping[int, Sequence[Sequence[Any]]],
    *,
    align_common: bool = True,
) -> BarDataset:
    """Apply causal front-month selection and discontinuity resets."""
    records: list[tuple[Any, ...]] = []
    for timeframe, rows in rows_by_timeframe.items():
        if timeframe not in SUPPORTED_TIMEFRAMES_MINUTES:
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
                    reset,
                    False,
                )
            )
            previous[root] = (ts_ns, contract, session)

    if not records:
        raise DatasetGovernanceError("causal front-month selection produced no bars")
    records.sort(key=lambda item: (item[0], item[1], item[5]))
    mutable = [list(record) for record in records]
    for index, record in enumerate(mutable):
        is_last = index == len(mutable) - 1
        next_differs = is_last or (
            mutable[index + 1][0] != record[0]
            or mutable[index + 1][1] != record[1]
            or mutable[index + 1][4] != record[4]
        )
        record[20] = next_differs
    columns = list(zip(*mutable, strict=True))
    dataset = BarDataset(
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
        volume=np.asarray(columns[10], dtype=np.float64),
        bid_open=np.asarray(columns[11], dtype=np.float64),
        ask_open=np.asarray(columns[12], dtype=np.float64),
        bid_qty_open=np.asarray(columns[13], dtype=np.float64),
        ask_qty_open=np.asarray(columns[14], dtype=np.float64),
        bid_close=np.asarray(columns[15], dtype=np.float64),
        ask_close=np.asarray(columns[16], dtype=np.float64),
        bid_qty_close=np.asarray(columns[17], dtype=np.float64),
        ask_qty_close=np.asarray(columns[18], dtype=np.float64),
        reset=np.asarray(columns[19], dtype=np.bool_),
        session_close=np.asarray(columns[20], dtype=np.bool_),
    )
    dataset.validate()
    return _align_common_trading_days(dataset) if align_common else dataset


def _restrict_to_full_sessions(
    dataset: BarDataset,
    window: TradingDateWindow,
    *,
    expected_timeframes: Sequence[int] | None = None,
) -> tuple[BarDataset, FullSessionEligibility]:
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
        roots=ROOTS,
        required_timeframes=required_timeframes,
    )
    if not eligibility.eligible_trading_dates:
        raise DatasetGovernanceError("no requested trading date has complete TXF/TMF day and night sessions")
    indices = np.flatnonzero(np.isin(dataset.trading_day, eligibility.eligible_trading_dates))
    restricted = BarDataset(
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


def save_governed_dataset(
    path: str | Path,
    dataset: BarDataset,
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
        "schema": DATASET_SCHEMA,
        "source": "hft.market_data",
        "source_type": "real",
        "generator": "research.combinatorial.smma_dataset",
        "owner": "research",
        "split": "full",
        "created_at": datetime.now(UTC).isoformat(),
        "dataset_id": f"smma_taifex_{min(days)}_{max(days)}",
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
        "roll_rule": "front contract selected by previous observed trading-day cumulative volume",
        "gap_rule": "contract/session change or unexpected gap forces reset and flat",
        "ohlc_rule": "Tick trade prints only; never mid",
        "quote_rule": (
            "first valid BidAsk price/quantity retained for next-bar execution; "
            "last valid BidAsk retained as decision-time evidence"
        ),
        "query_evidence": [dict(item) for item in query_evidence],
        "code_fingerprint": code_fingerprint,
        "content_sha256": content_hash,
        "data_fingerprint": content_hash,
        "generator_script": "research/combinatorial/smma_dataset.py",
        "generator_version": code_fingerprint,
        "parameters": {
            "requested_trading_date_from": requested_from,
            "requested_trading_date_to": requested_to,
            "symbols": sorted(str(value) for value in np.unique(dataset.root)),
            "timeframes_minutes": sorted(int(value) for value in np.unique(dataset.timeframe_min)),
            "price_scale_source": 1_000_000,
            "price_scale_output": 1,
        },
        "lineage": {
            "source": "hft.market_data",
            "query_evidence": [dict(item) for item in query_evidence],
            "calendar_name": window.calendar_name,
            "calendar_mapping_hash": window.calendar_mapping_hash,
            "code_fingerprint": code_fingerprint,
        },
        "data_ul": 5,
        "schema_version": 2,
        "governance_complete": eligibility is not None,
    }
    # Advisory source-layer provenance: which raw-ClickHouse audit this dataset was
    # built under. Never blocks the export -- a missing audit, or one whose range does
    # not cover the request, is recorded as an explicit "unstamped" /
    # "unstamped_range_mismatch" verdict rather than being silently omitted, so a
    # sidecar always says which of the three it is. Merged before the metadata hash,
    # so the stamp is covered by the hash and cannot be appended after the fact.
    payload.update(stamp_payload(load_latest_report(), requested_from=requested_from, requested_to=requested_to))
    payload.update(audit_claimed_ul(payload, DataUL.UL5))
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


def load_governed_dataset(path: str | Path) -> BarDataset:
    """Load only when the sidecar and full content hash match."""
    source = Path(path)
    sidecar = Path(str(source) + ".meta.json")
    if not source.exists() or not sidecar.exists():
        raise DatasetGovernanceError(f"dataset and sidecar are required: {source}")
    payload = json.loads(sidecar.read_text(encoding="utf-8"))
    expected_metadata_hash = str(payload.pop("metadata_hash", ""))
    if not expected_metadata_hash or _metadata_hash(payload) != expected_metadata_hash:
        raise DatasetGovernanceError("dataset sidecar fingerprint mismatch")
    if payload.get("schema") not in {DATASET_SCHEMA, *LEGACY_DATASET_SCHEMAS}:
        raise DatasetGovernanceError("dataset sidecar schema mismatch")
    actual_hash = _content_hash(source)
    if payload.get("content_sha256") != actual_hash:
        raise DatasetGovernanceError("dataset content fingerprint mismatch")
    with np.load(source, allow_pickle=False) as data:
        expected = set(BarDataset.__dataclass_fields__)
        if set(data.files) != expected:
            raise DatasetGovernanceError("dataset fields do not match BarDataset contract")
        dataset = BarDataset(**{field: np.asarray(data[field]) for field in expected})
    dataset.validate()
    if int(payload.get("row_count", -1)) != len(dataset):
        raise DatasetGovernanceError("dataset row count does not match sidecar")
    return dataset


def export_clickhouse_dataset(
    path: str | Path,
    *,
    code_fingerprint: str,
    client: Any | None = None,
    date_from: str = DATE_FROM,
    date_to: str = DATE_TO,
    timeframes_minutes: Sequence[int] = TIMEFRAMES_MINUTES,
) -> tuple[Path, Path]:
    """Export the frozen date window through guarded, read-only queries."""
    requested_timeframes = tuple(int(value) for value in timeframes_minutes)
    if (
        not requested_timeframes
        or len(set(requested_timeframes)) != len(requested_timeframes)
        or not set(requested_timeframes).issubset(set(SUPPORTED_TIMEFRAMES_MINUTES))
    ):
        raise ValueError("timeframes_minutes must be distinct supported values")
    if 60 not in requested_timeframes:
        raise ValueError("timeframes_minutes must include 60 for full-session eligibility")
    trading_window = build_trading_date_window(date_from, date_to)
    try:
        query_client = client if client is not None else get_ch_client()
    except Exception as exc:
        raise DatasetGovernanceError(f"ClickHouse client initialization failed: {type(exc).__name__}: {exc}") from exc
    base_timeframe = min(timeframe for timeframe in requested_timeframes if timeframe != 1440)
    query = _bar_query(
        base_timeframe,
        date_from=date_from,
        date_to=date_to,
        trading_window=trading_window,
    )
    base_evidence = _guard_query(query)
    row_limit = _query_row_limit(base_timeframe)
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
        raise DatasetGovernanceError(
            f"guarded ClickHouse export failed for {base_timeframe}m: {type(exc).__name__}: {exc}"
        ) from exc
    base_rows = list(result.result_rows)
    if len(base_rows) >= row_limit:
        raise DatasetGovernanceError(f"{base_timeframe}m export reached row limit; refusing possible truncation")
    rows_by_timeframe: dict[int, Sequence[Sequence[Any]]] = {base_timeframe: base_rows}
    for timeframe in requested_timeframes:
        if timeframe == base_timeframe:
            continue
        rows_by_timeframe[timeframe] = reaggregate_taifex_bar_rows(
            base_rows,
            source_timeframe_min=base_timeframe,
            target_timeframe_min=timeframe,
            layout=_BAR_AGGREGATION_LAYOUT,
        )
    evidence: list[dict[str, Any]] = []
    for timeframe in requested_timeframes:
        derived = timeframe != base_timeframe
        evidence.append(
            {
                **base_evidence,
                "timeframe_min": timeframe,
                "result_rows": len(rows_by_timeframe[timeframe]),
                "derived": derived,
                "derived_from_timeframe_min": base_timeframe if derived else None,
                "derivation": "causal_bar_reaggregation.v1" if derived else None,
            }
        )
    selected = rows_to_bar_dataset(rows_by_timeframe, align_common=False)
    dataset, eligibility = _restrict_to_full_sessions(
        selected,
        trading_window,
        expected_timeframes=requested_timeframes,
    )
    return save_governed_dataset(
        path,
        dataset,
        query_evidence=evidence,
        code_fingerprint=code_fingerprint,
        requested_date_from=date_from,
        requested_date_to=date_to,
        trading_window=trading_window,
        eligibility=eligibility,
    )
