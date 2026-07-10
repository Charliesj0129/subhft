"""Direct ClickHouse reconstruction for missing NWO day-session bars.

This module transfers only trade prints and top-of-book prices. The resulting
bars intentionally mirror ``ml_rsi_zeiierman_v0.bars.build_bars`` so expanded
research can fill isolated raw-data gaps without claiming a full-depth export.
"""

from __future__ import annotations

import subprocess
from collections.abc import Sequence
from datetime import date as date_type
from pathlib import Path
from typing import Any

import numpy as np

from research.experiments.validations.ml_rsi_zeiierman_v0.bars import (
    MAX_DAY_SPAN_FRAC,
    OUTLIER_BAND_FRAC,
    Bars,
)
from research.t1.regime_viability import NS_PER_MINUTE, _session_start_ns

PRICE_SCALE = 1_000_000.0
DAY_SESSION_MINUTES = 285


def _empty_bars(contract: str) -> Bars:
    return Bars(
        open=np.array([], dtype=float),
        high=np.array([], dtype=float),
        low=np.array([], dtype=float),
        close=np.array([], dtype=float),
        volume=np.array([], dtype=float),
        date=np.array([], dtype=str),
        is_session_close=np.array([], dtype=bool),
        contract=contract,
        bid_open=np.array([], dtype=float),
        ask_open=np.array([], dtype=float),
    )


def build_day_bars_from_rows(
    *,
    tick_ts: np.ndarray,
    tick_px: np.ndarray,
    tick_qty: np.ndarray,
    quote_ts: np.ndarray,
    quote_bid: np.ndarray,
    quote_ask: np.ndarray,
    date: str,
    contract: str,
    bar_min: int = 5,
    min_bars_per_day: int = 10,
) -> Bars:
    """Build one day with the frozen bar and as-of BBO semantics."""
    tick_ts = np.asarray(tick_ts, dtype=np.int64)
    tick_px = np.asarray(tick_px, dtype=float)
    tick_qty = np.asarray(tick_qty, dtype=float)
    quote_ts = np.asarray(quote_ts, dtype=np.int64)
    quote_bid = np.asarray(quote_bid, dtype=float)
    quote_ask = np.asarray(quote_ask, dtype=float)
    if not (len(tick_ts) == len(tick_px) == len(tick_qty)):
        raise ValueError("tick columns must have identical lengths")
    if not (len(quote_ts) == len(quote_bid) == len(quote_ask)):
        raise ValueError("quote columns must have identical lengths")

    s0 = _session_start_ns(date, hour=8, minute=45)
    s_end = s0 + DAY_SESSION_MINUTES * NS_PER_MINUTE
    tick_order = np.argsort(tick_ts, kind="stable")
    tick_ts, tick_px, tick_qty = (
        tick_ts[tick_order],
        tick_px[tick_order],
        tick_qty[tick_order],
    )
    valid_ticks = (tick_ts >= s0) & (tick_ts < s_end) & (tick_px > 0) & (tick_qty > 0)
    tick_ts, tick_px, tick_qty = (
        tick_ts[valid_ticks],
        tick_px[valid_ticks],
        tick_qty[valid_ticks],
    )
    if tick_ts.size < 50:
        return _empty_bars(contract)

    median = float(np.median(tick_px))
    keep = np.abs(tick_px - median) <= OUTLIER_BAND_FRAC * median
    tick_ts, tick_px, tick_qty = tick_ts[keep], tick_px[keep], tick_qty[keep]
    if tick_ts.size < 50:
        return _empty_bars(contract)
    low_q, high_q = np.percentile(tick_px, [0.5, 99.5])
    if (high_q - low_q) > MAX_DAY_SPAN_FRAC * median:
        return _empty_bars(contract)

    quote_valid = (quote_bid > 0) & (quote_ask >= quote_bid) & ((quote_ask - quote_bid) < 100.0)
    quote_ts, quote_bid, quote_ask = (
        quote_ts[quote_valid],
        quote_bid[quote_valid],
        quote_ask[quote_valid],
    )
    quote_order = np.argsort(quote_ts, kind="stable")
    quote_ts, quote_bid, quote_ask = (
        quote_ts[quote_order],
        quote_bid[quote_order],
        quote_ask[quote_order],
    )

    bar_ns = bar_min * NS_PER_MINUTE
    n_slots = DAY_SESSION_MINUTES // bar_min
    slot = np.clip(((tick_ts - s0) // bar_ns).astype(int), 0, n_slots - 1)
    rows: list[tuple[int, float, float, float, float, float, float, float]] = []
    for slot_index in range(n_slots):
        selected = slot == slot_index
        if not np.any(selected):
            continue
        prices = tick_px[selected]
        if prices.size >= 20:
            winsor_low, winsor_high = np.percentile(prices, [1, 99])
            prices = np.clip(prices, winsor_low, winsor_high)
        slot_open = s0 + slot_index * bar_ns
        quote_index = int(np.searchsorted(quote_ts, slot_open, side="right")) - 1
        bid = float(quote_bid[quote_index]) if quote_index >= 0 else np.nan
        ask = float(quote_ask[quote_index]) if quote_index >= 0 else np.nan
        rows.append(
            (
                slot_index,
                float(prices[0]),
                float(prices.max()),
                float(prices.min()),
                float(prices[-1]),
                float(tick_qty[selected].sum()),
                bid,
                ask,
            )
        )
    if len(rows) < min_bars_per_day:
        return _empty_bars(contract)

    return Bars(
        open=np.asarray([row[1] for row in rows]),
        high=np.asarray([row[2] for row in rows]),
        low=np.asarray([row[3] for row in rows]),
        close=np.asarray([row[4] for row in rows]),
        volume=np.asarray([row[5] for row in rows]),
        date=np.asarray([date] * len(rows)),
        is_session_close=np.asarray(
            [index == len(rows) - 1 for index in range(len(rows))], dtype=bool
        ),
        contract=contract,
        bid_open=np.asarray([row[6] for row in rows]),
        ask_open=np.asarray([row[7] for row in rows]),
    )


def merge_contract_bars(parts: Sequence[Bars]) -> Bars:
    """Merge non-overlapping date slices for one contract."""
    if not parts:
        raise ValueError("at least one Bars part is required")
    contract = parts[0].contract
    seen_dates: set[str] = set()
    for part in parts:
        if part.contract != contract:
            raise ValueError("all Bars parts must use the same contract")
        part_dates = {str(date) for date in part.date}
        overlap = seen_dates & part_dates
        if overlap:
            raise ValueError(f"duplicate dates across Bars parts: {sorted(overlap)}")
        seen_dates.update(part_dates)

    fields = ("open", "high", "low", "close", "volume", "date", "is_session_close")
    joined = {field: np.concatenate([np.asarray(getattr(part, field)) for part in parts]) for field in fields}
    bid_open = np.concatenate(
        [np.asarray(part.bid_open if part.bid_open is not None else np.full(len(part.date), np.nan)) for part in parts]
    )
    ask_open = np.concatenate(
        [np.asarray(part.ask_open if part.ask_open is not None else np.full(len(part.date), np.nan)) for part in parts]
    )
    order = np.argsort(joined["date"], kind="stable")
    return Bars(
        open=joined["open"][order],
        high=joined["high"][order],
        low=joined["low"][order],
        close=joined["close"][order],
        volume=joined["volume"][order],
        date=joined["date"][order],
        is_session_close=joined["is_session_close"][order],
        contract=contract,
        bid_open=bid_open[order],
        ask_open=ask_open[order],
    )


def load_clickhouse_day_bars(
    client: Any,
    *,
    symbol: str,
    date: str,
    bar_min: int = 5,
) -> Bars:
    """Aggregate frozen OHLCV rules in ClickHouse and reconstruct one day."""
    common_where = """
        symbol = %(symbol)s
        AND toDate(fromUnixTimestamp64Nano(ingest_ts), 'Asia/Taipei') = %(date)s
        AND fromUnixTimestamp64Nano(exch_ts, 'Asia/Taipei') >=
            toDateTime64(%(extract_start)s, 9, 'Asia/Taipei')
        AND fromUnixTimestamp64Nano(exch_ts, 'Asia/Taipei') <
            toDateTime64(%(extract_end)s, 9, 'Asia/Taipei')
    """
    parameters = {
        "symbol": symbol,
        "date": date,
        "extract_start": f"{date} 08:30:00",
        "extract_end": f"{date} 14:00:00",
    }
    settings = {"max_memory_usage": 2_500_000_000, "max_threads": 2}
    aggregate_parameters = {
        **parameters,
        "session_start": f"{date} 08:45:00",
        "session_ns": DAY_SESSION_MINUTES * NS_PER_MINUTE,
        "bar_ns": bar_min * NS_PER_MINUTE,
    }
    bar_rows = client.query(
        f"""
        WITH
            toUnixTimestamp64Nano(
                toDateTime64(%(session_start)s, 9, 'Asia/Taipei')
            ) AS session_start_ns,
            base AS (
                SELECT
                    exch_ts,
                    ingest_ts,
                    seq_no,
                    price_scaled / 1000000.0 AS px,
                    volume AS qty,
                    intDiv(exch_ts - session_start_ns, %(bar_ns)s) AS slot
                FROM hft.market_data
                WHERE type = 'Tick'
                  AND {common_where}
                  AND exch_ts >= session_start_ns
                  AND exch_ts < session_start_ns + %(session_ns)s
                  AND price_scaled > 0
                  AND volume > 0
            ),
            day_stats AS (
                SELECT
                    count() AS raw_count,
                    quantileExactWeightedInterpolated(0.5)(px, 1) AS med
                FROM base
            ),
            filtered AS (
                SELECT base.*
                FROM base CROSS JOIN day_stats
                WHERE raw_count >= 50 AND abs(px - med) <= 0.025 * med
            ),
            plausibility AS (
                SELECT
                    count() AS clean_count,
                    quantileExactWeightedInterpolated(0.005)(px, 1) AS lo,
                    quantileExactWeightedInterpolated(0.995)(px, 1) AS hi
                FROM filtered
            ),
            bar_stats AS (
                SELECT
                    slot,
                    count() AS bar_count,
                    quantileExactWeightedInterpolated(0.01)(px, 1) AS q1,
                    quantileExactWeightedInterpolated(0.99)(px, 1) AS q99
                FROM filtered
                GROUP BY slot
            )
        SELECT
            slot,
            argMin(
                if(bar_count >= 20, greatest(q1, least(q99, px)), px),
                tuple(exch_ts, ingest_ts, seq_no)
            ) AS open,
            max(if(bar_count >= 20, greatest(q1, least(q99, px)), px)) AS high,
            min(if(bar_count >= 20, greatest(q1, least(q99, px)), px)) AS low,
            argMax(
                if(bar_count >= 20, greatest(q1, least(q99, px)), px),
                tuple(exch_ts, ingest_ts, seq_no)
            ) AS close,
            sum(qty) AS volume
        FROM filtered
        INNER JOIN bar_stats USING slot
        CROSS JOIN plausibility
        CROSS JOIN day_stats
        WHERE clean_count >= 50 AND hi - lo <= 0.03 * med
        GROUP BY slot, bar_count, q1, q99
        ORDER BY slot
        """,
        parameters=aggregate_parameters,
        settings=settings,
    ).result_rows
    quote_parameters = {
        **parameters,
        "session_start": f"{date} 08:45:00",
        "slot_count": DAY_SESSION_MINUTES // bar_min,
        "bar_ns": bar_min * NS_PER_MINUTE,
    }
    quotes = client.query(
        f"""
        WITH toUnixTimestamp64Nano(
            toDateTime64(%(session_start)s, 9, 'Asia/Taipei')
        ) AS session_start_ns
        SELECT slots.slot_index, quotes.bid_scaled, quotes.ask_scaled
        FROM (
            SELECT
                %(symbol)s AS symbol,
                number AS slot_index,
                session_start_ns + number * %(bar_ns)s AS slot_ts
            FROM numbers(%(slot_count)s)
            ORDER BY slot_ts
        ) AS slots
        ASOF LEFT JOIN (
            SELECT
                symbol,
                exch_ts,
                bids_price[1] AS bid_scaled,
                asks_price[1] AS ask_scaled
            FROM hft.market_data
            WHERE type = 'BidAsk'
              AND length(bids_price) > 0
              AND length(asks_price) > 0
              AND bids_price[1] > 0
              AND asks_price[1] >= bids_price[1]
              AND asks_price[1] - bids_price[1] < 100000000
              AND {common_where}
            ORDER BY symbol, exch_ts, ingest_ts, seq_no
        ) AS quotes
          ON slots.symbol = quotes.symbol AND slots.slot_ts >= quotes.exch_ts
        ORDER BY slots.slot_index
        """,
        parameters=quote_parameters,
        settings=settings,
    ).result_rows
    if len(bar_rows) < 10:
        return _empty_bars(symbol.lower())
    quote_by_slot = {
        int(slot): (float(bid) / PRICE_SCALE, float(ask) / PRICE_SCALE)
        for slot, bid, ask in quotes
        if bid is not None and ask is not None and int(bid) > 0 and int(ask) >= int(bid)
    }
    bid_open = np.asarray(
        [quote_by_slot.get(int(row[0]), (np.nan, np.nan))[0] for row in bar_rows]
    )
    ask_open = np.asarray(
        [quote_by_slot.get(int(row[0]), (np.nan, np.nan))[1] for row in bar_rows]
    )
    return Bars(
        open=np.asarray([float(row[1]) for row in bar_rows]),
        high=np.asarray([float(row[2]) for row in bar_rows]),
        low=np.asarray([float(row[3]) for row in bar_rows]),
        close=np.asarray([float(row[4]) for row in bar_rows]),
        volume=np.asarray([float(row[5]) for row in bar_rows]),
        date=np.asarray([date] * len(bar_rows)),
        is_session_close=np.asarray(
            [index == len(bar_rows) - 1 for index in range(len(bar_rows))], dtype=bool
        ),
        contract=symbol.lower(),
        bid_open=bid_open,
        ask_open=ask_open,
    )


def _validated_literals(symbol: str, date: str, bar_min: int) -> tuple[str, str, int]:
    if not symbol.isalnum() or symbol.upper() != symbol:
        raise ValueError("symbol must be uppercase alphanumeric")
    date_type.fromisoformat(date)
    if bar_min <= 0 or DAY_SESSION_MINUTES % bar_min:
        raise ValueError("bar_min must evenly divide the frozen day session")
    return symbol, date, bar_min


def load_docker_day_bars(*, symbol: str, date: str, bar_min: int = 5) -> Bars:
    """Use the native container client to avoid large HTTP decode overhead."""
    symbol, date, bar_min = _validated_literals(symbol, date, bar_min)
    bar_ns = bar_min * NS_PER_MINUTE
    session_ns = DAY_SESSION_MINUTES * NS_PER_MINUTE
    session_start = f"{date} 08:45:00"
    extract_start = f"{date} 08:30:00"
    extract_end = f"{date} 14:00:00"
    common_where = f"""
        symbol = '{symbol}'
        AND toDate(fromUnixTimestamp64Nano(ingest_ts), 'Asia/Taipei') = '{date}'
        AND fromUnixTimestamp64Nano(exch_ts, 'Asia/Taipei') >=
            toDateTime64('{extract_start}', 9, 'Asia/Taipei')
        AND fromUnixTimestamp64Nano(exch_ts, 'Asia/Taipei') <
            toDateTime64('{extract_end}', 9, 'Asia/Taipei')
    """
    bar_query = f"""
        WITH
            toUnixTimestamp64Nano(
                toDateTime64('{session_start}', 9, 'Asia/Taipei')
            ) AS session_start_ns,
            base AS (
                SELECT exch_ts, ingest_ts, seq_no,
                    price_scaled / 1000000.0 AS px, volume AS qty,
                    intDiv(exch_ts - session_start_ns, {bar_ns}) AS slot
                FROM hft.market_data
                WHERE type = 'Tick' AND {common_where}
                  AND exch_ts >= session_start_ns
                  AND exch_ts < session_start_ns + {session_ns}
                  AND price_scaled > 0 AND volume > 0
            ),
            day_stats AS (
                SELECT count() AS raw_count,
                    quantileExactWeightedInterpolated(0.5)(px, 1) AS med
                FROM base
            ),
            filtered AS (
                SELECT base.* FROM base CROSS JOIN day_stats
                WHERE raw_count >= 50 AND abs(px - med) <= 0.025 * med
            ),
            plausibility AS (
                SELECT count() AS clean_count,
                    quantileExactWeightedInterpolated(0.005)(px, 1) AS lo,
                    quantileExactWeightedInterpolated(0.995)(px, 1) AS hi
                FROM filtered
            ),
            bar_stats AS (
                SELECT slot, count() AS bar_count,
                    quantileExactWeightedInterpolated(0.01)(px, 1) AS q1,
                    quantileExactWeightedInterpolated(0.99)(px, 1) AS q99
                FROM filtered GROUP BY slot
            )
        SELECT slot,
            argMin(if(bar_count >= 20, greatest(q1, least(q99, px)), px),
                tuple(exch_ts, ingest_ts, seq_no)) AS open,
            max(if(bar_count >= 20, greatest(q1, least(q99, px)), px)) AS high,
            min(if(bar_count >= 20, greatest(q1, least(q99, px)), px)) AS low,
            argMax(if(bar_count >= 20, greatest(q1, least(q99, px)), px),
                tuple(exch_ts, ingest_ts, seq_no)) AS close,
            sum(qty) AS volume
        FROM filtered INNER JOIN bar_stats USING slot
        CROSS JOIN plausibility CROSS JOIN day_stats
        WHERE clean_count >= 50 AND hi - lo <= 0.03 * med
        GROUP BY slot, bar_count, q1, q99 ORDER BY slot
        FORMAT TabSeparated
    """
    slot_count = DAY_SESSION_MINUTES // bar_min
    quote_query = f"""
        WITH toUnixTimestamp64Nano(
            toDateTime64('{session_start}', 9, 'Asia/Taipei')
        ) AS session_start_ns
        SELECT slots.slot_index, quotes.bid_scaled, quotes.ask_scaled
        FROM (
            SELECT '{symbol}' AS symbol, number AS slot_index,
                session_start_ns + number * {bar_ns} AS slot_ts
            FROM numbers({slot_count}) ORDER BY slot_ts
        ) AS slots
        ASOF LEFT JOIN (
            SELECT symbol, exch_ts, bids_price[1] AS bid_scaled,
                asks_price[1] AS ask_scaled
            FROM hft.market_data
            WHERE type = 'BidAsk' AND length(bids_price) > 0
              AND length(asks_price) > 0 AND bids_price[1] > 0
              AND asks_price[1] >= bids_price[1]
              AND asks_price[1] - bids_price[1] < 100000000
              AND {common_where}
            ORDER BY symbol, exch_ts, ingest_ts, seq_no
        ) AS quotes
          ON slots.symbol = quotes.symbol AND slots.slot_ts >= quotes.exch_ts
        ORDER BY slots.slot_index FORMAT TabSeparated
    """

    def query_rows(query: str) -> list[list[str]]:
        result = subprocess.run(
            ["docker", "exec", "clickhouse", "clickhouse-client", "--query", query],
            check=True,
            capture_output=True,
            text=True,
            timeout=120,
        )
        return [line.split("\t") for line in result.stdout.splitlines() if line]

    bar_rows = query_rows(bar_query)
    if len(bar_rows) < 10:
        return _empty_bars(symbol.lower())
    quote_by_slot = {
        int(slot): (float(bid) / PRICE_SCALE, float(ask) / PRICE_SCALE)
        for slot, bid, ask in query_rows(quote_query)
        if bid not in {"", "\\N"} and ask not in {"", "\\N"}
        and float(bid) > 0 and float(ask) >= float(bid)
    }
    return Bars(
        open=np.asarray([float(row[1]) for row in bar_rows]),
        high=np.asarray([float(row[2]) for row in bar_rows]),
        low=np.asarray([float(row[3]) for row in bar_rows]),
        close=np.asarray([float(row[4]) for row in bar_rows]),
        volume=np.asarray([float(row[5]) for row in bar_rows]),
        date=np.asarray([date] * len(bar_rows)),
        is_session_close=np.asarray(
            [index == len(bar_rows) - 1 for index in range(len(bar_rows))], dtype=bool
        ),
        contract=symbol.lower(),
        bid_open=np.asarray(
            [quote_by_slot.get(int(row[0]), (np.nan, np.nan))[0] for row in bar_rows]
        ),
        ask_open=np.asarray(
            [quote_by_slot.get(int(row[0]), (np.nan, np.nan))[1] for row in bar_rows]
        ),
    )


def save_bars(path: Path, bars: Bars) -> None:
    """Persist reconstructed bars without embedding credentials."""
    path.parent.mkdir(parents=True, exist_ok=True)
    np.savez_compressed(
        path,
        open=bars.open,
        high=bars.high,
        low=bars.low,
        close=bars.close,
        volume=bars.volume,
        date=bars.date,
        is_session_close=bars.is_session_close,
        bid_open=bars.bid_open,
        ask_open=bars.ask_open,
        contract=np.asarray(bars.contract),
    )


def load_bars(path: Path) -> Bars:
    """Load a persisted DB-direct Bars artifact."""
    with np.load(path, allow_pickle=False) as data:
        return Bars(
            open=data["open"],
            high=data["high"],
            low=data["low"],
            close=data["close"],
            volume=data["volume"],
            date=data["date"],
            is_session_close=data["is_session_close"],
            contract=str(data["contract"]),
            bid_open=data["bid_open"],
            ask_open=data["ask_open"],
        )
