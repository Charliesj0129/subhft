"""The front-month watch reads the invariant directly (``ops/front_month_watch.py``)."""

from __future__ import annotations

import asyncio
import datetime as dt
from types import SimpleNamespace

import pytest
from prometheus_client import CollectorRegistry, Gauge

from hft_platform.ops.front_month_watch import (
    evaluate_front_month,
    expected_front_code,
    publish_front_month,
    run_front_month_watch,
)

TAIPEI = dt.timezone(dt.timedelta(hours=8))


def _ns(y: int, m: int, d: int, hh: int = 0, mm: int = 0) -> int:
    return int(dt.datetime(y, m, d, hh, mm, tzinfo=TAIPEI).timestamp() * 1e9)


class _Weekdays:
    def is_trading_day(self, date: dt.date) -> bool:
        return date.weekday() < 5


CAL = _Weekdays()
NOV_03 = _ns(2026, 11, 3, 10)
NOV_18_AFTER_CLOSE = _ns(2026, 11, 18, 13, 31)


def test_expected_front_code_moves_at_1330_on_delivery_day() -> None:
    assert expected_front_code("TMF", now_ns=_ns(2026, 11, 18, 13, 29), calendar=CAL) == "TMFK6"
    assert expected_front_code("TMF", now_ns=NOV_18_AFTER_CLOSE, calendar=CAL) == "TMFL6"


def test_strategy_on_the_front_month_and_feed_carrying_it_is_consistent() -> None:
    reports = evaluate_front_month(
        {"R47_MAKER_TMF": ["TMFK6"]}, {"TMFK6", "TMFL6", "2330"}, now_ns=NOV_03, calendar=CAL
    )

    assert len(reports) == 1
    report = reports[0]
    assert (report.root, report.expected, report.subscribed) == ("TMF", "TMFK6", True)
    assert report.strategies_on_front == {"R47_MAKER_TMF": True}
    assert report.off_front == ()


def test_strategy_still_on_the_settled_month_is_reported() -> None:
    """THESHOW 2026-09-16: R47 kept TMFI6 after it settled."""
    reports = evaluate_front_month(
        {"R47_MAKER_TMF": ["TMFK6"]}, {"TMFK6", "TMFL6"}, now_ns=NOV_18_AFTER_CLOSE, calendar=CAL
    )

    report = reports[0]
    assert report.expected == "TMFL6"
    assert report.strategies_on_front == {"R47_MAKER_TMF": False}
    assert report.off_front == (("R47_MAKER_TMF", "TMFK6"),)


def test_feed_missing_the_front_month_is_reported() -> None:
    """THESHOW 2026-11-02: the pool ran a universe without the current TXF."""
    reports = evaluate_front_month({"S": ["TXFK6"]}, {"TXFE6", "TMFK6"}, now_ns=NOV_03, calendar=CAL)

    assert reports[0].root == "TXF"
    assert reports[0].subscribed is False


def test_resolved_alias_target_counts_as_subscribed() -> None:
    reports = evaluate_front_month({"S": ["TMFR1"]}, {"TMFK6"}, now_ns=NOV_03, calendar=CAL)

    assert reports[0].subscribed is True
    assert reports[0].strategies_on_front == {"S": True}


def test_r2_family_is_not_reported_as_off_the_front_month() -> None:
    reports = evaluate_front_month({"S": ["TMFR2"]}, {"TMFK6"}, now_ns=NOV_03, calendar=CAL)

    assert reports[0].strategies_on_front == {"S": True}
    assert reports[0].off_front == ()


def test_stocks_options_and_unparseable_symbols_are_ignored() -> None:
    reports = evaluate_front_month({"S": ["2330", "TXO202611C23000", "not a code"]}, set(), now_ns=NOV_03, calendar=CAL)

    assert reports == []


def test_one_off_symbol_makes_the_strategy_off_front_even_with_the_front_also_held() -> None:
    reports = evaluate_front_month({"S": ["TMFK6", "TMFI6"]}, {"TMFK6"}, now_ns=NOV_03, calendar=CAL)

    assert reports[0].strategies_on_front == {"S": False}
    assert reports[0].off_front == (("S", "TMFI6"),)


def _metrics() -> SimpleNamespace:
    registry = CollectorRegistry()
    return SimpleNamespace(
        front_month_subscribed=Gauge("fm_sub", "x", ["root"], registry=registry),
        front_month_strategy_on_front=Gauge("fm_on", "x", ["strategy_id", "root"], registry=registry),
        front_month_check_timestamp_seconds=Gauge("fm_ts", "x", registry=registry),
        registry=registry,
    )


def test_publish_writes_one_and_zero_per_root_and_strategy() -> None:
    metrics = _metrics()
    reports = evaluate_front_month({"R47_MAKER_TMF": ["TMFK6"]}, {"TMFL6"}, now_ns=NOV_18_AFTER_CLOSE, calendar=CAL)

    publish_front_month(reports, metrics, now_s=1_800_000_000.0)

    reg = metrics.registry
    assert reg.get_sample_value("fm_sub", {"root": "TMF"}) == 1.0
    assert reg.get_sample_value("fm_on", {"strategy_id": "R47_MAKER_TMF", "root": "TMF"}) == 0.0
    assert reg.get_sample_value("fm_ts") == 1_800_000_000.0


def test_publish_survives_a_metrics_object_without_the_gauges() -> None:
    reports = evaluate_front_month({"S": ["TMFK6"]}, {"TMFK6"}, now_ns=NOV_03, calendar=CAL)

    import structlog.testing

    with structlog.testing.capture_logs() as logs:
        publish_front_month(reports, SimpleNamespace(), now_s=1.0)
        publish_front_month(reports, None, now_s=1.0)

    assert [entry["event"] for entry in logs] == ["front_month_watch_publish_failed"]


@pytest.mark.asyncio
async def test_watch_reads_the_runner_and_feed_and_publishes(monkeypatch: pytest.MonkeyPatch) -> None:
    from hft_platform.ops import front_month_watch

    monkeypatch.setattr(front_month_watch.timebase, "now_ns", lambda: NOV_03)
    metrics = _metrics()
    runner = SimpleNamespace(strategies=[SimpleNamespace(strategy_id="R47_MAKER_TMF", symbols={"TMFK6"})])
    feed = SimpleNamespace(subscribed_codes={"TMFK6"}, alias_to_actual={"TMFR1": "TMFK6"})

    published = asyncio.Event()

    def _metrics_getter() -> SimpleNamespace:
        published.set()
        return metrics

    task = asyncio.create_task(run_front_month_watch(runner, feed, metrics_getter=_metrics_getter, interval_s=60))
    # The first evaluation loads the exchange calendar in a worker thread; wait
    # for the publish itself rather than a fixed sleep.
    await asyncio.wait_for(published.wait(), timeout=20)
    task.cancel()

    assert metrics.registry.get_sample_value("fm_ts") > 0
    assert metrics.registry.get_sample_value("fm_on", {"strategy_id": "R47_MAKER_TMF", "root": "TMF"}) == 1.0
    assert metrics.registry.get_sample_value("fm_sub", {"root": "TMF"}) == 1.0


@pytest.mark.asyncio
async def test_watch_disabled_by_zero_interval_returns_at_once() -> None:
    result = await asyncio.wait_for(
        run_front_month_watch(SimpleNamespace(), SimpleNamespace(), interval_s=0), timeout=1.0
    )
    assert result is None


def test_bootstrap_schedules_the_watch() -> None:
    from pathlib import Path

    source = Path("src/hft_platform/services/bootstrap.py").read_text(encoding="utf-8")
    assert "deferred_tasks.append(run_front_month_watch(strategy_runner, md_client))" in source
