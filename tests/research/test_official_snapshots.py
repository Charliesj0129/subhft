"""Offline tests for the daily OpenAPI snapshot list: what a run asks the official site for.

Every OpenAPI endpoint serves only its latest trading day, so history is whatever is snapshotted
each day. The list therefore decides the request volume against a site we are polite to; these
tests pin it. No test touches the network: transports are scripted fakes and time is a fake clock.
    uv run pytest tests/research/test_official_snapshots.py --no-cov -q
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from pathlib import Path

import pytest

from research.data_pipeline import official

TODAY = "2026-10-17"
NEXT_DAY = "2026-10-19"
BODY = b'[{"Date": "20261016"}]'
ORIGINAL_FIVE = {
    "DailyMarketReportFut",
    "DailyMarketReportOpt",
    "MarketDataOfMajorInstitutionalTradersDetailsOfFuturesContractsBytheDate",
    "MarketDataOfMajorInstitutionalTradersDetailsOfOptionsContractsBytheDate",
    "PutCallRatio",
}
ADDED = {
    "OpenInterestOfLargeTradersFutures",
    "OpenInterestOfLargeTradersOptions",
    "DailyOptionsDelta",
    "FinalSettlementPrice",
    "DailyVolumeReportOnCalendarSpreadOrders",
    "BlockTrade",
    "PositionLimitNonEquity",
}
PER_TRADE_ENDPOINTS = {"TimeAndSalesData", "OptionsTimeAndSalesData", "TimeAndSalesDataOnCalendarSpreadOrders"}
ROBOTS = "https://openapi.taifex.com.tw/robots.txt"


class ScriptedTransport:
    def __init__(self, routes: Mapping[str, official.Response]) -> None:
        self.routes = dict(routes)
        self.urls: list[str] = []

    def __call__(self, url: str, headers: Mapping[str, str], data: bytes | None, timeout_s: float) -> official.Response:
        self.urls.append(url)
        return self.routes.get(url, official.Response(404, "text/plain", b"not found"))

    def data_urls(self) -> list[str]:
        return [url for url in self.urls if url != ROBOTS]


class FakeTime:
    def __init__(self) -> None:
        self.now = 1_000.0
        self.slept: list[float] = []

    def clock(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.slept.append(seconds)
        self.now += seconds


def _routes(**overrides: official.Response) -> dict[str, official.Response]:
    routes = {ROBOTS: official.Response(404, "text/plain", b"")}
    for name in official.OPENAPI_SNAPSHOTS:
        routes[f"{official.OPENAPI_BASE}/{name}"] = official.Response(200, "application/json", BODY)
    for name, response in overrides.items():
        routes[f"{official.OPENAPI_BASE}/{name}"] = response
    return routes


def _fetcher(root: Path, routes: Mapping[str, official.Response]):
    clock = FakeTime()
    transport = ScriptedTransport(routes)
    return official.Fetcher(root, transport=transport, clock=clock.clock, sleep=clock.sleep), transport, clock


class TestSnapshotList:
    def test_the_list_has_no_duplicates_and_only_plain_endpoint_names(self) -> None:
        names = list(official.OPENAPI_SNAPSHOTS)

        assert len(names) == len(set(names))
        assert all(re.fullmatch(r"[A-Za-z]+", name) for name in names)

    def test_the_five_original_snapshots_are_kept(self) -> None:
        assert ORIGINAL_FIVE <= set(official.OPENAPI_SNAPSHOTS)

    def test_the_list_is_the_original_five_plus_the_seven_added_endpoints(self) -> None:
        assert set(official.OPENAPI_SNAPSHOTS) == ORIGINAL_FIVE | ADDED

    def test_per_trade_endpoints_are_not_snapshotted_because_the_daily_zips_carry_them(self) -> None:
        assert PER_TRADE_ENDPOINTS.isdisjoint(official.OPENAPI_SNAPSHOTS)


class TestSnapshotRun:
    def test_every_endpoint_is_requested_once_and_stored_under_its_name_and_day(self, tmp_path: Path) -> None:
        fetcher, transport, _ = _fetcher(tmp_path, _routes())

        results = official.fetch_openapi_snapshots(fetcher, today=TODAY)

        assert transport.data_urls() == [f"{official.OPENAPI_BASE}/{name}" for name in official.OPENAPI_SNAPSHOTS]
        assert len(results) == len(official.OPENAPI_SNAPSHOTS)
        for name in official.OPENAPI_SNAPSHOTS:
            assert (tmp_path / "openapi" / name / f"{TODAY}.json").read_bytes() == BODY

    def test_requests_are_spaced_by_the_openapi_interval(self, tmp_path: Path) -> None:
        fetcher, _, clock = _fetcher(tmp_path, _routes())

        official.fetch_openapi_snapshots(fetcher, today=TODAY)

        # robots.txt is the first request and is not delayed; every later request waits 10 s.
        assert clock.slept == [10.0] * len(official.OPENAPI_SNAPSHOTS)

    def test_a_second_run_on_the_same_day_makes_no_data_request(self, tmp_path: Path) -> None:
        first, _, _ = _fetcher(tmp_path, _routes())
        official.fetch_openapi_snapshots(first, today=TODAY)
        second, transport, _ = _fetcher(tmp_path, _routes())

        results = official.fetch_openapi_snapshots(second, today=TODAY)

        assert transport.data_urls() == []
        assert [item.from_cache for item in results] == [True] * len(official.OPENAPI_SNAPSHOTS)

    def test_a_later_day_requests_every_endpoint_again_and_keeps_both_days(self, tmp_path: Path) -> None:
        # The endpoints serve only their latest day, so a later run must not be answered from the
        # earlier day's copy: history exists only if each day's response is stored under its own name.
        first, _, _ = _fetcher(tmp_path, _routes())
        official.fetch_openapi_snapshots(first, today=TODAY)
        second, transport, _ = _fetcher(tmp_path, _routes())

        results = official.fetch_openapi_snapshots(second, today=NEXT_DAY)

        assert len(transport.data_urls()) == len(official.OPENAPI_SNAPSHOTS)
        assert [item.from_cache for item in results] == [False] * len(official.OPENAPI_SNAPSHOTS)
        for name in official.OPENAPI_SNAPSHOTS:
            assert (tmp_path / "openapi" / name / f"{TODAY}.json").read_bytes() == BODY
            assert (tmp_path / "openapi" / name / f"{NEXT_DAY}.json").read_bytes() == BODY

    def test_a_block_stops_the_run_and_nothing_after_it_is_requested(self, tmp_path: Path) -> None:
        blocked = official.OPENAPI_SNAPSHOTS[2]
        routes = _routes(**{blocked: official.Response(403, "text/html", b"<html>denied</html>")})
        fetcher, transport, _ = _fetcher(tmp_path, routes)

        with pytest.raises(official.Blocked):
            official.fetch_openapi_snapshots(fetcher, today=TODAY)

        assert transport.data_urls() == [f"{official.OPENAPI_BASE}/{name}" for name in official.OPENAPI_SNAPSHOTS[:3]]
        assert not (tmp_path / "openapi" / blocked / f"{TODAY}.json").exists()

    def test_a_missing_endpoint_is_recorded_and_the_run_continues(self, tmp_path: Path) -> None:
        missing = official.OPENAPI_SNAPSHOTS[1]
        routes = _routes(**{missing: official.Response(404, "text/plain", b"not found")})
        fetcher, transport, _ = _fetcher(tmp_path, routes)

        results = official.fetch_openapi_snapshots(fetcher, today=TODAY)

        assert len(transport.data_urls()) == len(official.OPENAPI_SNAPSHOTS)
        assert len(results) == len(official.OPENAPI_SNAPSHOTS) - 1
        assert not (tmp_path / "openapi" / missing / f"{TODAY}.json").exists()


class TestParseSkipsSnapshotsWithoutAParser:
    def test_snapshots_stored_raw_are_not_parsed_and_do_not_fail_the_parse(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(official, "OPENAPI_SNAPSHOTS", ("PositionLimitNonEquity", "BlockTrade"))
        fetcher, _, _ = _fetcher(tmp_path, _routes())
        official.fetch_openapi_snapshots(fetcher, today=TODAY)

        written = official.parse_stored(tmp_path, "openapi")

        assert written == []
        assert not (tmp_path / "parsed").exists()
        assert (tmp_path / "openapi" / "BlockTrade" / f"{TODAY}.json").read_bytes() == BODY
