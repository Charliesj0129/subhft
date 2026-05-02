# Market Analysis Report Service — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build an automated market analysis pipeline that generates actionable trading intelligence from ClickHouse tick data and delivers structured Telegram reports with informed flow, precise price levels, and scenario planning.

**Architecture:** 5-stage pipeline (DataCollector → SignalEngine → ScenarioBuilder → ReportRenderer → Distributor) triggered by cron at day/night session close. Each stage communicates via typed dataclass contracts. Separate `ReportSender` for multi-channel Telegram delivery with retry.

**Tech Stack:** Python 3.12, ClickHouse (clickhouse-driver), aiohttp, structlog, pytest

**Spec:** `docs/superpowers/specs/2026-03-28-market-analysis-report-service-design.md`

---

### Task 1: Data Contracts (models.py)

**Files:**
- Create: `src/hft_platform/reports/__init__.py`
- Create: `src/hft_platform/reports/models.py`
- Create: `tests/unit/test_report_models.py`

- [ ] **Step 1: Create package init**

```python
# src/hft_platform/reports/__init__.py
"""Market analysis report pipeline."""
```

- [ ] **Step 2: Write test for data contracts**

```python
# tests/unit/test_report_models.py
"""Tests for report data contracts."""
from __future__ import annotations

import pytest

from hft_platform.reports.models import (
    Bar5m,
    ChannelConfig,
    DepthBar,
    FlowBar,
    KeyLevel,
    LargeTrade,
    PriceLevel,
    Scenario,
    ScenarioReport,
    SessionData,
    SignalReport,
)


class TestBar5m:
    def test_create_bar(self) -> None:
        bar = Bar5m(
            ts="2026-03-27 15:00:00",
            open=330490000,
            high=330600000,
            low=330020000,
            close=330340000,
            volume=1027,
            ticks=632,
        )
        assert bar.open == 330490000
        assert bar.ticks == 632

    def test_slots_prevents_extra_attrs(self) -> None:
        bar = Bar5m(ts="t", open=0, high=0, low=0, close=0, volume=0, ticks=0)
        with pytest.raises(AttributeError):
            bar.extra = 1  # type: ignore[attr-defined]


class TestFlowBar:
    def test_create_flow_bar(self) -> None:
        fb = FlowBar(
            ts="2026-03-27 15:00:00",
            ticks=632,
            total_vol=1027,
            uptick_vol=318,
            downtick_vol=448,
            flat_vol=261,
            ud_ratio=0.71,
            net_flow=-130,
        )
        assert fb.ud_ratio == pytest.approx(0.71)
        assert fb.net_flow == -130


class TestLargeTrade:
    def test_direction_field(self) -> None:
        lt = LargeTrade(
            ts="2026-03-27 21:58:48",
            price=324000000,
            volume=28,
            direction="sell",
        )
        assert lt.direction == "sell"
        assert lt.volume == 28


class TestSessionData:
    def test_create_minimal(self) -> None:
        sd = SessionData(
            session="night",
            symbol="TXFD6",
            date="2026-03-27",
            open=330490000,
            high=330490000,
            low=323750000,
            close=324380000,
            volume=58107,
            tick_count=38153,
            bars_5m=[],
            flow_5m=[],
            large_trades=[],
            spread_dist={},
            depth_imbalance=[],
        )
        assert sd.session == "night"
        assert sd.symbol == "TXFD6"


class TestSignalReport:
    def test_bias_field(self) -> None:
        fb_sell = FlowBar(ts="t", ticks=1, total_vol=1, uptick_vol=0, downtick_vol=1, flat_vol=0, ud_ratio=0.0, net_flow=-1)
        fb_buy = FlowBar(ts="t", ticks=1, total_vol=1, uptick_vol=1, downtick_vol=0, flat_vol=0, ud_ratio=99.0, net_flow=1)
        sd = SessionData(session="day", symbol="TXFD6", date="2026-03-27",
                         open=0, high=0, low=0, close=0, volume=0, tick_count=0,
                         bars_5m=[], flow_5m=[], large_trades=[], spread_dist={}, depth_imbalance=[])
        sr = SignalReport(
            session_data=sd,
            total_net_flow=-1581,
            ud_ratio_session=0.906,
            strongest_sell=fb_sell,
            strongest_buy=fb_buy,
            large_buy_volume=380,
            large_sell_volume=650,
            large_net=-270,
            key_large_trades=[],
            supports=[],
            resistances=[],
            bias="bearish",
            bias_confidence=0.75,
            rule_scores={"IF-01_session_ud": -0.8},
        )
        assert sr.bias == "bearish"
        assert sr.bias_confidence == pytest.approx(0.75)


class TestScenarioReport:
    def test_scenarios_list(self) -> None:
        s = Scenario(
            id="break_below_support",
            label="破底加速",
            probability="較高",
            condition="若破 32,375",
            target=320000000,
            description="目標看 32,000",
        )
        assert s.id == "break_below_support"


class TestChannelConfig:
    def test_frozen(self) -> None:
        ch = ChannelConfig(name="owner", chat_id="123", tier="paid", enabled=True)
        with pytest.raises(AttributeError):
            ch.enabled = False  # type: ignore[misc]
```

- [ ] **Step 3: Run test to verify it fails**

Run: `uv run pytest tests/unit/test_report_models.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'hft_platform.reports'`

- [ ] **Step 4: Implement models.py**

```python
# src/hft_platform/reports/models.py
"""Inter-stage data contracts for the report pipeline.

Price convention: All price fields use **platform scale (x10,000)**,
matching ``contracts.types.ScaledPrice``. The DataCollector converts
ClickHouse scale (x1,000,000) at the ingestion boundary.
"""
from __future__ import annotations

from dataclasses import dataclass

from hft_platform.contracts.types import ScaledPrice


# ── Stage 1 outputs ──────────────────────────────────────────────────

@dataclass(slots=True)
class Bar5m:
    """5-minute OHLCV bar."""
    ts: str
    open: ScaledPrice
    high: ScaledPrice
    low: ScaledPrice
    close: ScaledPrice
    volume: int
    ticks: int


@dataclass(slots=True)
class FlowBar:
    """5-minute uptick/downtick flow."""
    ts: str
    ticks: int
    total_vol: int
    uptick_vol: int
    downtick_vol: int
    flat_vol: int
    ud_ratio: float
    net_flow: int


@dataclass(slots=True)
class LargeTrade:
    """Single large trade event."""
    ts: str
    price: ScaledPrice
    volume: int
    direction: str  # "buy" | "sell" | "unknown"


@dataclass(slots=True)
class DepthBar:
    """Hourly L1 depth imbalance."""
    hour: int
    avg_bid_vol: float
    avg_ask_vol: float
    bid_ratio: float


@dataclass(slots=True)
class SessionData:
    """Output of DataCollector (Stage 1)."""
    session: str   # "day" | "night"
    symbol: str    # "TXFD6"
    date: str      # trading date, e.g. "2026-03-27"
    open: ScaledPrice
    high: ScaledPrice
    low: ScaledPrice
    close: ScaledPrice
    volume: int
    tick_count: int
    bars_5m: list[Bar5m]
    flow_5m: list[FlowBar]
    large_trades: list[LargeTrade]
    spread_dist: dict[int, int]
    depth_imbalance: list[DepthBar]


# ── Stage 2 outputs ──────────────────────────────────────────────────

@dataclass(slots=True)
class PriceLevel:
    """Support or resistance level."""
    price: ScaledPrice
    strength: float  # 0.0-1.0
    reason: str


@dataclass(slots=True)
class SignalReport:
    """Output of SignalEngine (Stage 2)."""
    session_data: SessionData
    total_net_flow: int
    ud_ratio_session: float
    strongest_sell: FlowBar
    strongest_buy: FlowBar
    large_buy_volume: int
    large_sell_volume: int
    large_net: int
    key_large_trades: list[LargeTrade]
    supports: list[PriceLevel]
    resistances: list[PriceLevel]
    bias: str       # "bearish" | "bullish" | "neutral"
    bias_confidence: float  # 0.0-1.0
    rule_scores: dict[str, float]


# ── Stage 3 outputs ──────────────────────────────────────────────────

@dataclass(slots=True)
class Scenario:
    """One branch of the scenario plan."""
    id: str
    label: str
    probability: str  # "較高" | "較低"
    condition: str
    target: ScaledPrice
    description: str


@dataclass(slots=True)
class KeyLevel:
    """Named support/resistance for display."""
    price: ScaledPrice
    label: str      # "S1", "R1"
    importance: int  # 1-3
    reason: str


@dataclass(slots=True)
class ScenarioReport:
    """Output of ScenarioBuilder (Stage 3)."""
    signal: SignalReport
    direction: str         # "偏空" | "偏多" | "中性"
    confidence_pct: int    # 60-80
    entry_zone: tuple[ScaledPrice, ScaledPrice]
    target: ScaledPrice
    stop_loss: ScaledPrice
    scenarios: list[Scenario]
    key_levels: list[KeyLevel]


# ── Distributor config ───────────────────────────────────────────────

@dataclass(slots=True, frozen=True)
class ChannelConfig:
    """Telegram channel routing config."""
    name: str
    chat_id: str
    tier: str    # "free" | "paid"
    enabled: bool
```

- [ ] **Step 5: Run test to verify it passes**

Run: `uv run pytest tests/unit/test_report_models.py -v`
Expected: all PASS

- [ ] **Step 6: Commit**

```bash
git add src/hft_platform/reports/__init__.py src/hft_platform/reports/models.py tests/unit/test_report_models.py
git commit -m "feat(reports): add data contract models for report pipeline"
```

---

### Task 2: Date Resolution + Pipeline CLI Skeleton (pipeline.py)

**Files:**
- Create: `src/hft_platform/reports/pipeline.py`
- Create: `tests/unit/test_report_pipeline.py`

- [ ] **Step 1: Write tests for date resolution and CLI arg parsing**

```python
# tests/unit/test_report_pipeline.py
"""Tests for report pipeline orchestrator and date resolution."""
from __future__ import annotations

from datetime import datetime

import pytest
from zoneinfo import ZoneInfo

from hft_platform.reports.pipeline import resolve_trading_date

TPE = ZoneInfo("Asia/Taipei")


class TestResolveTradingDate:
    def test_day_session_returns_today(self) -> None:
        now = datetime(2026, 3, 27, 13, 50, tzinfo=TPE)
        assert resolve_trading_date("day", now=now) == "2026-03-27"

    def test_night_session_at_0510_returns_yesterday(self) -> None:
        """Cron fires at 05:10 on 3/28 → reports 3/27 night session."""
        now = datetime(2026, 3, 28, 5, 10, tzinfo=TPE)
        assert resolve_trading_date("night", now=now) == "2026-03-27"

    def test_night_session_at_1530_returns_today(self) -> None:
        """If run at 15:30, the night session just opened today."""
        now = datetime(2026, 3, 27, 15, 30, tzinfo=TPE)
        assert resolve_trading_date("night", now=now) == "2026-03-27"

    def test_night_session_at_0100_returns_yesterday(self) -> None:
        now = datetime(2026, 3, 28, 1, 0, tzinfo=TPE)
        assert resolve_trading_date("night", now=now) == "2026-03-27"

    def test_night_session_at_exactly_1500_returns_today(self) -> None:
        now = datetime(2026, 3, 27, 15, 0, tzinfo=TPE)
        assert resolve_trading_date("night", now=now) == "2026-03-27"

    def test_night_session_at_1459_returns_yesterday(self) -> None:
        now = datetime(2026, 3, 27, 14, 59, tzinfo=TPE)
        assert resolve_trading_date("night", now=now) == "2026-03-26"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/unit/test_report_pipeline.py -v`
Expected: FAIL — `ModuleNotFoundError`

- [ ] **Step 3: Implement pipeline.py**

```python
# src/hft_platform/reports/pipeline.py
"""Report pipeline orchestrator and CLI entry point.

Usage:
    python -m hft_platform.reports.pipeline --session day
    python -m hft_platform.reports.pipeline --session night --date 2026-03-27
    python -m hft_platform.reports.pipeline --session night --dry-run
    python -m hft_platform.reports.pipeline --session night --debug
"""
from __future__ import annotations

import argparse
import asyncio
import os
import sys
from datetime import datetime, timedelta

import structlog
from zoneinfo import ZoneInfo

logger = structlog.get_logger(__name__)

TPE = ZoneInfo("Asia/Taipei")


def resolve_trading_date(session: str, *, now: datetime | None = None) -> str:
    """Determine the trading date for a report.

    Day session: today (cron runs at 13:50 same day).
    Night session: the date the session OPENED (15:00 side).
      - At 05:10 on 3/28 → 3/27 (yesterday)
      - At 15:30 on 3/27 → 3/27 (today, session just opened)
    """
    if now is None:
        now = datetime.now(TPE)
    if session == "day":
        return now.strftime("%Y-%m-%d")
    # Night: if before 15:00, the session opened yesterday
    if now.hour < 15:
        return (now.date() - timedelta(days=1)).isoformat()
    return now.strftime("%Y-%m-%d")


async def run_pipeline(
    session: str,
    date: str,
    *,
    dry_run: bool = False,
    debug: bool = False,
) -> None:
    """Execute the full report pipeline."""
    logger.info(
        "report_pipeline_start",
        session=session,
        date=date,
        dry_run=dry_run,
        debug=debug,
    )

    # Stages will be wired in subsequent tasks
    # Stage 1: DataCollector
    # Stage 2: SignalEngine
    # Stage 3: ScenarioBuilder
    # Stage 4: ReportRenderer
    # Stage 5: Distributor

    logger.info("report_pipeline_complete", session=session, date=date)


def main() -> None:
    parser = argparse.ArgumentParser(description="Market Analysis Report Pipeline")
    parser.add_argument(
        "--session",
        required=True,
        choices=["day", "night"],
        help="Trading session to report on",
    )
    parser.add_argument(
        "--date",
        default=None,
        help="Trading date (YYYY-MM-DD). Auto-resolved if omitted.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Generate report but do not send to Telegram",
    )
    parser.add_argument(
        "--debug",
        action="store_true",
        help="Print rendered messages to stdout",
    )
    args = parser.parse_args()

    if os.environ.get("HFT_REPORT_ENABLED", "0") != "1" and not args.dry_run and not args.debug:
        logger.warning("report_disabled", hint="Set HFT_REPORT_ENABLED=1 to enable")
        sys.exit(0)

    date = args.date or resolve_trading_date(args.session)
    logger.info("report_date_resolved", session=args.session, date=date, explicit=args.date is not None)

    asyncio.run(run_pipeline(args.session, date, dry_run=args.dry_run, debug=args.debug))


if __name__ == "__main__":
    main()
```

- [ ] **Step 4: Add `__main__.py` for `python -m` support**

```python
# src/hft_platform/reports/__main__.py
"""Allow ``python -m hft_platform.reports.pipeline``."""
from hft_platform.reports.pipeline import main

main()
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `uv run pytest tests/unit/test_report_pipeline.py -v`
Expected: all 6 PASS

- [ ] **Step 6: Commit**

```bash
git add src/hft_platform/reports/pipeline.py src/hft_platform/reports/__main__.py tests/unit/test_report_pipeline.py
git commit -m "feat(reports): add pipeline orchestrator with date resolution"
```

---

### Task 3: DataCollector — ClickHouse Queries (collector.py)

**Files:**
- Create: `src/hft_platform/reports/collector.py`
- Create: `tests/unit/test_report_collector.py`

**Context:** The DataCollector issues 6 ClickHouse queries (Q1-Q6 from spec §5) and returns `SessionData`. It converts CH price scale (x1,000,000) → platform scale (x10,000) at the boundary.

Existing CH connection pattern (from `scripts/weekly_summary.py`): `from clickhouse_driver import Client; client = Client(host=...)`.

The `monitor/_types.py:446-448` defines: `CH_PRICE_SCALE = 1_000_000`, `PLATFORM_SCALE = 10_000`, `CH_TO_PLATFORM_DIVISOR = 100`.

- [ ] **Step 1: Write tests for price conversion and session filter**

```python
# tests/unit/test_report_collector.py
"""Tests for DataCollector."""
from __future__ import annotations

import pytest

from hft_platform.reports.collector import (
    DataCollector,
    _ch_to_platform,
    _day_filter,
    _night_filter,
)


class TestChToPlatform:
    def test_convert_32375(self) -> None:
        # CH: 32375 * 1_000_000 = 32_375_000_000
        # Platform: 32375 * 10_000 = 323_750_000
        assert _ch_to_platform(32_375_000_000) == 323_750_000

    def test_convert_zero(self) -> None:
        assert _ch_to_platform(0) == 0

    def test_convert_round_trip(self) -> None:
        # 33049 points → CH = 33_049_000_000 → platform = 330_490_000
        ch = 33_049_000_000
        plat = _ch_to_platform(ch)
        assert plat == 330_490_000
        # Back to human: 330_490_000 / 10_000 = 33_049.0
        assert plat / 10_000 == 33_049.0


class TestSessionFilters:
    def test_day_filter_contains_date(self) -> None:
        f = _day_filter("2026-03-27")
        assert "2026-03-27" in f
        assert "07:00:00" in f
        assert "13:45:00" in f
        assert "Asia/Taipei" in f

    def test_night_filter_spans_midnight(self) -> None:
        f = _night_filter("2026-03-27")
        assert "2026-03-27 15:00:00" in f
        assert "INTERVAL 14 HOUR" in f
        assert "Asia/Taipei" in f

    def test_night_filter_does_not_use_toDate(self) -> None:
        """Spec requires exch_ts range, NOT toDate() which has TZ issues."""
        f = _night_filter("2026-03-27")
        assert "toDate(" not in f
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/unit/test_report_collector.py -v`
Expected: FAIL — `ModuleNotFoundError`

- [ ] **Step 3: Implement collector.py**

```python
# src/hft_platform/reports/collector.py
"""Stage 1: DataCollector — query ClickHouse and produce SessionData.

Converts ClickHouse price scale (x1,000,000) to platform scale (x10,000)
at the read boundary. All downstream pipeline stages use platform scale.
"""
from __future__ import annotations

import os
from typing import Any

import structlog

from hft_platform.monitor._types import CH_TO_PLATFORM_DIVISOR
from hft_platform.reports.models import (
    Bar5m,
    DepthBar,
    FlowBar,
    LargeTrade,
    SessionData,
)

logger = structlog.get_logger(__name__)

TZ = "Asia/Taipei"
TS_EXPR = f"toDateTime64(exch_ts/1e9, 3, '{TZ}')"
CH_SETTINGS = "SETTINGS max_memory_usage = 2000000000"

LARGE_TRADE_THRESHOLD: dict[str, int] = {
    "TXFD6": 10,
    "TMFD6": 30,
    "MXFD6": 30,
}


def _ch_to_platform(ch_price: int) -> int:
    """Convert ClickHouse scale (x1,000,000) to platform scale (x10,000)."""
    return ch_price // CH_TO_PLATFORM_DIVISOR


def _day_filter(date: str) -> str:
    """SQL filter for day session: 07:00 ~ 13:45 CST on given date."""
    return (
        f"{TS_EXPR} >= toDateTime64('{date} 07:00:00', 3, '{TZ}') AND "
        f"{TS_EXPR} < toDateTime64('{date} 13:45:00', 3, '{TZ}')"
    )


def _night_filter(date: str) -> str:
    """SQL filter for night session: date 15:00 ~ date+1 05:00 CST."""
    return (
        f"{TS_EXPR} >= toDateTime64('{date} 15:00:00', 3, '{TZ}') AND "
        f"{TS_EXPR} < toDateTime64('{date} 15:00:00', 3, '{TZ}') + INTERVAL 14 HOUR"
    )


class DataCollector:
    """Queries ClickHouse and returns SessionData."""

    def __init__(self, ch_host: str = "") -> None:
        from clickhouse_driver import Client

        host = ch_host or os.environ.get("HFT_CLICKHOUSE_HOST", "localhost")
        self._client = Client(host=host)

    def _session_filter(self, session: str, date: str) -> str:
        if session == "day":
            return _day_filter(date)
        return _night_filter(date)

    def _query(self, sql: str, params: dict[str, Any] | None = None) -> list[tuple[Any, ...]]:
        return self._client.execute(f"{sql}\n{CH_SETTINGS}", params or {})

    def collect(self, session: str, date: str, symbol: str = "TXFD6") -> SessionData:
        """Run all queries and assemble SessionData."""
        sf = self._session_filter(session, date)
        base_where = f"symbol = '{symbol}' AND type = 'Tick' AND price_scaled > 0 AND {sf}"
        ba_where = f"symbol = '{symbol}' AND type = 'BidAsk' AND length(bids_price) > 0 AND length(asks_price) > 0 AND {sf}"

        ohlcv = self._query_ohlcv(base_where)
        bars = self._query_bars_5m(base_where)
        flow = self._query_flow_5m(base_where, sf)
        large = self._query_large_trades(base_where, symbol)
        spread = self._query_spread_dist(ba_where)
        depth = self._query_depth_imbalance(ba_where)

        return SessionData(
            session=session,
            symbol=symbol,
            date=date,
            open=ohlcv["open"],
            high=ohlcv["high"],
            low=ohlcv["low"],
            close=ohlcv["close"],
            volume=ohlcv["volume"],
            tick_count=ohlcv["ticks"],
            bars_5m=bars,
            flow_5m=flow,
            large_trades=large,
            spread_dist=spread,
            depth_imbalance=depth,
        )

    def _query_ohlcv(self, where: str) -> dict[str, int]:
        rows = self._query(f"""
            SELECT
                argMin(price_scaled, exch_ts) AS open_p,
                max(price_scaled) AS high_p,
                min(price_scaled) AS low_p,
                argMax(price_scaled, exch_ts) AS close_p,
                sum(volume) AS vol,
                count() AS ticks
            FROM hft.market_data WHERE {where}
        """)
        if not rows or rows[0][5] == 0:
            return {"open": 0, "high": 0, "low": 0, "close": 0, "volume": 0, "ticks": 0}
        r = rows[0]
        return {
            "open": _ch_to_platform(r[0]),
            "high": _ch_to_platform(r[1]),
            "low": _ch_to_platform(r[2]),
            "close": _ch_to_platform(r[3]),
            "volume": r[4],
            "ticks": r[5],
        }

    def _query_bars_5m(self, where: str) -> list[Bar5m]:
        rows = self._query(f"""
            SELECT
                toString(toStartOfFiveMinutes({TS_EXPR})) AS ts,
                argMin(price_scaled, exch_ts),
                max(price_scaled),
                min(price_scaled),
                argMax(price_scaled, exch_ts),
                sum(volume),
                count()
            FROM hft.market_data WHERE {where}
            GROUP BY ts ORDER BY ts
        """)
        return [
            Bar5m(
                ts=r[0],
                open=_ch_to_platform(r[1]),
                high=_ch_to_platform(r[2]),
                low=_ch_to_platform(r[3]),
                close=_ch_to_platform(r[4]),
                volume=r[5],
                ticks=r[6],
            )
            for r in rows
        ]

    def _query_flow_5m(self, base_where: str, sf: str) -> list[FlowBar]:
        rows = self._query(f"""
            SELECT window, ticks, total_vol, uptick_vol, downtick_vol, flat_vol,
                round(uptick_vol * 1.0 / greatest(downtick_vol, 1), 3),
                uptick_vol - downtick_vol
            FROM (
                SELECT
                    toString(toStartOfFiveMinutes(ts)) AS window,
                    count() AS ticks,
                    sum(v) AS total_vol,
                    sum(CASE WHEN p > prev_p THEN v ELSE 0 END) AS uptick_vol,
                    sum(CASE WHEN p < prev_p THEN v ELSE 0 END) AS downtick_vol,
                    sum(CASE WHEN p = prev_p THEN v ELSE 0 END) AS flat_vol
                FROM (
                    SELECT
                        {TS_EXPR} AS ts,
                        price_scaled AS p,
                        volume AS v,
                        lagInFrame(price_scaled) OVER (ORDER BY exch_ts) AS prev_p
                    FROM hft.market_data
                    WHERE {base_where}
                )
                WHERE prev_p > 0
                GROUP BY window
            )
            ORDER BY window
        """)
        return [
            FlowBar(
                ts=r[0],
                ticks=r[1],
                total_vol=r[2],
                uptick_vol=r[3],
                downtick_vol=r[4],
                flat_vol=r[5],
                ud_ratio=float(r[6]),
                net_flow=r[7],
            )
            for r in rows
        ]

    def _query_large_trades(self, where: str, symbol: str) -> list[LargeTrade]:
        threshold = LARGE_TRADE_THRESHOLD.get(symbol, 10)
        rows = self._query(f"""
            SELECT
                toString({TS_EXPR}),
                price_scaled,
                volume
            FROM hft.market_data
            WHERE {where} AND volume >= {threshold}
            ORDER BY exch_ts
        """)
        return [
            LargeTrade(
                ts=r[0],
                price=_ch_to_platform(r[1]),
                volume=r[2],
                direction="unknown",
            )
            for r in rows
        ]

    def _query_spread_dist(self, where: str) -> dict[int, int]:
        rows = self._query(f"""
            SELECT
                toInt32((asks_price[1] - bids_price[1]) / 10000) AS spread_pts,
                count() AS cnt
            FROM hft.market_data WHERE {where}
            GROUP BY spread_pts ORDER BY spread_pts
        """)
        return {int(r[0]): int(r[1]) for r in rows if r[0] is not None}

    def _query_depth_imbalance(self, where: str) -> list[DepthBar]:
        rows = self._query(f"""
            SELECT
                toHour({TS_EXPR}) AS hr,
                avg(bids_vol[1]),
                avg(asks_vol[1]),
                round(avg(bids_vol[1]) / (avg(bids_vol[1]) + avg(asks_vol[1])), 4)
            FROM hft.market_data WHERE {where}
                AND length(bids_vol) > 0 AND length(asks_vol) > 0
            GROUP BY hr ORDER BY hr
        """)
        return [
            DepthBar(
                hour=int(r[0]),
                avg_bid_vol=float(r[1]),
                avg_ask_vol=float(r[2]),
                bid_ratio=float(r[3]),
            )
            for r in rows
        ]
```

- [ ] **Step 4: Run tests**

Run: `uv run pytest tests/unit/test_report_collector.py -v`
Expected: all PASS

- [ ] **Step 5: Commit**

```bash
git add src/hft_platform/reports/collector.py tests/unit/test_report_collector.py
git commit -m "feat(reports): add DataCollector with CH queries and price scale conversion"
```

---

### Task 4: Informed Flow Rules (rules/informed_flow.py)

**Files:**
- Create: `src/hft_platform/reports/rules/__init__.py`
- Create: `src/hft_platform/reports/rules/informed_flow.py`
- Create: `tests/unit/test_report_rules_flow.py`

**Context:** 6 rules (IF-01 through IF-06) that score informed flow signals from -1.0 to +1.0. Each rule is a pure function taking `SessionData` or `list[FlowBar]`/`list[LargeTrade]` and returning a float score.

- [ ] **Step 1: Write tests for IF-01 (Session U/D Ratio)**

```python
# tests/unit/test_report_rules_flow.py
"""Tests for informed flow rules."""
from __future__ import annotations

import pytest

from hft_platform.reports.models import FlowBar, LargeTrade
from hft_platform.reports.rules.informed_flow import (
    score_end_of_session_drift,
    score_large_trade_net,
    score_session_ud,
    score_sustained_pressure,
    score_volume_spike,
)


def _fb(ud: float, net: int = 0, vol: int = 100, ticks: int = 50) -> FlowBar:
    up = int(vol * ud / (1 + ud)) if ud > 0 else 0
    dn = vol - up
    return FlowBar(ts="t", ticks=ticks, total_vol=vol, uptick_vol=up,
                    downtick_vol=dn, flat_vol=0, ud_ratio=ud, net_flow=net)


class TestScoreSessionUd:
    def test_bearish(self) -> None:
        bars = [_fb(0.8, vol=100), _fb(0.9, vol=200)]
        # total up = 44+94=138, total dn = 56+106=162 → ratio ≈ 0.852
        score = score_session_ud(bars)
        assert score < -0.3  # bearish

    def test_bullish(self) -> None:
        bars = [_fb(1.3, vol=100), _fb(1.2, vol=200)]
        score = score_session_ud(bars)
        assert score > 0.3  # bullish

    def test_neutral(self) -> None:
        bars = [_fb(1.0, vol=100)]
        score = score_session_ud(bars)
        assert -0.5 < score < 0.5

    def test_empty_bars(self) -> None:
        assert score_session_ud([]) == 0.0


class TestScoreSustainedPressure:
    def test_four_consecutive_bearish(self) -> None:
        bars = [_fb(0.6)] * 5
        score = score_sustained_pressure(bars)
        assert score < -0.5

    def test_no_sustained(self) -> None:
        bars = [_fb(0.8), _fb(1.2), _fb(0.65), _fb(1.0)]
        score = score_sustained_pressure(bars)
        assert abs(score) < 0.5

    def test_four_consecutive_bullish(self) -> None:
        bars = [_fb(1.4)] * 5
        score = score_sustained_pressure(bars)
        assert score > 0.5


class TestScoreLargeTradeNet:
    def test_net_sell(self) -> None:
        trades = [
            LargeTrade(ts="t", price=0, volume=30, direction="sell"),
            LargeTrade(ts="t", price=0, volume=10, direction="buy"),
        ]
        score = score_large_trade_net(trades)
        assert score < 0

    def test_net_buy(self) -> None:
        trades = [
            LargeTrade(ts="t", price=0, volume=50, direction="buy"),
            LargeTrade(ts="t", price=0, volume=20, direction="sell"),
        ]
        score = score_large_trade_net(trades)
        assert score > 0

    def test_empty(self) -> None:
        assert score_large_trade_net([]) == 0.0

    def test_unknown_direction_ignored(self) -> None:
        trades = [LargeTrade(ts="t", price=0, volume=100, direction="unknown")]
        assert score_large_trade_net(trades) == 0.0


class TestScoreEndOfSessionDrift:
    def test_eod_bearish_drift(self) -> None:
        session_bars = [_fb(1.1)] * 20 + [_fb(0.5)] * 6
        score = score_end_of_session_drift(session_bars)
        assert score < -0.3

    def test_no_drift(self) -> None:
        session_bars = [_fb(1.0)] * 20
        score = score_end_of_session_drift(session_bars)
        assert abs(score) < 0.3


class TestScoreVolumeSpike:
    def test_spike_with_bearish_direction(self) -> None:
        normal = [_fb(1.0, vol=100)] * 10
        spike = [_fb(0.5, vol=300, net=-100)]
        score, events = score_volume_spike(normal + spike)
        assert len(events) >= 1
        assert score < 0  # spike direction is bearish

    def test_no_spike(self) -> None:
        bars = [_fb(1.0, vol=100)] * 10
        score, events = score_volume_spike(bars)
        assert len(events) == 0
        assert score == 0.0
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/unit/test_report_rules_flow.py -v`
Expected: FAIL — `ModuleNotFoundError`

- [ ] **Step 3: Implement informed_flow.py**

```python
# src/hft_platform/reports/rules/__init__.py
"""Signal rules for the report pipeline."""

# src/hft_platform/reports/rules/informed_flow.py
"""Informed flow rules (IF-01 through IF-06).

Each rule is a pure function returning a score in [-1.0, +1.0].
Negative = bearish, positive = bullish.
"""
from __future__ import annotations

from hft_platform.reports.models import FlowBar, LargeTrade


def score_session_ud(bars: list[FlowBar]) -> float:
    """IF-01: Session U/D Ratio. < 0.9 → bearish, > 1.1 → bullish."""
    if not bars:
        return 0.0
    total_up = sum(b.uptick_vol for b in bars)
    total_dn = sum(b.downtick_vol for b in bars)
    if total_dn == 0:
        return 1.0 if total_up > 0 else 0.0
    ratio = total_up / total_dn
    # Linear map: 0.9 → -1.0, 1.0 → 0.0, 1.1 → +1.0
    return max(-1.0, min(1.0, (ratio - 1.0) * 10.0))


def score_sustained_pressure(bars: list[FlowBar]) -> float:
    """IF-02: Consecutive 5min bars with U/D < 0.7 or > 1.3. Count >= 4 → ±1.0."""
    if not bars:
        return 0.0
    max_bear_run = 0
    max_bull_run = 0
    bear_run = 0
    bull_run = 0
    for b in bars:
        if b.ud_ratio < 0.7:
            bear_run += 1
            max_bear_run = max(max_bear_run, bear_run)
        else:
            bear_run = 0
        if b.ud_ratio > 1.3:
            bull_run += 1
            max_bull_run = max(max_bull_run, bull_run)
        else:
            bull_run = 0
    if max_bear_run >= 4:
        return -min(1.0, max_bear_run / 4.0)
    if max_bull_run >= 4:
        return min(1.0, max_bull_run / 4.0)
    return 0.0


def score_large_trade_net(trades: list[LargeTrade]) -> float:
    """IF-03: Net large trade volume. Normalized to [-1, 1]."""
    if not trades:
        return 0.0
    buy_vol = sum(t.volume for t in trades if t.direction == "buy")
    sell_vol = sum(t.volume for t in trades if t.direction == "sell")
    total = buy_vol + sell_vol
    if total == 0:
        return 0.0
    net = buy_vol - sell_vol
    return max(-1.0, min(1.0, net / total))


def find_large_trade_clusters(
    trades: list[LargeTrade],
    price_tolerance: int = 30_000,  # ±3 pts in platform scale
    time_window_s: float = 60.0,
) -> list[tuple[int, int]]:
    """IF-04: Find price clusters where ≥3 large trades within ±3pts and 60s.

    Returns list of (price, total_volume) for institutional-level clusters.
    """
    if len(trades) < 3:
        return []
    clusters: list[tuple[int, int]] = []
    for i, anchor in enumerate(trades):
        cluster_vol = anchor.volume
        cluster_count = 1
        for j in range(i + 1, len(trades)):
            other = trades[j]
            if abs(other.price - anchor.price) <= price_tolerance:
                cluster_count += 1
                cluster_vol += other.volume
        if cluster_count >= 3:
            clusters.append((anchor.price, cluster_vol))
    return clusters


def score_end_of_session_drift(bars: list[FlowBar]) -> float:
    """IF-05: Last 30min (6 bars) U/D vs session U/D. Divergence > 0.2 → score."""
    if len(bars) < 8:
        return 0.0
    eod_bars = bars[-6:]
    session_up = sum(b.uptick_vol for b in bars)
    session_dn = sum(b.downtick_vol for b in bars)
    eod_up = sum(b.uptick_vol for b in eod_bars)
    eod_dn = sum(b.downtick_vol for b in eod_bars)
    if session_dn == 0 or eod_dn == 0:
        return 0.0
    session_ud = session_up / session_dn
    eod_ud = eod_up / eod_dn
    drift = eod_ud - session_ud
    if abs(drift) < 0.2:
        return 0.0
    return max(-1.0, min(1.0, drift * 3.0))


def score_volume_spike(bars: list[FlowBar]) -> tuple[float, list[FlowBar]]:
    """IF-06: 5min vol > 2× session mean → key event. Returns (score, spike_bars)."""
    if not bars:
        return 0.0, []
    mean_vol = sum(b.total_vol for b in bars) / len(bars)
    threshold = mean_vol * 2.0
    spikes = [b for b in bars if b.total_vol > threshold]
    if not spikes:
        return 0.0, []
    # Score = average direction of spike bars
    total_net = sum(b.net_flow for b in spikes)
    total_vol = sum(b.total_vol for b in spikes)
    if total_vol == 0:
        return 0.0, spikes
    score = max(-1.0, min(1.0, total_net / total_vol * 5.0))
    return score, spikes
```

- [ ] **Step 4: Run tests**

Run: `uv run pytest tests/unit/test_report_rules_flow.py -v`
Expected: all PASS

- [ ] **Step 5: Commit**

```bash
git add src/hft_platform/reports/rules/__init__.py src/hft_platform/reports/rules/informed_flow.py tests/unit/test_report_rules_flow.py
git commit -m "feat(reports): add informed flow rules IF-01 through IF-06"
```

---

### Task 5: Support/Resistance Rules (rules/support_resistance.py)

**Files:**
- Create: `src/hft_platform/reports/rules/support_resistance.py`
- Create: `tests/unit/test_report_rules_sr.py`

**Context:** 6 rules (SR-01 through SR-06) that identify support/resistance price levels from session data.

- [ ] **Step 1: Write tests**

```python
# tests/unit/test_report_rules_sr.py
"""Tests for support/resistance rules."""
from __future__ import annotations

import pytest

from hft_platform.reports.models import Bar5m, LargeTrade, PriceLevel, SessionData
from hft_platform.reports.rules.support_resistance import (
    find_double_bottoms_tops,
    find_failed_breakouts,
    find_large_trade_levels,
    find_round_numbers,
    find_session_extremes,
    find_volume_at_price,
)


def _make_sd(**kwargs: object) -> SessionData:
    defaults: dict[str, object] = dict(
        session="day", symbol="TXFD6", date="2026-03-27",
        open=330490000, high=330490000, low=323750000, close=324380000,
        volume=58107, tick_count=38153,
        bars_5m=[], flow_5m=[], large_trades=[], spread_dist={}, depth_imbalance=[],
    )
    defaults.update(kwargs)
    return SessionData(**defaults)  # type: ignore[arg-type]


class TestLargeTradelevels:
    def test_cluster_at_price(self) -> None:
        trades = [
            LargeTrade(ts="t", price=327500000, volume=32, direction="buy"),
            LargeTrade(ts="t", price=327500000, volume=13, direction="buy"),
            LargeTrade(ts="t", price=327300000, volume=27, direction="buy"),
        ]
        levels = find_large_trade_levels(trades, min_volume=20)
        assert any(lv.price == 327500000 for lv in levels)

    def test_sell_cluster_is_resistance(self) -> None:
        trades = [
            LargeTrade(ts="t", price=326100000, volume=38, direction="sell"),
        ]
        levels = find_large_trade_levels(trades, min_volume=20)
        assert any("壓力" in lv.reason or "sell" in lv.reason.lower() for lv in levels)


class TestDoubleBottomTop:
    def test_double_bottom(self) -> None:
        bars = [
            Bar5m(ts="t1", open=325000000, high=326000000, low=323750000, close=325000000, volume=100, ticks=50),
            Bar5m(ts="t2", open=325000000, high=327000000, low=325000000, close=326000000, volume=100, ticks=50),
            Bar5m(ts="t3", open=326000000, high=326000000, low=323780000, close=325000000, volume=100, ticks=50),
        ]
        levels = find_double_bottoms_tops(bars, tolerance=50000)  # ±5 pts
        assert len(levels) >= 1
        assert levels[0].price == 323750000  # first touch price

    def test_no_double_bottom(self) -> None:
        bars = [
            Bar5m(ts="t1", open=325000000, high=326000000, low=323750000, close=325000000, volume=100, ticks=50),
            Bar5m(ts="t2", open=325000000, high=326000000, low=320000000, close=325000000, volume=100, ticks=50),
        ]
        levels = find_double_bottoms_tops(bars, tolerance=50000)
        assert len(levels) == 0


class TestRoundNumbers:
    def test_round_numbers_in_range(self) -> None:
        levels = find_round_numbers(low=323000000, high=330500000)
        prices = {lv.price for lv in levels}
        assert 325000000 in prices  # 32,500
        assert 330000000 in prices  # 33,000

    def test_importance_scaling(self) -> None:
        levels = find_round_numbers(low=319000000, high=321000000)
        for lv in levels:
            if lv.price == 320000000:  # 32,000 = 千位整數
                assert lv.importance == 3


class TestSessionExtremes:
    def test_returns_high_and_low(self) -> None:
        sd = _make_sd(high=330490000, low=323750000)
        levels = find_session_extremes(sd)
        prices = {lv.price for lv in levels}
        assert 330490000 in prices
        assert 323750000 in prices


class TestVolumeAtPrice:
    def test_top_buckets(self) -> None:
        bars = [
            Bar5m(ts="t", open=325000000, high=325500000, low=324500000, close=325000000, volume=1000, ticks=500),
            Bar5m(ts="t", open=325000000, high=325500000, low=324500000, close=325000000, volume=900, ticks=450),
            Bar5m(ts="t", open=330000000, high=330500000, low=329500000, close=330000000, volume=100, ticks=50),
        ]
        levels = find_volume_at_price(bars, bucket_size=500000, top_n=2)
        assert len(levels) <= 2
        # Highest volume bucket should be around 325000000 area
        assert levels[0].price >= 324500000
        assert levels[0].price <= 325500000
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/unit/test_report_rules_sr.py -v`
Expected: FAIL

- [ ] **Step 3: Implement support_resistance.py**

```python
# src/hft_platform/reports/rules/support_resistance.py
"""Support/Resistance rules (SR-01 through SR-06).

Each rule returns a list of PriceLevel objects.
"""
from __future__ import annotations

from hft_platform.reports.models import Bar5m, LargeTrade, PriceLevel, SessionData

PLATFORM_SCALE = 10_000


def find_large_trade_levels(
    trades: list[LargeTrade],
    min_volume: int = 20,
) -> list[PriceLevel]:
    """SR-01: ≥min_volume lot trades mark S/R levels."""
    levels: list[PriceLevel] = []
    for t in trades:
        if t.volume >= min_volume:
            side = "支撐" if t.direction == "buy" else "壓力" if t.direction == "sell" else "關鍵"
            levels.append(PriceLevel(
                price=t.price,
                strength=min(1.0, t.volume / 50.0),
                reason=f"{side} {t.volume}口@{t.price // PLATFORM_SCALE:,}",
            ))
    return levels


def find_double_bottoms_tops(
    bars: list[Bar5m],
    tolerance: int = 50_000,  # ±5 pts in platform scale
) -> list[PriceLevel]:
    """SR-02: Two touches at same price (±tolerance) with reversal between."""
    if len(bars) < 3:
        return []
    levels: list[PriceLevel] = []
    lows = [(i, b.low) for i, b in enumerate(bars)]
    highs = [(i, b.high) for i, b in enumerate(bars)]

    # Double bottoms
    for i, (idx_a, low_a) in enumerate(lows):
        for idx_b, low_b in lows[i + 2:]:  # at least 2 bars apart
            if abs(low_a - low_b) <= tolerance:
                # Check reversal between: at least one bar with higher low
                between = [b.low for b in bars[idx_a + 1:idx_b]]
                if between and max(between) > low_a + tolerance:
                    levels.append(PriceLevel(
                        price=low_a,
                        strength=0.9,
                        reason=f"雙底 {low_a // PLATFORM_SCALE:,}",
                    ))
                    break  # one double bottom per anchor

    # Double tops
    for i, (idx_a, high_a) in enumerate(highs):
        for idx_b, high_b in highs[i + 2:]:
            if abs(high_a - high_b) <= tolerance:
                between = [b.high for b in bars[idx_a + 1:idx_b]]
                if between and min(between) < high_a - tolerance:
                    levels.append(PriceLevel(
                        price=high_a,
                        strength=0.9,
                        reason=f"雙頂 {high_a // PLATFORM_SCALE:,}",
                    ))
                    break
    return levels


def find_round_numbers(low: int, high: int) -> list[PriceLevel]:
    """SR-03: Round number levels within the session range."""
    levels: list[PriceLevel] = []
    # Check multiples of 1000 pts (10_000_000 in platform scale), 500, 100
    for step, importance in [(10_000_000, 3), (5_000_000, 2), (1_000_000, 1)]:
        start = (low // step) * step
        p = start
        while p <= high:
            if low <= p <= high:
                # Avoid duplicates: higher step already added this price
                if not any(lv.price == p for lv in levels):
                    levels.append(PriceLevel(
                        price=p,
                        strength=importance / 3.0,
                        reason=f"整數關卡 {p // PLATFORM_SCALE:,}",
                    ))
            p += step
    return levels


def find_session_extremes(sd: SessionData) -> list[PriceLevel]:
    """SR-04: Session high and low."""
    return [
        PriceLevel(price=sd.high, strength=0.5, reason=f"{sd.session}盤高點 {sd.high // PLATFORM_SCALE:,}"),
        PriceLevel(price=sd.low, strength=0.5, reason=f"{sd.session}盤低點 {sd.low // PLATFORM_SCALE:,}"),
    ]


def find_volume_at_price(
    bars: list[Bar5m],
    bucket_size: int = 500_000,  # 50 pts in platform scale
    top_n: int = 3,
) -> list[PriceLevel]:
    """SR-05: Volume-at-price profile, top N buckets."""
    if not bars:
        return []
    buckets: dict[int, int] = {}
    for b in bars:
        mid = (b.high + b.low) // 2
        bucket_key = (mid // bucket_size) * bucket_size
        buckets[bucket_key] = buckets.get(bucket_key, 0) + b.volume
    sorted_buckets = sorted(buckets.items(), key=lambda x: x[1], reverse=True)[:top_n]
    total_vol = sum(v for _, v in sorted_buckets) or 1
    return [
        PriceLevel(
            price=price,
            strength=min(1.0, vol / total_vol * 2.0),
            reason=f"成交密集區 {price // PLATFORM_SCALE:,} ({vol:,}口)",
        )
        for price, vol in sorted_buckets
    ]


def find_failed_breakouts(
    bars: list[Bar5m],
    large_trades: list[LargeTrade],
    min_reversal_pts: int = 500_000,  # 50 pts in platform scale
) -> list[PriceLevel]:
    """SR-06: Price breaks level then reverses with large trade confirmation."""
    levels: list[PriceLevel] = []
    for i in range(1, len(bars) - 1):
        prev_bar = bars[i - 1]
        bar = bars[i]
        next_bar = bars[i + 1]
        # Bearish failed breakout: new high then close below prev close
        if bar.high > prev_bar.high and next_bar.close < bar.open - min_reversal_pts:
            # Check if a large sell trade near the high
            for t in large_trades:
                if t.ts >= bar.ts and t.direction == "sell" and abs(t.price - bar.high) < 1_000_000:
                    levels.append(PriceLevel(
                        price=bar.high,
                        strength=min(1.0, t.volume / 30.0),
                        reason=f"假突破壓力 {bar.high // PLATFORM_SCALE:,} ({t.volume}口打回)",
                    ))
                    break
        # Bullish failed breakdown: new low then close above prev close
        if bar.low < prev_bar.low and next_bar.close > bar.open + min_reversal_pts:
            for t in large_trades:
                if t.ts >= bar.ts and t.direction == "buy" and abs(t.price - bar.low) < 1_000_000:
                    levels.append(PriceLevel(
                        price=bar.low,
                        strength=min(1.0, t.volume / 30.0),
                        reason=f"假跌破支撐 {bar.low // PLATFORM_SCALE:,} ({t.volume}口承接)",
                    ))
                    break
    return levels
```

- [ ] **Step 4: Run tests**

Run: `uv run pytest tests/unit/test_report_rules_sr.py -v`
Expected: all PASS

- [ ] **Step 5: Commit**

```bash
git add src/hft_platform/reports/rules/support_resistance.py tests/unit/test_report_rules_sr.py
git commit -m "feat(reports): add support/resistance rules SR-01 through SR-06"
```

---

### Task 6: SignalEngine (signals.py)

**Files:**
- Create: `src/hft_platform/reports/signals.py`
- Create: `tests/unit/test_report_signals.py`

**Context:** Orchestrates all rules, computes weighted score, assigns large trade directions, and produces `SignalReport`.

- [ ] **Step 1: Write tests**

```python
# tests/unit/test_report_signals.py
"""Tests for SignalEngine."""
from __future__ import annotations

import pytest

from hft_platform.reports.models import FlowBar, LargeTrade, SessionData
from hft_platform.reports.signals import SignalEngine


def _fb(ud: float, net: int = 0, vol: int = 100) -> FlowBar:
    up = int(vol * ud / (1 + ud)) if ud > 0 else 0
    dn = vol - up
    return FlowBar(ts="t", ticks=50, total_vol=vol, uptick_vol=up,
                    downtick_vol=dn, flat_vol=0, ud_ratio=ud, net_flow=net)


def _make_sd(flow: list[FlowBar] | None = None, trades: list[LargeTrade] | None = None) -> SessionData:
    return SessionData(
        session="day", symbol="TXFD6", date="2026-03-27",
        open=330490000, high=330490000, low=323750000, close=324380000,
        volume=58107, tick_count=38153,
        bars_5m=[], flow_5m=flow or [], large_trades=trades or [],
        spread_dist={}, depth_imbalance=[],
    )


class TestSignalEngine:
    def test_bearish_session(self) -> None:
        bars = [_fb(0.7, net=-50, vol=200)] * 12
        sd = _make_sd(flow=bars)
        engine = SignalEngine()
        report = engine.analyze(sd)
        assert report.bias == "bearish"
        assert report.bias_confidence > 0.3
        assert report.total_net_flow < 0

    def test_bullish_session(self) -> None:
        bars = [_fb(1.4, net=80, vol=200)] * 12
        sd = _make_sd(flow=bars)
        engine = SignalEngine()
        report = engine.analyze(sd)
        assert report.bias == "bullish"
        assert report.bias_confidence > 0.3

    def test_neutral_session(self) -> None:
        bars = [_fb(1.0, net=0, vol=100)] * 12
        sd = _make_sd(flow=bars)
        engine = SignalEngine()
        report = engine.analyze(sd)
        assert report.bias == "neutral"

    def test_large_trade_direction_assignment(self) -> None:
        """Trades near bar lows → sell, near highs → buy."""
        bars = [_fb(0.8, net=-20)]
        trades = [
            LargeTrade(ts="t", price=323750000, volume=28, direction="unknown"),
            LargeTrade(ts="t", price=330490000, volume=32, direction="unknown"),
        ]
        sd = _make_sd(flow=bars, trades=trades)
        engine = SignalEngine()
        report = engine.analyze(sd)
        # Direction should be assigned based on proximity to session extremes
        directions = {t.price: t.direction for t in report.key_large_trades}
        # 323750000 near session low → sell
        assert directions.get(323750000) == "sell"
        # 330490000 near session high → buy
        assert directions.get(330490000) == "buy"

    def test_rule_scores_populated(self) -> None:
        bars = [_fb(0.8)] * 5
        sd = _make_sd(flow=bars)
        engine = SignalEngine()
        report = engine.analyze(sd)
        assert "IF-01_session_ud" in report.rule_scores

    def test_empty_session(self) -> None:
        sd = _make_sd()
        engine = SignalEngine()
        report = engine.analyze(sd)
        assert report.bias == "neutral"
        assert report.total_net_flow == 0
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/unit/test_report_signals.py -v`
Expected: FAIL

- [ ] **Step 3: Implement signals.py**

```python
# src/hft_platform/reports/signals.py
"""Stage 2: SignalEngine — analyze SessionData and produce SignalReport."""
from __future__ import annotations

import structlog

from hft_platform.reports.models import (
    FlowBar,
    LargeTrade,
    PriceLevel,
    SessionData,
    SignalReport,
)
from hft_platform.reports.rules.informed_flow import (
    find_large_trade_clusters,
    score_end_of_session_drift,
    score_large_trade_net,
    score_session_ud,
    score_sustained_pressure,
    score_volume_spike,
)
from hft_platform.reports.rules.support_resistance import (
    find_double_bottoms_tops,
    find_failed_breakouts,
    find_large_trade_levels,
    find_round_numbers,
    find_session_extremes,
    find_volume_at_price,
)

logger = structlog.get_logger(__name__)

WEIGHTS: dict[str, float] = {
    "IF-01_session_ud": 0.25,
    "IF-02_sustained": 0.15,
    "IF-03_large_net": 0.20,
    "IF-04_cluster": 0.10,
    "IF-05_eod_drift": 0.10,
    "IF-06_vol_spike": 0.05,
    "SR-02_double_pattern": 0.10,
    "SR-06_failed_breakout": 0.05,
}


class SignalEngine:
    """Orchestrates all rules and produces a SignalReport."""

    def analyze(self, sd: SessionData) -> SignalReport:
        flow = sd.flow_5m
        trades = self._assign_directions(sd.large_trades, sd)

        # Compute rule scores
        scores: dict[str, float] = {}
        scores["IF-01_session_ud"] = score_session_ud(flow)
        scores["IF-02_sustained"] = score_sustained_pressure(flow)
        scores["IF-03_large_net"] = score_large_trade_net(trades)

        clusters = find_large_trade_clusters(trades)
        scores["IF-04_cluster"] = -0.5 if any(
            any(t.direction == "sell" for t in trades if abs(t.price - cp) < 30_000)
            for cp, _ in clusters
        ) else (0.5 if clusters else 0.0)

        scores["IF-05_eod_drift"] = score_end_of_session_drift(flow)

        vol_score, _ = score_volume_spike(flow)
        scores["IF-06_vol_spike"] = vol_score

        # Support/Resistance rules
        double_levels = find_double_bottoms_tops(sd.bars_5m)
        scores["SR-02_double_pattern"] = 0.3 if double_levels else 0.0

        failed = find_failed_breakouts(sd.bars_5m, trades)
        scores["SR-06_failed_breakout"] = -0.3 if any(
            "壓力" in lv.reason for lv in failed
        ) else (0.3 if failed else 0.0)

        # Weighted sum
        weighted = sum(scores.get(k, 0.0) * w for k, w in WEIGHTS.items())
        if weighted < -0.3:
            bias = "bearish"
        elif weighted > 0.3:
            bias = "bullish"
        else:
            bias = "neutral"
        confidence = min(1.0, abs(weighted))

        # Aggregate flow stats
        total_net = sum(b.net_flow for b in flow)
        total_up = sum(b.uptick_vol for b in flow)
        total_dn = sum(b.downtick_vol for b in flow)
        ud_session = total_up / total_dn if total_dn > 0 else 1.0

        strongest_sell = min(flow, key=lambda b: b.ud_ratio) if flow else FlowBar(ts="", ticks=0, total_vol=0, uptick_vol=0, downtick_vol=0, flat_vol=0, ud_ratio=1.0, net_flow=0)
        strongest_buy = max(flow, key=lambda b: b.ud_ratio) if flow else FlowBar(ts="", ticks=0, total_vol=0, uptick_vol=0, downtick_vol=0, flat_vol=0, ud_ratio=1.0, net_flow=0)

        buy_vol = sum(t.volume for t in trades if t.direction == "buy")
        sell_vol = sum(t.volume for t in trades if t.direction == "sell")

        # Collect all S/R levels
        all_levels: list[PriceLevel] = []
        all_levels.extend(find_large_trade_levels(trades))
        all_levels.extend(double_levels)
        all_levels.extend(find_round_numbers(sd.low, sd.high))
        all_levels.extend(find_session_extremes(sd))
        all_levels.extend(find_volume_at_price(sd.bars_5m))
        all_levels.extend(failed)

        # Split into supports (below close) and resistances (above close)
        supports = sorted(
            [lv for lv in all_levels if lv.price <= sd.close],
            key=lambda lv: lv.strength,
            reverse=True,
        )[:3]
        resistances = sorted(
            [lv for lv in all_levels if lv.price > sd.close],
            key=lambda lv: lv.strength,
            reverse=True,
        )[:3]

        return SignalReport(
            session_data=sd,
            total_net_flow=total_net,
            ud_ratio_session=round(ud_session, 3),
            strongest_sell=strongest_sell,
            strongest_buy=strongest_buy,
            large_buy_volume=buy_vol,
            large_sell_volume=sell_vol,
            large_net=buy_vol - sell_vol,
            key_large_trades=trades,
            supports=supports,
            resistances=resistances,
            bias=bias,
            bias_confidence=round(confidence, 3),
            rule_scores=scores,
        )

    def _assign_directions(
        self, trades: list[LargeTrade], sd: SessionData
    ) -> list[LargeTrade]:
        """Assign buy/sell direction based on proximity to session extremes."""
        if not trades or sd.high == sd.low:
            return trades
        mid = (sd.high + sd.low) // 2
        result: list[LargeTrade] = []
        for t in trades:
            if t.direction != "unknown":
                result.append(t)
                continue
            if t.price <= mid:
                direction = "sell"
            else:
                direction = "buy"
            result.append(LargeTrade(
                ts=t.ts, price=t.price, volume=t.volume, direction=direction,
            ))
        return result
```

- [ ] **Step 4: Run tests**

Run: `uv run pytest tests/unit/test_report_signals.py -v`
Expected: all PASS

- [ ] **Step 5: Commit**

```bash
git add src/hft_platform/reports/signals.py tests/unit/test_report_signals.py
git commit -m "feat(reports): add SignalEngine with weighted rule scoring"
```

---

### Task 7: ScenarioBuilder (scenarios.py)

**Files:**
- Create: `src/hft_platform/reports/scenarios.py`
- Create: `src/hft_platform/reports/rules/scenario_rules.py`
- Create: `tests/unit/test_report_scenarios.py`

**Context:** Takes `SignalReport`, generates scenarios (SC-01 through SC-04), computes entry/target/stop, and returns `ScenarioReport`.

- [ ] **Step 1: Write tests**

```python
# tests/unit/test_report_scenarios.py
"""Tests for ScenarioBuilder."""
from __future__ import annotations

import pytest

from hft_platform.reports.models import (
    FlowBar,
    PriceLevel,
    SessionData,
    SignalReport,
)
from hft_platform.reports.scenarios import ScenarioBuilder


def _fb() -> FlowBar:
    return FlowBar(ts="t", ticks=1, total_vol=1, uptick_vol=0, downtick_vol=1, flat_vol=0, ud_ratio=0.5, net_flow=-1)


def _make_signal(bias: str, supports: list[PriceLevel] | None = None, resistances: list[PriceLevel] | None = None) -> SignalReport:
    sd = SessionData(
        session="night", symbol="TXFD6", date="2026-03-27",
        open=330490000, high=330490000, low=323750000, close=324380000,
        volume=58107, tick_count=38153,
        bars_5m=[], flow_5m=[_fb()] * 10, large_trades=[], spread_dist={}, depth_imbalance=[],
    )
    return SignalReport(
        session_data=sd,
        total_net_flow=-1581,
        ud_ratio_session=0.906,
        strongest_sell=_fb(),
        strongest_buy=_fb(),
        large_buy_volume=380,
        large_sell_volume=650,
        large_net=-270,
        key_large_trades=[],
        supports=supports or [
            PriceLevel(price=323750000, strength=0.9, reason="雙底"),
            PriceLevel(price=320000000, strength=0.6, reason="整千"),
        ],
        resistances=resistances or [
            PriceLevel(price=327500000, strength=0.9, reason="壓力"),
            PriceLevel(price=330000000, strength=0.7, reason="整千"),
        ],
        bias=bias,
        bias_confidence=0.75,
        rule_scores={},
    )


class TestScenarioBuilder:
    def test_bearish_generates_scenarios(self) -> None:
        signal = _make_signal("bearish")
        builder = ScenarioBuilder()
        report = builder.build(signal)
        assert report.direction == "偏空"
        assert report.confidence_pct >= 60
        assert len(report.scenarios) >= 2
        assert len(report.key_levels) >= 2

    def test_bullish_direction(self) -> None:
        signal = _make_signal("bullish")
        builder = ScenarioBuilder()
        report = builder.build(signal)
        assert report.direction == "偏多"

    def test_neutral_direction(self) -> None:
        signal = _make_signal("neutral")
        builder = ScenarioBuilder()
        report = builder.build(signal)
        assert report.direction == "中性"

    def test_entry_zone_below_resistance_for_bearish(self) -> None:
        signal = _make_signal("bearish")
        builder = ScenarioBuilder()
        report = builder.build(signal)
        # Entry zone should be near resistance for shorting
        assert report.entry_zone[0] > report.target

    def test_stop_loss_above_entry_for_bearish(self) -> None:
        signal = _make_signal("bearish")
        builder = ScenarioBuilder()
        report = builder.build(signal)
        assert report.stop_loss > report.entry_zone[1]

    def test_scenario_ids_unique(self) -> None:
        signal = _make_signal("bearish")
        builder = ScenarioBuilder()
        report = builder.build(signal)
        ids = [s.id for s in report.scenarios]
        assert len(ids) == len(set(ids))

    def test_empty_supports(self) -> None:
        signal = _make_signal("bearish", supports=[], resistances=[])
        builder = ScenarioBuilder()
        report = builder.build(signal)
        assert report.direction == "偏空"
        # Should still produce a report, just with defaults
```

- [ ] **Step 2: Run tests, verify fail**

Run: `uv run pytest tests/unit/test_report_scenarios.py -v`
Expected: FAIL

- [ ] **Step 3: Implement scenario_rules.py and scenarios.py**

```python
# src/hft_platform/reports/rules/scenario_rules.py
"""Scenario generation rules (SC-01 through SC-04)."""
from __future__ import annotations

from hft_platform.reports.models import PriceLevel, Scenario, SignalReport

PLATFORM_SCALE = 10_000


def scenario_break_below_support(signal: SignalReport) -> Scenario | None:
    """SC-01: If price breaks strongest support → target next support."""
    supports = signal.supports
    if len(supports) < 2:
        return None
    s1 = supports[0]
    s2 = supports[1]
    return Scenario(
        id="break_below_support",
        label="破底加速",
        probability="較高" if signal.bias == "bearish" else "較低",
        condition=f"若破 {s1.price // PLATFORM_SCALE:,}",
        target=s2.price,
        description=f"目標看 {s2.price // PLATFORM_SCALE:,}，特徵: 量增價跌 + 大單持續空方",
    )


def scenario_hold_and_bounce(signal: SignalReport) -> Scenario | None:
    """SC-02: If support holds + U/D flips bullish → target resistance."""
    supports = signal.supports
    resistances = signal.resistances
    if not supports or not resistances:
        return None
    s1 = supports[0]
    r1 = resistances[0]
    return Scenario(
        id="hold_and_bounce",
        label="守底反彈",
        probability="較低" if signal.bias == "bearish" else "較高",
        condition=f"若守住 {s1.price // PLATFORM_SCALE:,} 且站回 {r1.price // PLATFORM_SCALE:,}",
        target=r1.price,
        description=f"空方失敗，目標看 {r1.price // PLATFORM_SCALE:,}",
    )


def scenario_range_bound(signal: SignalReport) -> Scenario | None:
    """SC-03 (consolidation): Range between S1 and R1."""
    supports = signal.supports
    resistances = signal.resistances
    if not supports or not resistances:
        return None
    s1 = supports[0]
    r1 = resistances[0]
    return Scenario(
        id="range_bound",
        label="區間震盪",
        probability="較低",
        condition=f"若在 {s1.price // PLATFORM_SCALE:,}-{r1.price // PLATFORM_SCALE:,} 之間反覆",
        target=0,
        description="等方向確認再操作，觀察大單方向",
    )
```

```python
# src/hft_platform/reports/scenarios.py
"""Stage 3: ScenarioBuilder — generate scenarios and key levels from SignalReport."""
from __future__ import annotations

import structlog

from hft_platform.reports.models import (
    KeyLevel,
    PriceLevel,
    ScenarioReport,
    SignalReport,
)
from hft_platform.reports.rules.scenario_rules import (
    scenario_break_below_support,
    scenario_hold_and_bounce,
    scenario_range_bound,
)

logger = structlog.get_logger(__name__)

PLATFORM_SCALE = 10_000


class ScenarioBuilder:
    """Produces ScenarioReport from SignalReport."""

    def build(self, signal: SignalReport) -> ScenarioReport:
        sd = signal.session_data

        # Direction
        direction_map = {"bearish": "偏空", "bullish": "偏多", "neutral": "中性"}
        direction = direction_map.get(signal.bias, "中性")
        confidence_pct = int(50 + signal.bias_confidence * 30)

        # Key levels
        key_levels = self._build_key_levels(signal.supports, signal.resistances)

        # Scenarios
        scenarios = []
        for gen in [scenario_break_below_support, scenario_hold_and_bounce, scenario_range_bound]:
            s = gen(signal)
            if s is not None:
                scenarios.append(s)

        # Entry/target/stop
        entry_zone, target, stop_loss = self._compute_trade_levels(signal)

        return ScenarioReport(
            signal=signal,
            direction=direction,
            confidence_pct=confidence_pct,
            entry_zone=entry_zone,
            target=target,
            stop_loss=stop_loss,
            scenarios=scenarios,
            key_levels=key_levels,
        )

    def _build_key_levels(
        self, supports: list[PriceLevel], resistances: list[PriceLevel]
    ) -> list[KeyLevel]:
        levels: list[KeyLevel] = []
        for i, s in enumerate(supports[:3], 1):
            levels.append(KeyLevel(
                price=s.price,
                label=f"S{i}",
                importance=max(1, min(3, int(s.strength * 3) + 1)),
                reason=s.reason,
            ))
        for i, r in enumerate(resistances[:3], 1):
            levels.append(KeyLevel(
                price=r.price,
                label=f"R{i}",
                importance=max(1, min(3, int(r.strength * 3) + 1)),
                reason=r.reason,
            ))
        return levels

    def _compute_trade_levels(
        self, signal: SignalReport
    ) -> tuple[tuple[int, int], int, int]:
        supports = signal.supports
        resistances = signal.resistances
        sd = signal.session_data
        atr = sd.high - sd.low  # session range as ATR proxy

        if signal.bias == "bearish" and resistances:
            r1 = resistances[0].price
            entry_low = r1 - atr // 10
            entry_high = r1
            target = supports[0].price if supports else sd.low
            stop_loss = r1 + atr // 5
        elif signal.bias == "bullish" and supports:
            s1 = supports[0].price
            entry_low = s1
            entry_high = s1 + atr // 10
            target = resistances[0].price if resistances else sd.high
            stop_loss = s1 - atr // 5
        else:
            mid = (sd.high + sd.low) // 2
            entry_low = mid - atr // 10
            entry_high = mid + atr // 10
            target = sd.close
            stop_loss = sd.close
        return (entry_low, entry_high), target, stop_loss
```

- [ ] **Step 4: Run tests**

Run: `uv run pytest tests/unit/test_report_scenarios.py -v`
Expected: all PASS

- [ ] **Step 5: Commit**

```bash
git add src/hft_platform/reports/rules/scenario_rules.py src/hft_platform/reports/scenarios.py tests/unit/test_report_scenarios.py
git commit -m "feat(reports): add ScenarioBuilder with SC-01 through SC-04"
```

---

### Task 8: ReportRenderer (renderer.py)

**Files:**
- Create: `src/hft_platform/reports/renderer.py`
- Create: `tests/unit/test_report_renderer.py`

**Context:** Renders `ScenarioReport` into Telegram HTML messages. Two tiers: free (2-3 msgs) and paid (5 msgs). Each message must be ≤ 4096 chars.

- [ ] **Step 1: Write tests**

```python
# tests/unit/test_report_renderer.py
"""Tests for ReportRenderer."""
from __future__ import annotations

import pytest

from hft_platform.reports.models import (
    FlowBar,
    KeyLevel,
    PriceLevel,
    Scenario,
    ScenarioReport,
    SessionData,
    SignalReport,
)
from hft_platform.reports.renderer import ReportRenderer

TELEGRAM_MAX_LEN = 4096


def _make_report() -> ScenarioReport:
    fb = FlowBar(ts="2026-03-27 21:50:00", ticks=737, total_vol=1122,
                  uptick_vol=288, downtick_vol=540, flat_vol=294, ud_ratio=0.533, net_flow=-252)
    fb_buy = FlowBar(ts="2026-03-27 23:00:00", ticks=463, total_vol=763,
                      uptick_vol=373, downtick_vol=206, flat_vol=184, ud_ratio=1.811, net_flow=167)
    sd = SessionData(
        session="night", symbol="TXFD6", date="2026-03-27",
        open=330490000, high=330490000, low=323750000, close=324380000,
        volume=58107, tick_count=38153,
        bars_5m=[], flow_5m=[fb] * 20, large_trades=[], spread_dist={3: 147202, 4: 81011},
        depth_imbalance=[],
    )
    signal = SignalReport(
        session_data=sd, total_net_flow=-1581, ud_ratio_session=0.906,
        strongest_sell=fb, strongest_buy=fb_buy,
        large_buy_volume=380, large_sell_volume=650, large_net=-270,
        key_large_trades=[], supports=[
            PriceLevel(price=323750000, strength=0.9, reason="雙底"),
            PriceLevel(price=320000000, strength=0.6, reason="整千關卡"),
        ], resistances=[
            PriceLevel(price=327500000, strength=0.9, reason="反彈天花板"),
            PriceLevel(price=330000000, strength=0.7, reason="被砸穿"),
        ], bias="bearish", bias_confidence=0.75, rule_scores={},
    )
    return ScenarioReport(
        signal=signal, direction="偏空", confidence_pct=75,
        entry_zone=(327000000, 327500000), target=323750000, stop_loss=328500000,
        scenarios=[
            Scenario(id="break", label="破底加速", probability="較高",
                     condition="若破 32,375", target=320000000, description="目標看 32,000"),
            Scenario(id="bounce", label="守底反彈", probability="較低",
                     condition="若守住 32,375", target=327500000, description="目標看 32,750"),
        ],
        key_levels=[
            KeyLevel(price=323750000, label="S1", importance=3, reason="雙底"),
            KeyLevel(price=327500000, label="R1", importance=3, reason="反彈天花板"),
        ],
    )


class TestReportRenderer:
    def test_paid_returns_5_messages(self) -> None:
        r = ReportRenderer()
        msgs = r.render(_make_report(), tier="paid")
        assert len(msgs) == 5

    def test_free_returns_3_messages(self) -> None:
        r = ReportRenderer()
        msgs = r.render(_make_report(), tier="free")
        assert len(msgs) == 3

    def test_all_messages_under_limit(self) -> None:
        r = ReportRenderer()
        for tier in ("free", "paid"):
            msgs = r.render(_make_report(), tier=tier)
            for i, m in enumerate(msgs):
                assert len(m) <= TELEGRAM_MAX_LEN, f"{tier} msg {i} too long: {len(m)}"

    def test_paid_contains_price_levels(self) -> None:
        r = ReportRenderer()
        msgs = r.render(_make_report(), tier="paid")
        combined = " ".join(msgs)
        assert "32,375" in combined
        assert "S1" in combined

    def test_paid_contains_scenarios(self) -> None:
        r = ReportRenderer()
        msgs = r.render(_make_report(), tier="paid")
        combined = " ".join(msgs)
        assert "破底加速" in combined
        assert "守底反彈" in combined

    def test_free_does_not_contain_levels(self) -> None:
        r = ReportRenderer()
        msgs = r.render(_make_report(), tier="free")
        combined = " ".join(msgs)
        assert "S1" not in combined

    def test_disclaimer_always_present(self) -> None:
        r = ReportRenderer()
        for tier in ("free", "paid"):
            msgs = r.render(_make_report(), tier=tier)
            assert "投資有風險" in msgs[-1]

    def test_summary_contains_ohlc(self) -> None:
        r = ReportRenderer()
        msgs = r.render(_make_report(), tier="paid")
        assert "33,049" in msgs[0]  # open price
        assert "32,375" in msgs[0]  # low price
```

- [ ] **Step 2: Run tests, verify fail**

Run: `uv run pytest tests/unit/test_report_renderer.py -v`
Expected: FAIL

- [ ] **Step 3: Implement renderer.py**

```python
# src/hft_platform/reports/renderer.py
"""Stage 4: ReportRenderer — render ScenarioReport to Telegram HTML messages."""
from __future__ import annotations

from hft_platform.reports.models import ScenarioReport

PLATFORM_SCALE = 10_000
SESSION_LABELS = {"day": "日盤", "night": "夜盤"}


def _p(scaled: int) -> str:
    """Format scaled price to human readable with comma."""
    return f"{scaled // PLATFORM_SCALE:,}"


def _pct(open_p: int, close_p: int) -> str:
    """Format price change percentage."""
    if open_p == 0:
        return "0.00%"
    change = close_p - open_p
    pct = change / open_p * 100
    arrow = "▲" if change > 0 else "▼" if change < 0 else "─"
    return f"{arrow}{abs(change) // PLATFORM_SCALE:,} ({pct:+.2f}%)"


def _stars(n: int) -> str:
    return "★" * n + "☆" * (3 - n)


def _ud_bar(ratio: float) -> str:
    """Visual bar for U/D ratio."""
    if ratio < 0.7:
        return "█░░░░"
    if ratio < 0.85:
        return "██░░░"
    if ratio < 0.95:
        return "███░░"
    if ratio < 1.05:
        return "▓▓▓░░"
    if ratio < 1.2:
        return "░░▓▓▓"
    return "░░░░█"


class ReportRenderer:
    """Renders ScenarioReport into Telegram messages."""

    def render(self, report: ScenarioReport, tier: str) -> list[str]:
        if tier == "free":
            return [
                self._render_summary(report),
                self._render_flow_brief(report),
                self._render_disclaimer(),
            ]
        return [
            self._render_summary(report),
            self._render_flow_detail(report),
            self._render_levels(report),
            self._render_scenarios(report),
            self._render_disclaimer(),
        ]

    def _render_summary(self, r: ScenarioReport) -> str:
        sd = r.signal.session_data
        label = SESSION_LABELS.get(sd.session, sd.session)
        spread_str = ""
        if sd.spread_dist:
            total = sum(sd.spread_dist.values())
            cumulative = 0
            median_pts = 0
            for pts in sorted(sd.spread_dist.keys()):
                cumulative += sd.spread_dist[pts]
                if cumulative >= total / 2:
                    median_pts = pts
                    break
            spread_str = f"\nSpread 中位數: {median_pts}pts"

        return (
            f"📊 台指期{label}報告 {sd.date}\n\n"
            f"{sd.symbol}  {_p(sd.open)} → {_p(sd.close)}  {_pct(sd.open, sd.close)}\n"
            f"High {_p(sd.high)} | Low {_p(sd.low)} | Vol {sd.volume:,}\n"
            f"Ticks {sd.tick_count:,}{spread_str}"
        )

    def _render_flow_brief(self, r: ScenarioReport) -> str:
        sig = r.signal
        return (
            f"🔍 知情流摘要\n\n"
            f"方向: {r.direction} {r.confidence_pct}%\n"
            f"全場淨流: {sig.total_net_flow:+,} 口\n"
            f"大單淨方向: {'空方' if sig.large_net < 0 else '多方'} {abs(sig.large_net):+,} 口\n\n"
            f"#台指期 #盤後分析"
        )

    def _render_flow_detail(self, r: ScenarioReport) -> str:
        sig = r.signal
        lines = [
            "🔍 知情流分析\n",
            f"▎全場 U/D = {sig.ud_ratio_session:.3f}  淨流 {sig.total_net_flow:+,} 口",
            f"▎最強空方: {sig.strongest_sell.ts[-8:]} U/D={sig.strongest_sell.ud_ratio:.3f} net={sig.strongest_sell.net_flow:+,}",
            f"▎最強多方: {sig.strongest_buy.ts[-8:]} U/D={sig.strongest_buy.ud_ratio:.3f} net={sig.strongest_buy.net_flow:+,}",
            "",
            f"▎大單:",
            f"  🔴 賣方 ~{sig.large_sell_volume:,} 口  🟢 買方 ~{sig.large_buy_volume:,} 口",
        ]

        # Top 5 key large trades by volume
        top_trades = sorted(sig.key_large_trades, key=lambda t: t.volume, reverse=True)[:5]
        for t in top_trades:
            icon = "🔴" if t.direction == "sell" else "🟢" if t.direction == "buy" else "⚪"
            lines.append(f"  {icon} {t.volume}口@{_p(t.price)}")

        # 2-hour U/D summary
        flow = sig.session_data.flow_5m
        if flow:
            lines.append("")
            lines.append("▎時段 U/D:")
            # Group by 2-hour windows
            chunk_size = 24  # 24 × 5min = 2 hours
            for start in range(0, len(flow), chunk_size):
                chunk = flow[start:start + chunk_size]
                if not chunk:
                    break
                up = sum(b.uptick_vol for b in chunk)
                dn = sum(b.downtick_vol for b in chunk)
                ratio = up / dn if dn > 0 else 1.0
                ts_label = chunk[0].ts[-8:-3] if chunk[0].ts else "?"
                ts_end = chunk[-1].ts[-8:-3] if chunk[-1].ts else "?"
                lines.append(f"  {ts_label}-{ts_end} {_ud_bar(ratio)} {ratio:.2f}")

        return "\n".join(lines)

    def _render_levels(self, r: ScenarioReport) -> str:
        lines = ["🎯 關鍵點位\n", "▎支撐:"]
        for kl in r.key_levels:
            if kl.label.startswith("S"):
                lines.append(f"  {kl.label}  {_p(kl.price)}  {_stars(kl.importance)} {kl.reason}")
        lines.append("\n▎壓力:")
        for kl in r.key_levels:
            if kl.label.startswith("R"):
                lines.append(f"  {kl.label}  {_p(kl.price)}  {_stars(kl.importance)} {kl.reason}")

        bias_label = "空方" if r.direction == "偏空" else "多方" if r.direction == "偏多" else "觀望"
        lines.append(f"\n▎進場參考 ({bias_label}):")
        lines.append(f"  進場區  {_p(r.entry_zone[0])}-{_p(r.entry_zone[1])}")
        lines.append(f"  目標    {_p(r.target)}")
        lines.append(f"  止損    {_p(r.stop_loss)}")

        return "\n".join(lines)

    def _render_scenarios(self, r: ScenarioReport) -> str:
        lines = ["📋 情境規劃\n"]
        for i, s in enumerate(r.scenarios):
            letter = chr(65 + i)  # A, B, C...
            lines.append(f"【情境 {letter}】{s.label} — 機率{s.probability}")
            lines.append(f"  {s.condition}")
            if s.target:
                lines.append(f"  → 目標 {_p(s.target)}")
            lines.append(f"  {s.description}")
            lines.append("")
        return "\n".join(lines).rstrip()

    def _render_disclaimer(self) -> str:
        return (
            "⚠️ 本報告基於歷史行情數據自動生成，\n"
            "僅供參考，不構成投資建議。\n"
            "投資有風險，請自行評估。"
        )
```

- [ ] **Step 4: Run tests**

Run: `uv run pytest tests/unit/test_report_renderer.py -v`
Expected: all PASS

- [ ] **Step 5: Commit**

```bash
git add src/hft_platform/reports/renderer.py tests/unit/test_report_renderer.py
git commit -m "feat(reports): add ReportRenderer with free/paid tier templates"
```

---

### Task 9: ReportSender + Distributor (distributor.py)

**Files:**
- Create: `src/hft_platform/reports/distributor.py`
- Create: `tests/unit/test_report_distributor.py`

**Context:** `ReportSender` wraps aiohttp for multi-channel Telegram delivery with retry on 429/5xx. `Distributor` routes rendered messages to channels by tier.

- [ ] **Step 1: Write tests**

```python
# tests/unit/test_report_distributor.py
"""Tests for Distributor and ReportSender."""
from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from hft_platform.reports.distributor import Distributor, ReportSender, load_channels
from hft_platform.reports.models import ChannelConfig


class TestLoadChannels:
    def test_owner_channel_from_env(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("HFT_TELEGRAM_CHAT_ID", "123")
        channels = load_channels()
        assert len(channels) == 1
        assert channels[0].name == "owner"
        assert channels[0].tier == "paid"
        assert channels[0].enabled is True

    def test_all_channels(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("HFT_TELEGRAM_CHAT_ID", "123")
        monkeypatch.setenv("HFT_REPORT_PAID_CHANNEL_ID", "456")
        monkeypatch.setenv("HFT_REPORT_PAID_ENABLED", "1")
        monkeypatch.setenv("HFT_REPORT_FREE_CHANNEL_ID", "789")
        monkeypatch.setenv("HFT_REPORT_FREE_ENABLED", "1")
        channels = load_channels()
        assert len(channels) == 3
        tiers = {ch.name: ch.tier for ch in channels}
        assert tiers == {"owner": "paid", "paid": "paid", "free": "free"}

    def test_disabled_channel_not_enabled(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("HFT_TELEGRAM_CHAT_ID", "123")
        monkeypatch.setenv("HFT_REPORT_FREE_CHANNEL_ID", "789")
        # HFT_REPORT_FREE_ENABLED not set → defaults to "0"
        channels = load_channels()
        free = [ch for ch in channels if ch.name == "free"]
        assert len(free) == 1
        assert free[0].enabled is False

    def test_no_env_returns_empty(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("HFT_TELEGRAM_CHAT_ID", raising=False)
        channels = load_channels()
        assert channels == []


class TestReportSender:
    @pytest.mark.asyncio
    async def test_send_success(self) -> None:
        mock_resp = AsyncMock()
        mock_resp.status = 200
        mock_resp.json = AsyncMock(return_value={"ok": True})
        mock_resp.__aenter__ = AsyncMock(return_value=mock_resp)
        mock_resp.__aexit__ = AsyncMock(return_value=False)

        mock_session = AsyncMock()
        mock_session.post = MagicMock(return_value=mock_resp)

        sender = ReportSender(bot_token="test_token")
        sender._session = mock_session

        result = await sender.send("123", "Hello")
        assert result is True

    @pytest.mark.asyncio
    async def test_send_no_token(self) -> None:
        sender = ReportSender(bot_token="")
        result = await sender.send("123", "Hello")
        assert result is False


class TestDistributor:
    @pytest.mark.asyncio
    async def test_routes_by_tier(self) -> None:
        mock_sender = AsyncMock(spec=ReportSender)
        mock_sender.send_batch = AsyncMock(return_value=2)

        channels = [
            ChannelConfig(name="owner", chat_id="111", tier="paid", enabled=True),
            ChannelConfig(name="free", chat_id="222", tier="free", enabled=True),
        ]
        rendered = {
            "free": ["msg1_free", "msg2_free"],
            "paid": ["msg1_paid", "msg2_paid", "msg3_paid"],
        }

        dist = Distributor(sender=mock_sender, channels=channels)
        await dist.send(rendered)

        calls = mock_sender.send_batch.call_args_list
        assert len(calls) == 2
        # owner gets paid tier
        assert calls[0].args == ("111", ["msg1_paid", "msg2_paid", "msg3_paid"])
        # free gets free tier
        assert calls[1].args == ("222", ["msg1_free", "msg2_free"])

    @pytest.mark.asyncio
    async def test_skips_disabled_channels(self) -> None:
        mock_sender = AsyncMock(spec=ReportSender)
        mock_sender.send_batch = AsyncMock(return_value=0)

        channels = [
            ChannelConfig(name="paid", chat_id="456", tier="paid", enabled=False),
        ]
        dist = Distributor(sender=mock_sender, channels=channels)
        await dist.send({"paid": ["msg"], "free": ["msg"]})

        mock_sender.send_batch.assert_not_called()
```

- [ ] **Step 2: Run tests, verify fail**

Run: `uv run pytest tests/unit/test_report_distributor.py -v`
Expected: FAIL

- [ ] **Step 3: Implement distributor.py**

```python
# src/hft_platform/reports/distributor.py
"""Stage 5: Distributor — multi-channel Telegram delivery with ReportSender."""
from __future__ import annotations

import asyncio
import os
from typing import Any

import structlog

try:
    import aiohttp
except ImportError:  # pragma: no cover
    aiohttp = None  # type: ignore[assignment]

from hft_platform.reports.models import ChannelConfig

logger = structlog.get_logger(__name__)

_TELEGRAM_API = "https://api.telegram.org/bot{token}/sendMessage"


def load_channels() -> list[ChannelConfig]:
    """Build channel list from environment variables."""
    channels: list[ChannelConfig] = []
    owner_id = os.environ.get("HFT_TELEGRAM_CHAT_ID", "")
    if owner_id:
        channels.append(ChannelConfig("owner", owner_id, "paid", enabled=True))
    paid_id = os.environ.get("HFT_REPORT_PAID_CHANNEL_ID", "")
    if paid_id:
        channels.append(ChannelConfig(
            "paid", paid_id, "paid",
            enabled=os.environ.get("HFT_REPORT_PAID_ENABLED", "0") == "1",
        ))
    free_id = os.environ.get("HFT_REPORT_FREE_CHANNEL_ID", "")
    if free_id:
        channels.append(ChannelConfig(
            "free", free_id, "free",
            enabled=os.environ.get("HFT_REPORT_FREE_ENABLED", "0") == "1",
        ))
    return channels


class ReportSender:
    """Dedicated Telegram sender for report delivery.

    NOT the same as notifications.telegram.TelegramSender:
    - Always enabled (if token set)
    - Per-call chat_id (multi-channel)
    - Explicit delay between messages (not rate-limit-drop)
    - Retries on 429/5xx
    """

    __slots__ = ("_token", "_session")

    def __init__(self, bot_token: str = "") -> None:
        self._token = bot_token or os.environ.get("HFT_TELEGRAM_BOT_TOKEN", "")
        self._session: Any = None

    async def _ensure_session(self) -> Any:
        if self._session is None and aiohttp is not None:
            self._session = aiohttp.ClientSession()
        return self._session

    async def send(self, chat_id: str, text: str, parse_mode: str = "HTML") -> bool:
        """Send one message. Retries on 429/5xx up to 3 times."""
        if not self._token:
            logger.warning("report_sender.no_token")
            return False

        session = await self._ensure_session()
        if session is None:
            logger.warning("report_sender.no_aiohttp")
            return False

        url = _TELEGRAM_API.format(token=self._token)
        payload = {"chat_id": chat_id, "text": text, "parse_mode": parse_mode}

        for attempt in range(3):
            try:
                async with session.post(url, json=payload) as resp:
                    if resp.status == 200:
                        return True
                    if resp.status == 429:
                        retry_after = int(resp.headers.get("Retry-After", "5"))
                        logger.warning("report_sender.rate_limited", retry_after=retry_after, attempt=attempt)
                        await asyncio.sleep(retry_after)
                        continue
                    if resp.status >= 500:
                        wait = 2 ** attempt
                        logger.warning("report_sender.server_error", status=resp.status, wait=wait)
                        await asyncio.sleep(wait)
                        continue
                    body = await resp.text()
                    logger.error("report_sender.client_error", status=resp.status, body=body[:200])
                    return False
            except Exception:
                logger.exception("report_sender.exception", attempt=attempt)
                if attempt < 2:
                    await asyncio.sleep(2 ** attempt)
        return False

    async def send_batch(self, chat_id: str, messages: list[str], delay_s: float = 1.5) -> int:
        """Send multiple messages sequentially. Returns count of successful sends."""
        sent = 0
        for i, msg in enumerate(messages):
            ok = await self.send(chat_id, msg)
            if ok:
                sent += 1
            if i < len(messages) - 1:
                await asyncio.sleep(delay_s)
        return sent

    async def close(self) -> None:
        if self._session is not None:
            await self._session.close()
            self._session = None


class Distributor:
    """Routes rendered messages to Telegram channels by tier."""

    def __init__(self, sender: ReportSender, channels: list[ChannelConfig]) -> None:
        self._sender = sender
        self._channels = channels

    async def send(self, rendered: dict[str, list[str]]) -> None:
        """Send rendered messages to all enabled channels."""
        for ch in self._channels:
            if not ch.enabled:
                continue
            messages = rendered.get(ch.tier, [])
            if not messages:
                continue
            sent = await self._sender.send_batch(ch.chat_id, messages)
            logger.info(
                "distributor.sent",
                channel=ch.name,
                tier=ch.tier,
                total=len(messages),
                sent=sent,
            )
```

- [ ] **Step 4: Run tests**

Run: `uv run pytest tests/unit/test_report_distributor.py -v`
Expected: all PASS

- [ ] **Step 5: Commit**

```bash
git add src/hft_platform/reports/distributor.py tests/unit/test_report_distributor.py
git commit -m "feat(reports): add ReportSender and Distributor with retry and multi-channel"
```

---

### Task 10: Wire Pipeline + Integration Test

**Files:**
- Modify: `src/hft_platform/reports/pipeline.py`
- Create: `tests/unit/test_report_integration.py`

**Context:** Wire all 5 stages together in `run_pipeline()`. Add integration test that runs `--dry-run` with mocked CH data.

- [ ] **Step 1: Write integration test**

```python
# tests/unit/test_report_integration.py
"""Integration test for the full report pipeline."""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from hft_platform.reports.models import (
    Bar5m,
    DepthBar,
    FlowBar,
    LargeTrade,
    SessionData,
)
from hft_platform.reports.pipeline import run_pipeline


def _mock_session_data() -> SessionData:
    bars = [
        Bar5m(ts=f"2026-03-27 {15 + i // 12}:{(i % 12) * 5:02d}:00",
              open=330000000 - i * 100000, high=330100000 - i * 100000,
              low=329800000 - i * 100000, close=329900000 - i * 100000,
              volume=500, ticks=300)
        for i in range(24)
    ]
    flow = [
        FlowBar(ts=b.ts, ticks=300, total_vol=500, uptick_vol=200,
                 downtick_vol=250, flat_vol=50, ud_ratio=0.8, net_flow=-50)
        for b in bars
    ]
    trades = [
        LargeTrade(ts="2026-03-27 21:58:00", price=324000000, volume=28, direction="unknown"),
        LargeTrade(ts="2026-03-27 23:31:00", price=327500000, volume=32, direction="unknown"),
    ]
    return SessionData(
        session="night", symbol="TXFD6", date="2026-03-27",
        open=330490000, high=330490000, low=323750000, close=324380000,
        volume=58107, tick_count=38153,
        bars_5m=bars, flow_5m=flow, large_trades=trades,
        spread_dist={3: 147202, 4: 81011},
        depth_imbalance=[DepthBar(hour=15, avg_bid_vol=3.0, avg_ask_vol=2.8, bid_ratio=0.517)],
    )


class TestPipelineIntegration:
    @pytest.mark.asyncio
    async def test_dry_run_produces_output(self, capsys: pytest.CaptureFixture[str]) -> None:
        mock_collector = MagicMock()
        mock_collector.collect = MagicMock(return_value=_mock_session_data())

        with patch("hft_platform.reports.pipeline.DataCollector", return_value=mock_collector):
            await run_pipeline("night", "2026-03-27", dry_run=True, debug=True)

        captured = capsys.readouterr()
        assert "台指期" in captured.out
        assert "知情流" in captured.out
        assert "投資有風險" in captured.out

    @pytest.mark.asyncio
    async def test_dry_run_does_not_send(self) -> None:
        mock_collector = MagicMock()
        mock_collector.collect = MagicMock(return_value=_mock_session_data())

        with patch("hft_platform.reports.pipeline.DataCollector", return_value=mock_collector), \
             patch("hft_platform.reports.pipeline.Distributor") as mock_dist_cls:
            await run_pipeline("night", "2026-03-27", dry_run=True)
            mock_dist_cls.return_value.send.assert_not_called()
```

- [ ] **Step 2: Run test, verify fail**

Run: `uv run pytest tests/unit/test_report_integration.py -v`
Expected: FAIL (pipeline not wired yet)

- [ ] **Step 3: Wire pipeline stages**

Update `src/hft_platform/reports/pipeline.py` — replace the `run_pipeline` function:

```python
async def run_pipeline(
    session: str,
    date: str,
    *,
    dry_run: bool = False,
    debug: bool = False,
) -> None:
    """Execute the full report pipeline."""
    from hft_platform.reports.collector import DataCollector
    from hft_platform.reports.distributor import (
        Distributor,
        ReportSender,
        load_channels,
    )
    from hft_platform.reports.renderer import ReportRenderer
    from hft_platform.reports.scenarios import ScenarioBuilder
    from hft_platform.reports.signals import SignalEngine

    logger.info("report_pipeline_start", session=session, date=date, dry_run=dry_run)

    # Stage 1: Collect data
    collector = DataCollector()
    session_data = collector.collect(session, date)
    logger.info("stage1_complete", ticks=session_data.tick_count, bars=len(session_data.bars_5m))

    if session_data.tick_count == 0:
        logger.warning("report_pipeline_empty_session", session=session, date=date)
        return

    # Stage 2: Analyze signals
    engine = SignalEngine()
    signal_report = engine.analyze(session_data)
    logger.info("stage2_complete", bias=signal_report.bias, confidence=signal_report.bias_confidence)

    # Stage 3: Build scenarios
    builder = ScenarioBuilder()
    scenario_report = builder.build(signal_report)
    logger.info("stage3_complete", direction=scenario_report.direction, scenarios=len(scenario_report.scenarios))

    # Stage 4: Render
    renderer = ReportRenderer()
    rendered = {
        "free": renderer.render(scenario_report, tier="free"),
        "paid": renderer.render(scenario_report, tier="paid"),
    }
    logger.info("stage4_complete", free_msgs=len(rendered["free"]), paid_msgs=len(rendered["paid"]))

    if debug:
        for tier, msgs in rendered.items():
            print(f"\n{'=' * 40} {tier.upper()} {'=' * 40}")
            for i, m in enumerate(msgs, 1):
                print(f"\n--- Message {i}/{len(msgs)} ({len(m)} chars) ---")
                print(m)

    if dry_run:
        logger.info("report_pipeline_dry_run_complete")
        return

    # Stage 5: Distribute
    channels = load_channels()
    sender = ReportSender()
    distributor = Distributor(sender=sender, channels=channels)
    try:
        await distributor.send(rendered)
    finally:
        await sender.close()

    logger.info("report_pipeline_complete", session=session, date=date)
```

- [ ] **Step 4: Run tests**

Run: `uv run pytest tests/unit/test_report_integration.py tests/unit/test_report_pipeline.py -v`
Expected: all PASS

- [ ] **Step 5: Run full test suite for reports**

Run: `uv run pytest tests/unit/test_report_*.py -v`
Expected: all PASS across all 7 test files

- [ ] **Step 6: Lint check**

Run: `uv run ruff check src/hft_platform/reports/ tests/unit/test_report_*.py`
Expected: no errors (fix any that appear)

- [ ] **Step 7: Commit**

```bash
git add src/hft_platform/reports/pipeline.py tests/unit/test_report_integration.py
git commit -m "feat(reports): wire full pipeline and add integration test"
```

---

### Post-Implementation Checklist

After all 10 tasks are complete:

- [ ] Run `uv run pytest tests/unit/test_report_*.py -v --tb=short` — all pass
- [ ] Run `uv run ruff check src/hft_platform/reports/` — clean
- [ ] Run `uv run mypy src/hft_platform/reports/` — clean (or minimal type: ignore)
- [ ] Test dry-run against real CH data on remote:
  ```bash
  ssh ${REMOTE_USER}@${REMOTE_HOST} "cd ~/subhft && HFT_REPORT_ENABLED=1 python -m hft_platform.reports.pipeline --session night --date 2026-03-27 --debug --dry-run"
  ```
- [ ] If dry-run looks good, test real send to owner channel:
  ```bash
  ssh ${REMOTE_USER}@${REMOTE_HOST} "cd ~/subhft && HFT_REPORT_ENABLED=1 python -m hft_platform.reports.pipeline --session night --date 2026-03-27"
  ```
- [ ] Set up cron entries on remote machine (see spec §8)
