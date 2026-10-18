"""Offline tests for the official TAIFEX/TWSE fetcher, manifest and parsers.

No test here touches the network: transports are scripted fakes and time is a fake clock.
    uv run pytest tests/research/test_official_reference.py --no-cov -q
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from pathlib import Path

import pytest

from research.data_pipeline import official

URL = "https://openapi.taifex.com.tw/v1/DailyMarketReportFut"
TWSE_URL = "https://www.twse.com.tw/rwd/en/afterTrading/STOCK_DAY?date=20260901&stockNo=2330&response=json"
WEB_URL = "https://www.taifex.com.tw/cht/3/futPrevious30DaysSalesData"
JSON_HEADERS = "application/json"


class ScriptedTransport:
    """Serves canned responses by URL and records every call."""

    def __init__(self, routes: Mapping[str, official.Response]) -> None:
        self.routes = dict(routes)
        self.calls: list[tuple[str, Mapping[str, str], bytes | None]] = []

    def __call__(self, url: str, headers: Mapping[str, str], data: bytes | None, timeout_s: float) -> official.Response:
        self.calls.append((url, dict(headers), data))
        return self.routes.get(url, official.Response(404, "text/plain", b"not found"))


class FakeTime:
    """Monotonic clock whose sleep() advances it, so spacing is testable without waiting."""

    def __init__(self) -> None:
        self.now = 1_000.0
        self.slept: list[float] = []

    def clock(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.slept.append(seconds)
        self.now += seconds


def _ok(body: bytes = b'[{"a": 1}]', content_type: str = JSON_HEADERS) -> official.Response:
    return official.Response(200, content_type, body)


def _fetcher(tmp_path: Path, routes: Mapping[str, official.Response], **kwargs: object):
    clock = FakeTime()
    transport = ScriptedTransport(routes)
    fetcher = official.Fetcher(
        tmp_path,
        transport=transport,
        clock=clock.clock,
        sleep=clock.sleep,
        **kwargs,  # type: ignore[arg-type]
    )
    return fetcher, transport, clock


ROBOTS_OPEN = {
    "https://openapi.taifex.com.tw/robots.txt": official.Response(404, "text/plain", b""),
    "https://www.twse.com.tw/robots.txt": official.Response(404, "text/plain", b""),
    "https://www.taifex.com.tw/robots.txt": official.Response(404, "text/plain", b""),
}


class TestSpacing:
    def test_requests_to_one_source_are_spaced_by_its_minimum_interval(self, tmp_path: Path) -> None:
        routes = {**ROBOTS_OPEN, URL: _ok(), URL + "2": _ok()}
        fetcher, _, clock = _fetcher(tmp_path, routes)

        fetcher.fetch("taifex_openapi", URL, "a.json", "json")
        fetcher.fetch("taifex_openapi", URL + "2", "b.json", "json")

        # robots.txt, then a.json, then b.json: three requests, each 10 s after the previous one.
        assert clock.slept == [10.0, 10.0]

    def test_the_web_site_is_spaced_at_least_a_minute(self, tmp_path: Path) -> None:
        routes = {**ROBOTS_OPEN, WEB_URL: _ok(b"<html>", "text/html")}
        fetcher, _, clock = _fetcher(tmp_path, routes)

        fetcher.fetch("taifex_web", WEB_URL, "p.html", "html")

        assert clock.slept == [60.0]

    def test_time_already_elapsed_counts_towards_the_spacing(self, tmp_path: Path) -> None:
        routes = {**ROBOTS_OPEN, URL: _ok()}
        fetcher, _, clock = _fetcher(tmp_path, routes)
        fetcher.allowed_by_robots("taifex_openapi", URL)
        clock.now += 7.0

        fetcher.fetch("taifex_openapi", URL, "a.json", "json")

        assert clock.slept == [3.0]

    def test_spacing_is_tracked_per_source(self, tmp_path: Path) -> None:
        routes = {**ROBOTS_OPEN, URL: _ok(), TWSE_URL: _ok()}
        fetcher, _, clock = _fetcher(tmp_path, routes)

        fetcher.fetch("taifex_openapi", URL, "a.json", "json")
        fetcher.fetch("twse", TWSE_URL, "t.json", "json")

        # OpenAPI: robots.txt then the file (10 s apart). TWSE: its first request (robots.txt) is
        # not delayed by the OpenAPI history; only its own second request waits, for 5 s.
        assert clock.slept == [10.0, 5.0]


class TestStopOnBlock:
    @pytest.mark.parametrize("status", [403, 429, 503])
    def test_block_statuses_stop_the_run_and_are_recorded(self, tmp_path: Path, status: int) -> None:
        routes = {**ROBOTS_OPEN, URL: official.Response(status, "text/html", b"<html>denied</html>")}
        fetcher, transport, _ = _fetcher(tmp_path, routes)

        with pytest.raises(official.Blocked) as excinfo:
            fetcher.fetch("taifex_openapi", URL, "a.json", "json")

        assert str(status) in excinfo.value.reason
        recorded = [e for e in fetcher.manifest.entries() if e.status == official.BLOCKED_STATUS]
        assert [e.http_status for e in recorded] == [status]
        assert not (tmp_path / "a.json").exists()
        assert len(transport.calls) == 2  # robots.txt + the blocked request; nothing retried

    def test_an_html_challenge_page_served_as_200_is_a_block(self, tmp_path: Path) -> None:
        page = b"<!DOCTYPE html><html><title>Just a moment...</title><div id='cf-chl'></div></html>"
        routes = {**ROBOTS_OPEN, URL: official.Response(200, "text/html; charset=UTF-8", page)}
        fetcher, _, _ = _fetcher(tmp_path, routes)

        with pytest.raises(official.Blocked):
            fetcher.fetch("taifex_openapi", URL, "a.json", "json")

        assert not (tmp_path / "a.json").exists()

    def test_the_body_of_a_block_is_kept_for_a_person_to_read(self, tmp_path: Path) -> None:
        page = "<html><body>請選擇契約</body></html>".encode("cp950")
        routes = {**ROBOTS_OPEN, URL: official.Response(200, "text/html;charset=MS950", page)}
        fetcher, _, _ = _fetcher(tmp_path, routes)

        with pytest.raises(official.Blocked):
            fetcher.fetch("taifex_openapi", URL, "a.json", "json")

        blocked = [e for e in fetcher.manifest.entries() if e.status == official.BLOCKED_STATUS][0]
        kept = tmp_path / blocked.note.split("body kept at ")[1]
        assert kept.read_bytes() == page
        assert not (tmp_path / "a.json").exists()

    def test_html_is_acceptable_when_html_was_requested(self, tmp_path: Path) -> None:
        routes = {**ROBOTS_OPEN, WEB_URL: official.Response(200, "text/html", b"<html><body>list</body></html>")}
        fetcher, _, _ = _fetcher(tmp_path, routes)

        result = fetcher.fetch("taifex_web", WEB_URL, "page.html", "html")

        assert result is not None and result.path.read_bytes().startswith(b"<html>")

    def test_a_challenge_is_a_block_even_when_html_was_requested(self, tmp_path: Path) -> None:
        page = b"<html><head><title>Attention Required! | Cloudflare</title></head></html>"
        routes = {**ROBOTS_OPEN, WEB_URL: official.Response(200, "text/html", page)}
        fetcher, _, _ = _fetcher(tmp_path, routes)

        with pytest.raises(official.Blocked):
            fetcher.fetch("taifex_web", WEB_URL, "page.html", "html")

    def test_the_run_cannot_continue_after_a_block_without_a_new_fetcher(self, tmp_path: Path) -> None:
        """Stopping is the caller's job; the manifest must still say the last attempt was a block."""
        routes = {**ROBOTS_OPEN, URL: official.Response(429, "text/html", b"slow down")}
        fetcher, _, _ = _fetcher(tmp_path, routes)
        with pytest.raises(official.Blocked):
            fetcher.fetch("taifex_openapi", URL, "a.json", "json")

        assert list(fetcher.manifest.entries())[-1].status == official.BLOCKED_STATUS


class TestManifestAndResume:
    def test_a_fetched_file_is_stored_verbatim_with_its_sha256(self, tmp_path: Path) -> None:
        body = b'[{"Date":"20261001","Volume":"34458"}]'
        fetcher, _, _ = _fetcher(tmp_path, {**ROBOTS_OPEN, URL: _ok(body)})

        result = fetcher.fetch("taifex_openapi", URL, "fut/20261001.json", "json")

        assert result is not None
        assert result.path.read_bytes() == body
        assert result.entry.sha256 == official.sha256_hex(body)
        assert result.entry.url == URL
        assert result.entry.size_bytes == len(body)
        assert fetcher.manifest.verify(tmp_path) == []

    def test_a_second_run_skips_what_the_manifest_already_holds(self, tmp_path: Path) -> None:
        routes = {**ROBOTS_OPEN, URL: _ok()}
        first, _, _ = _fetcher(tmp_path, routes)
        first.fetch("taifex_openapi", URL, "a.json", "json")

        second, transport, _ = _fetcher(tmp_path, routes)
        result = second.fetch("taifex_openapi", URL, "a.json", "json")

        assert result is not None and result.from_cache
        assert transport.calls == []

    def test_a_file_deleted_from_disk_is_fetched_again(self, tmp_path: Path) -> None:
        routes = {**ROBOTS_OPEN, URL: _ok()}
        first, _, _ = _fetcher(tmp_path, routes)
        first.fetch("taifex_openapi", URL, "a.json", "json")
        (tmp_path / "a.json").unlink()

        second, transport, _ = _fetcher(tmp_path, routes)
        second.fetch("taifex_openapi", URL, "a.json", "json")

        assert any(call[0] == URL for call in transport.calls)

    def test_verify_reports_a_tampered_file(self, tmp_path: Path) -> None:
        fetcher, _, _ = _fetcher(tmp_path, {**ROBOTS_OPEN, URL: _ok()})
        fetcher.fetch("taifex_openapi", URL, "a.json", "json")
        (tmp_path / "a.json").write_bytes(b"tampered")

        assert fetcher.manifest.verify(tmp_path) == ["sha256 mismatch: a.json"]

    def test_a_404_is_recorded_and_returns_none(self, tmp_path: Path) -> None:
        fetcher, _, _ = _fetcher(tmp_path, {**ROBOTS_OPEN})

        assert fetcher.fetch("taifex_openapi", URL, "a.json", "json") is None
        assert [e.status for e in fetcher.manifest.entries()][-1] == "NOT_FOUND"

    def test_a_final_404_is_not_asked_again_but_a_plain_404_is(self, tmp_path: Path) -> None:
        first, _, _ = _fetcher(tmp_path, {**ROBOTS_OPEN})
        first.fetch("taifex_openapi", URL, "a.json", "json", final_404=True)

        again, transport, _ = _fetcher(tmp_path, {**ROBOTS_OPEN})
        assert again.fetch("taifex_openapi", URL, "a.json", "json", final_404=True) is None
        assert [call[0] for call in transport.calls] == []

        retry, transport, _ = _fetcher(tmp_path, {**ROBOTS_OPEN})
        retry.fetch("taifex_openapi", URL, "a.json", "json")
        assert URL in [call[0] for call in transport.calls]

    def test_post_bodies_are_part_of_the_request_identity(self) -> None:
        assert official.request_key(WEB_URL, b"a=1") != official.request_key(WEB_URL, b"a=2")
        assert official.request_key(WEB_URL) == WEB_URL

    def test_the_manifest_is_valid_jsonl_with_the_audit_fields(self, tmp_path: Path) -> None:
        fetcher, _, _ = _fetcher(tmp_path, {**ROBOTS_OPEN, URL: _ok()})
        fetcher.fetch("taifex_openapi", URL, "a.json", "json")

        lines = (tmp_path / official.MANIFEST_NAME).read_text(encoding="utf-8").splitlines()
        last = json.loads(lines[-1])

        assert {"url", "fetched_at", "sha256", "size_bytes", "http_status", "status", "path"} <= set(last)


class TestBudgetAndRobots:
    def test_the_request_cap_stops_the_run_cleanly(self, tmp_path: Path) -> None:
        routes = {**ROBOTS_OPEN, URL: _ok(), URL + "2": _ok(), URL + "3": _ok()}
        fetcher, transport, _ = _fetcher(tmp_path, routes, max_requests=3)

        fetcher.fetch("taifex_openapi", URL, "a.json", "json")
        fetcher.fetch("taifex_openapi", URL + "2", "b.json", "json")
        with pytest.raises(official.RequestBudgetExhausted):
            fetcher.fetch("taifex_openapi", URL + "3", "c.json", "json")

        assert len(transport.calls) == 3

    def test_a_path_that_robots_txt_disallows_is_never_requested(self, tmp_path: Path) -> None:
        robots = b"User-agent: *\nDisallow: /v1/\n"
        routes = {"https://openapi.taifex.com.tw/robots.txt": official.Response(200, "text/plain", robots), URL: _ok()}
        fetcher, transport, _ = _fetcher(tmp_path, routes)

        with pytest.raises(official.RobotsDisallowed):
            fetcher.fetch("taifex_openapi", URL, "a.json", "json")

        assert URL not in [call[0] for call in transport.calls]

    def test_an_html_error_page_in_place_of_robots_txt_means_no_rules(self, tmp_path: Path) -> None:
        routes = {
            "https://openapi.taifex.com.tw/robots.txt": official.Response(200, "text/html", b"<html>404</html>"),
            URL: _ok(),
        }
        fetcher, _, _ = _fetcher(tmp_path, routes)

        assert fetcher.fetch("taifex_openapi", URL, "a.json", "json") is not None

    def test_robots_txt_is_read_once_per_source(self, tmp_path: Path) -> None:
        routes = {**ROBOTS_OPEN, URL: _ok(), URL + "2": _ok()}
        fetcher, transport, _ = _fetcher(tmp_path, routes)

        fetcher.fetch("taifex_openapi", URL, "a.json", "json")
        fetcher.fetch("taifex_openapi", URL + "2", "b.json", "json")

        assert [c[0] for c in transport.calls].count("https://openapi.taifex.com.tw/robots.txt") == 1

    def test_requests_carry_an_honest_user_agent(self, tmp_path: Path) -> None:
        fetcher, transport, _ = _fetcher(tmp_path, {**ROBOTS_OPEN, URL: _ok()})

        fetcher.fetch("taifex_openapi", URL, "a.json", "json")

        agents = {call[1]["User-Agent"] for call in transport.calls}
        assert agents == {official.USER_AGENT}
        assert "Mozilla" not in official.USER_AGENT


class TestRedirects:
    def test_a_redirected_robots_txt_means_no_rules_and_records_where_it_pointed(self, tmp_path: Path) -> None:
        moved = official.Response(302, "text/html", b"", "https://openapi.taifex.com.tw/")
        routes = {"https://openapi.taifex.com.tw/robots.txt": moved, URL: _ok()}
        fetcher, _, _ = _fetcher(tmp_path, routes)

        assert fetcher.fetch("taifex_openapi", URL, "a.json", "json") is not None
        redirects = [e for e in fetcher.manifest.entries() if e.status == "REDIRECT"]
        assert [e.note for e in redirects] == ["-> https://openapi.taifex.com.tw/"]

    def test_a_redirect_on_a_data_url_stops_the_run(self, tmp_path: Path) -> None:
        moved = official.Response(302, "text/html", b"", "https://elsewhere.example/challenge")
        fetcher, _, _ = _fetcher(tmp_path, {**ROBOTS_OPEN, URL: moved})

        with pytest.raises(official.Blocked) as excinfo:
            fetcher.fetch("taifex_openapi", URL, "a.json", "json")

        assert "elsewhere.example" in excinfo.value.reason

    def test_redirects_are_never_followed_by_the_default_transport(self) -> None:
        assert official._NoRedirect().redirect_request() is None


class TestInputGuards:
    def test_only_https_urls_on_the_sources_own_host_are_allowed(self, tmp_path: Path) -> None:
        fetcher, transport, _ = _fetcher(tmp_path, {})

        for bad in ("http://openapi.taifex.com.tw/v1/x", "https://evil.example/v1/x", "file:///etc/passwd"):
            with pytest.raises(ValueError):
                fetcher.fetch("taifex_openapi", bad, "x.json", "json")

        assert transport.calls == []

    @pytest.mark.parametrize("relpath", ["../x.json", "/abs/x.json", "a/../../x.json", ""])
    def test_a_relative_path_cannot_escape_the_data_root(self, tmp_path: Path, relpath: str) -> None:
        fetcher, _, _ = _fetcher(tmp_path, {**ROBOTS_OPEN, URL: _ok()})

        with pytest.raises(ValueError):
            fetcher.fetch("taifex_openapi", URL, relpath, "json")

    def test_text_is_decoded_strictly_from_utf8_or_big5(self) -> None:
        assert official.decode_text("交易日期".encode("utf-8")) == "交易日期"
        assert official.decode_text("交易日期".encode("cp950")) == "交易日期"
        with pytest.raises(official.OfficialDataError):
            official.decode_text(b"\xff\xfe\xfa\xfb\x80\x81")


FUT_JSON = json.dumps(
    [
        {
            "Date": "20261005",
            "Contract": "TX",
            "ContractMonth(Week)": "202610",
            "Open": "49500",
            "High": "50010",
            "Low": "49320",
            "Last": "49949",
            "Change": "432",
            "%": "0.87%",
            "Volume": "41858",
            "SettlementPrice": "49944",
            "OpenInterest": "108339",
            "BestBid": "49948",
            "BestAsk": "49950",
            "HistoricalHigh": "50010",
            "HistoricalLow": "7000",
            "TradingHalt": "",
            "TradingSession": "一般",
            "Volume(ExecutionsAmongSpreadOrderAndSingleOrderOnly)": "",
        },
        {
            "Date": "20261005",
            "Contract": "TX",
            "ContractMonth(Week)": "202610",
            "Open": "49100",
            "High": "49400",
            "Low": "49000",
            "Last": "49346",
            "Change": "-171",
            "%": "-0.35%",
            "Volume": "32,288",
            "SettlementPrice": "NULL",
            "OpenInterest": "-",
            "BestBid": "49345",
            "BestAsk": "49347",
            "HistoricalHigh": "50010",
            "HistoricalLow": "7000",
            "TradingHalt": "",
            "TradingSession": "盤後",
            "Volume(ExecutionsAmongSpreadOrderAndSingleOrderOnly)": "",
        },
    ],
    ensure_ascii=False,
).encode("utf-8")

STOCK_DAY_JSON = json.dumps(
    {
        "stat": "OK",
        "date": "20260901",
        "title": "2026/09  Daily Trading Value/Volume of 2330 ",
        "fields": [
            "Date",
            "Trade Volume",
            "Trade Value",
            "Opening Price",
            "Highest Price",
            "Lowest Price",
            "Closing Price",
            "Change",
            "Transaction",
            "Mark",
        ],
        "data": [
            [
                "2026/09/01",
                "31,855,287",
                "77,463,413,685",
                "2,395.00",
                "2,440.00",
                "2,390.00",
                "2,440.00",
                "+35.00",
                "65,271",
                "",
            ],
            [
                "2026/09/02",
                "25,151,394",
                "60,261,105,308",
                "2,415.00",
                "2,420.00",
                "2,385.00",
                "2,385.00",
                "-55.00",
                "198,779",
                "",
            ],
        ],
    }
).encode("utf-8")


class TestParsers:
    def test_numbers_drop_commas_signs_and_the_exchanges_placeholders(self) -> None:
        assert official.parse_number("1,234.5") == 1234.5
        assert official.parse_number("+35.00") == 35.0
        assert official.parse_number("-55.00") == -55.0
        assert official.parse_number("0.25%") == 0.25
        for placeholder in ("", "-", "--", "NULL", "X", None, "n/a-garbage"):
            assert official.parse_number(placeholder) is None
        assert official.parse_int("32,288") == 32288

    def test_dates_accept_both_exchange_formats_and_reject_anything_else(self) -> None:
        assert official.iso_date("20261005") == "2026-10-05"
        assert official.iso_date("2026/10/05") == "2026-10-05"
        with pytest.raises(official.OfficialDataError):
            official.iso_date("115/10/05")

    def test_openapi_futures_rows_keep_the_session_and_blank_the_placeholders(self) -> None:
        rows = official.parse_openapi_daily_market(FUT_JSON, options=False)

        assert [r["session"] for r in rows] == ["regular", "after_hours"]
        assert rows[0]["date"] == "2026-10-05"
        assert rows[0]["volume"] == 41858
        assert rows[0]["settlement"] == 49944.0
        assert rows[1]["volume"] == 32288
        assert rows[1]["settlement"] is None
        assert rows[1]["open_interest"] is None
        assert rows[1]["change"] == -171.0

    def test_openapi_rows_with_an_unknown_session_are_an_error_not_a_guess(self) -> None:
        bad = json.dumps(
            [{"Date": "20261005", "Contract": "TX", "ContractMonth(Week)": "202610", "TradingSession": "??"}]
        )

        with pytest.raises(official.OfficialDataError):
            official.parse_openapi_daily_market(bad.encode(), options=False)

    def test_openapi_options_rows_carry_strike_and_right(self) -> None:
        body = json.dumps(
            [
                {
                    "Date": "20261005",
                    "Contract": "TXO",
                    "ContractMonth(Week)": "202610",
                    "StrikePrice": "50,000",
                    "CallPut": "買權",
                    "Open": "-",
                    "High": "-",
                    "Low": "-",
                    "Close": "120",
                    "Volume": "1,500",
                    "SettlementPrice": "118",
                    "OpenInterest": "9000",
                    "BestBid": "117",
                    "BestAsk": "119",
                    "TradingSession": "一般",
                }
            ],
            ensure_ascii=False,
        ).encode("utf-8")

        row = official.parse_openapi_daily_market(body, options=True)[0]

        assert (row["strike"], row["right"], row["last"], row["volume"]) == (50000.0, "C", 120.0, 1500)
        assert row["open"] is None

    def test_a_non_array_body_is_rejected(self) -> None:
        with pytest.raises(official.OfficialDataError):
            official.parse_openapi_daily_market(b'{"error": "x"}', options=False)

    def test_twse_stock_day_rows_are_typed(self) -> None:
        rows = official.parse_twse_stock_day(STOCK_DAY_JSON)

        assert rows[0] == {
            "date": "2026-09-01",
            "volume": 31855287,
            "value": 77463413685,
            "open": 2395.0,
            "high": 2440.0,
            "low": 2390.0,
            "close": 2440.0,
            "change": 35.0,
            "transactions": 65271,
        }
        assert rows[1]["change"] == -55.0

    def test_twse_stock_day_refuses_a_non_ok_stat_or_changed_columns(self) -> None:
        with pytest.raises(official.OfficialDataError):
            official.parse_twse_stock_day(b'{"stat": "No Data"}')
        changed = json.loads(STOCK_DAY_JSON)
        changed["fields"][1] = "Shares"
        with pytest.raises(official.OfficialDataError):
            official.parse_twse_stock_day(json.dumps(changed).encode())


def _listing(kind_dir: str, prefix: str, days: list[str]) -> bytes:
    rows = "".join(
        f"<tr><td>{d.replace('_', '/')} PM 04:37:40</td><td>{d.replace('_', '/')}</td><td>"
        f'<input type="button" onClick="javascript:window.open(\'https://www.taifex.com.tw/file/taifex/Dailydownload/'
        f'{kind_dir}/{prefix}_{d}.zip\')" value="rpt"></td><td>'
        f'<input type="button" onClick="javascript:window.open(\'https://www.taifex.com.tw/file/taifex/Dailydownload/'
        f'{kind_dir}CSV/{prefix}_{d}.zip\')" value="csv"></td></tr>'
        for d in days
    )
    return f"<html><body><table>{rows}</table></body></html>".encode()


class TestTickDatasets:
    def test_tick_links_pick_the_csv_zip_for_each_listed_day(self) -> None:
        page = _listing("Dailydownload", "Daily", ["2026_10_01", "2026_10_02"])

        links = official.tick_links(page)

        assert links == {
            "2026-10-01": "https://www.taifex.com.tw/file/taifex/Dailydownload/DailydownloadCSV/Daily_2026_10_01.zip",
            "2026-10-02": "https://www.taifex.com.tw/file/taifex/Dailydownload/DailydownloadCSV/Daily_2026_10_02.zip",
        }

    def test_option_listings_use_their_own_file_prefix(self) -> None:
        page = _listing("OptionsDailydownload", "OptionsDaily", ["2026_10_01"])

        assert official.tick_links(page)["2026-10-01"].endswith("OptionsDailydownloadCSV/OptionsDaily_2026_10_01.zip")

    def test_fetch_ticks_downloads_only_the_requested_range(self, tmp_path: Path) -> None:
        zip_url = "https://www.taifex.com.tw/file/taifex/Dailydownload/DailydownloadCSV/Daily_{}.zip"
        routes = {
            **ROBOTS_OPEN,
            official.TICK_LISTINGS["fut_ticks"]: official.Response(
                200, "text/html", _listing("Dailydownload", "Daily", ["2026_09_30", "2026_10_01", "2026_10_02"])
            ),
            zip_url.format("2026_10_01"): official.Response(200, "application/zip", b"PK\x03\x04one"),
            zip_url.format("2026_10_02"): official.Response(200, "application/zip", b"PK\x03\x04two"),
        }
        fetcher, transport, _ = _fetcher(tmp_path, routes)

        results = official.fetch_ticks(fetcher, "fut_ticks", "2026-10-01", "2026-10-02", today="2026-10-06")

        assert [r.path.name for r in results] == ["2026-10-01.zip", "2026-10-02.zip"]
        assert zip_url.format("2026_09_30") not in [call[0] for call in transport.calls]

    def test_the_listing_is_fetched_again_on_a_new_day_but_not_twice_on_the_same_day(self, tmp_path: Path) -> None:
        routes = {
            **ROBOTS_OPEN,
            official.TICK_LISTINGS["fut_ticks"]: official.Response(
                200, "text/html", _listing("Dailydownload", "Daily", [])
            ),
        }
        first, _, _ = _fetcher(tmp_path, routes)
        official.fetch_ticks(first, "fut_ticks", "2026-10-01", "2026-10-02", today="2026-10-06")

        same_day, transport_same, _ = _fetcher(tmp_path, routes)
        official.fetch_ticks(same_day, "fut_ticks", "2026-10-01", "2026-10-02", today="2026-10-06")
        next_day, transport_next, _ = _fetcher(tmp_path, routes)
        official.fetch_ticks(next_day, "fut_ticks", "2026-10-01", "2026-10-02", today="2026-10-07")

        listing = official.TICK_LISTINGS["fut_ticks"]
        assert listing not in [call[0] for call in transport_same.calls]
        assert listing in [call[0] for call in transport_next.calls]

    def test_a_body_that_is_not_a_zip_is_an_error_and_is_not_stored(self, tmp_path: Path) -> None:
        zip_url = "https://www.taifex.com.tw/file/taifex/Dailydownload/DailydownloadCSV/Daily_2026_10_01.zip"
        routes = {
            **ROBOTS_OPEN,
            official.TICK_LISTINGS["fut_ticks"]: official.Response(
                200, "text/html", _listing("Dailydownload", "Daily", ["2026_10_01"])
            ),
            zip_url: official.Response(200, "application/zip", b"not a zip"),
        }
        fetcher, _, _ = _fetcher(tmp_path, routes)

        with pytest.raises(official.OfficialDataError):
            official.fetch_ticks(fetcher, "fut_ticks", "2026-10-01", "2026-10-01", today="2026-10-06")

        assert not (tmp_path / "taifex/fut_ticks/2026-10-01.zip").exists()


class PostRecorder(ScriptedTransport):
    """Serves one response to any POST and keeps the bodies."""

    def __init__(self, response: official.Response, routes: Mapping[str, official.Response]) -> None:
        super().__init__(routes)
        self.response = response

    def __call__(self, url, headers, data, timeout_s):  # type: ignore[no-untyped-def]
        if data is not None:
            self.calls.append((url, dict(headers), data))
            return self.response
        return super().__call__(url, headers, data, timeout_s)


def _post_fetcher(tmp_path: Path, response: official.Response):
    clock = FakeTime()
    transport = PostRecorder(response, ROBOTS_OPEN)
    fetcher = official.Fetcher(tmp_path, transport=transport, clock=clock.clock, sleep=clock.sleep)
    return fetcher, transport


class TestHistoryDownloads:
    def test_month_windows_split_a_range_at_calendar_month_ends(self) -> None:
        assert official.month_windows("2026-01-26", "2026-03-05") == [
            ("2026-01-26", "2026-01-31"),
            ("2026-02-01", "2026-02-28"),
            ("2026-03-01", "2026-03-05"),
        ]
        assert official.month_windows("2026-10-01", "2026-10-01") == [("2026-10-01", "2026-10-01")]
        assert official.month_windows("2026-10-05", "2026-10-01") == []

    def test_a_leap_february_is_one_window(self) -> None:
        assert official.month_windows("2028-02-01", "2028-02-29") == [("2028-02-01", "2028-02-29")]

    def test_the_annual_download_posts_the_year_the_way_the_site_form_does(self, tmp_path: Path) -> None:
        fetcher, transport = _post_fetcher(tmp_path, official.Response(200, "application/zip", b"PK\x03\x04year"))

        result = official.fetch_history_year(fetcher, "fut_daily", 2024)

        assert result is not None and result.path.name == "year_2024.zip"
        posts = [call for call in transport.calls if call[2] is not None]
        assert posts[0][0] == "https://www.taifex.com.tw/cht/3/futDataDown"
        assert posts[0][2] == b"down_type=2&his_year=2024"

    def test_options_history_uses_the_options_endpoint_and_starts_in_2001(self, tmp_path: Path) -> None:
        fetcher, transport = _post_fetcher(tmp_path, official.Response(200, "application/zip", b"PK\x03\x04year"))

        official.fetch_history_year(fetcher, "opt_daily", 2001)
        with pytest.raises(ValueError):
            official.fetch_history_year(fetcher, "opt_daily", 2000)

        assert [call[0] for call in transport.calls if call[2] is not None] == [
            "https://www.taifex.com.tw/cht/3/optDataDown"
        ]

    def test_a_range_download_makes_one_post_per_month_with_slash_dates(self, tmp_path: Path) -> None:
        fetcher, transport = _post_fetcher(tmp_path, official.Response(200, "text/csv", b"a,b\n1,2\n"))

        results = official.fetch_history_range(
            fetcher, "fut_daily", "2026-01-26", "2026-02-10", commodity="TX", today="2026-10-06"
        )

        posts = [call[2] for call in transport.calls if call[2] is not None]
        assert posts == [
            b"down_type=1&commodity_id=TX&commodity_id2=&queryStartDate=2026%2F01%2F26&queryEndDate=2026%2F01%2F31",
            b"down_type=1&commodity_id=TX&commodity_id2=&queryStartDate=2026%2F02%2F01&queryEndDate=2026%2F02%2F10",
        ]
        assert [r.path.name for r in results] == ["TX_2026-01-26_2026-01-31.csv", "TX_2026-02-01_2026-02-10.csv"]

    def test_a_finished_month_is_cached_but_the_current_month_is_fetched_again_tomorrow(self, tmp_path: Path) -> None:
        response = official.Response(200, "text/csv", b"a,b\n1,2\n")
        first, _ = _post_fetcher(tmp_path, response)
        official.fetch_history_range(first, "fut_daily", "2026-09-01", "2026-10-06", today="2026-10-06")

        again, transport = _post_fetcher(tmp_path, response)
        official.fetch_history_range(again, "fut_daily", "2026-09-01", "2026-10-06", today="2026-10-06")
        assert [c for c in transport.calls if c[2] is not None] == []

        tomorrow, transport = _post_fetcher(tmp_path, response)
        official.fetch_history_range(tomorrow, "fut_daily", "2026-09-01", "2026-10-07", today="2026-10-07")
        bodies = [c[2] for c in transport.calls if c[2] is not None]
        assert len(bodies) == 1 and b"queryStartDate=2026%2F10%2F01" in bodies[0]

    def test_a_challenge_page_in_reply_to_a_download_post_stops_the_run(self, tmp_path: Path) -> None:
        page = official.Response(200, "text/html", b"<html><title>Just a moment...</title></html>")
        fetcher, _ = _post_fetcher(tmp_path, page)

        with pytest.raises(official.Blocked):
            official.fetch_history_year(fetcher, "fut_daily", 2024)


# ---------------------------------------------------------------------------
# Tick files: parsing, Parquet, reconciliation, CLI
# ---------------------------------------------------------------------------

FUT_TICK_CSV = "\n".join(
    [
        ",".join(official.FUT_TICK_HEADER),
        "20261005,TX,202610,084500,49500,4,,,*",  # 2 contracts (B+S), opening auction
        "20261005,TX,202610,090000,49510,6,,,",  # 3 contracts
        "20261005,TX,202610/202611,090100,-12,2,,,",  # a calendar spread: 1 contract
        "20261004,TX,202610,150000,49400,2,,,",  # night session of trading day 20261005: 1 contract
        "20261005,TX,202610,133000,49480,2,,,",  # 1 contract, closing minutes
        "",
    ]
)
OPT_TICK_CSV = "\n".join(
    [
        ",".join(official.OPT_TICK_HEADER),
        "-" * 20 + ",--,--,--,--,--,--,--,--",
        "20261005,TXO,50000,202610,C,090000,120.5,7,",
        "20261005,TXO,50000,202610,P,090001,95,3,*",
        "",
    ]
)


def _tick_zip(path: Path, text: str) -> None:
    import zipfile

    path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("Daily_2026_10_05.csv", text.encode("cp950"))


def _manifest_with(root: Path, relpath: str) -> None:
    data = (root / relpath).read_bytes()
    official.Manifest(root / "manifest.jsonl").append(
        official.ManifestEntry(
            key=relpath,
            source="taifex_web",
            url="https://example.invalid/" + relpath,
            fetched_at="2026-10-06T00:00:00+00:00",
            status="OK",
            http_status=200,
            sha256=official.sha256_hex(data),
            size_bytes=len(data),
            path=relpath,
            content_type="application/zip",
        )
    )


class TestTickParsing:
    def test_futures_quantity_is_halved_because_the_file_counts_both_sides(self) -> None:
        rows = list(official.parse_tick_csv(FUT_TICK_CSV, options=False))

        assert [row["contracts"] for row in rows] == [2, 3, 1, 1, 1]
        assert rows[0]["auction"] is True and rows[1]["auction"] is False
        assert [row["spread"] for row in rows] == [False, False, True, False, False]

    def test_an_odd_futures_quantity_is_a_format_surprise_and_raises(self) -> None:
        text = ",".join(official.FUT_TICK_HEADER) + "\n20261005,TX,202610,090000,49510,5,,,\n"

        with pytest.raises(official.OfficialDataError, match="even"):
            list(official.parse_tick_csv(text, options=False))

    def test_options_quantity_is_taken_as_contracts_and_the_dashed_row_is_skipped(self) -> None:
        rows = list(official.parse_tick_csv(OPT_TICK_CSV, options=True))

        assert [(row["right"], row["strike"], row["contracts"]) for row in rows] == [
            ("C", 50000.0, 7),
            ("P", 50000.0, 3),
        ]

    def test_a_changed_header_is_refused(self) -> None:
        with pytest.raises(official.OfficialDataError, match="header"):
            list(official.parse_tick_csv("a,b,c\n1,2,3\n", options=False))

    def test_sessions_follow_the_trading_day_not_the_calendar_date(self) -> None:
        assert official.tick_session("2026-10-05", "2026-10-05", 9 * 3600) == "day"
        assert official.tick_session("2026-10-05", "2026-10-04", 15 * 3600) == "night"
        assert official.tick_session("2026-10-05", "2026-10-05", 3 * 3600) == "night"
        assert official.tick_session("2026-10-05", "2026-10-05", 14 * 3600) == "other"

    def test_a_monday_file_carries_the_friday_night_session_across_both_calendar_dates(self) -> None:
        assert official.tick_session("2026-10-05", "2026-10-02", 20 * 3600) == "night"
        assert official.tick_session("2026-10-05", "2026-10-03", 2 * 3600) == "night"
        assert official.tick_session("2026-10-05", "2026-10-03", 9 * 3600) == "other"
        assert official.tick_session("2026-10-05", "2026-10-05", 16 * 3600) == "other"

    def test_the_summary_groups_contracts_and_trades_by_session_and_leg(self) -> None:
        rows = list(official.parse_tick_csv(FUT_TICK_CSV, options=False))

        summary = official.summarize_ticks(rows, "2026-10-05")

        single = {(item["session"]): item["contracts"] for item in summary if not item["spread"]}
        assert single == {"day": 6, "night": 1}
        assert [item for item in summary if item["spread"]] == [
            {"product": "TX", "expiry": "202610/202611", "session": "day", "spread": True, "contracts": 1, "trades": 1}
        ]


class TestParsedLayer:
    def test_tick_zips_become_parquet_and_a_summary(self, tmp_path: Path) -> None:
        import pyarrow.parquet as pq

        _tick_zip(tmp_path / "taifex/fut_ticks/2026-10-05.zip", FUT_TICK_CSV)
        _manifest_with(tmp_path, "taifex/fut_ticks/2026-10-05.zip")

        written = official.parse_stored(tmp_path, "fut_ticks")

        assert [path.relative_to(tmp_path).as_posix() for path in written] == ["parsed/fut_ticks/2026-10-05.parquet"]
        table = pq.read_table(written[0])
        assert table.num_rows == 5
        assert set(table.column("session").to_pylist()) == {"day", "night"}
        assert json.loads((tmp_path / "parsed/fut_ticks/2026-10-05.summary.json").read_text("utf-8"))

    def test_a_file_changed_after_download_is_not_parsed(self, tmp_path: Path) -> None:
        _tick_zip(tmp_path / "taifex/fut_ticks/2026-10-05.zip", FUT_TICK_CSV)
        _manifest_with(tmp_path, "taifex/fut_ticks/2026-10-05.zip")
        _tick_zip(tmp_path / "taifex/fut_ticks/2026-10-05.zip", FUT_TICK_CSV + "20261005,TX,202610,090500,49500,2,,,\n")

        with pytest.raises(official.OfficialDataError, match="sha256"):
            official.parse_stored(tmp_path, "fut_ticks")

        assert not (tmp_path / "parsed").exists()

    def test_openapi_and_twse_files_are_parsed_by_their_own_parsers(self, tmp_path: Path) -> None:
        import pyarrow.parquet as pq

        (tmp_path / "openapi/DailyMarketReportFut").mkdir(parents=True)
        (tmp_path / "openapi/DailyMarketReportFut/2026-10-05.json").write_bytes(FUT_JSON)
        (tmp_path / "twse/stock_day/2330").mkdir(parents=True)
        (tmp_path / "twse/stock_day/2330/202609.json").write_bytes(STOCK_DAY_JSON)
        _manifest_with(tmp_path, "openapi/DailyMarketReportFut/2026-10-05.json")
        _manifest_with(tmp_path, "twse/stock_day/2330/202609.json")

        fut = official.parse_stored(tmp_path, "openapi")
        stocks = official.parse_stored(tmp_path, "twse_stock_day")

        assert pq.read_table(fut[0]).num_rows == 2
        assert pq.read_table(stocks[0]).column("stock").to_pylist() == ["2330", "2330"]

    def test_an_empty_parse_writes_no_file(self, tmp_path: Path) -> None:
        assert official.write_parquet([], tmp_path / "x.parquet") == 0
        assert list(tmp_path.iterdir()) == []


def _official_volume(rows: list[tuple[str, str, int]]) -> list[dict[str, object]]:
    sessions = {"day": "regular", "night": "after_hours"}
    return [
        {"contract": "TX", "expiry": expiry, "session": sessions[session], "volume": volume}
        for expiry, session, volume in rows
    ]


def _tick_total(expiry: str, session: str, contracts: int, *, spread: bool = False, product: str = "TX") -> dict:
    return {
        "product": product,
        "expiry": expiry,
        "session": session,
        "spread": spread,
        "contracts": contracts,
        "trades": 1,
    }


class TestReconciliation:
    def test_ticks_are_compared_with_the_official_volume_per_session(self) -> None:
        ticks = [_tick_total("202610", "day", 34_278), _tick_total("202610", "night", 28_717)]
        rows = official.reconcile_volume(
            ticks, _official_volume([("202610", "day", 34_280), ("202610", "night", 28_717)]), contract="TX"
        )

        assert [(r["session"], r["ticks"], r["official"], r["difference"]) for r in rows] == [
            ("day", 34_278, 34_280, -2),
            ("night", 28_717, 28_717, 0),
        ]

    def test_other_products_are_ignored(self) -> None:
        ticks = [_tick_total("202610", "day", 5, product="MTX")]

        assert official.reconcile_volume(ticks, [], contract="TX") == []

    def test_a_spread_counts_half_its_file_quantity_and_adds_to_both_legs(self) -> None:
        """2026-10-05 day session: 41,765 + 90 + 3 = 41,858 and 333 + 90 = 423, to the lot."""
        ticks = [
            _tick_total("202610", "day", 41_765),
            _tick_total("202611", "day", 333),
            _tick_total("202610/202611", "day", 180, spread=True),
            _tick_total("202610/202612", "day", 6, spread=True),
        ]
        volume = _official_volume(
            [
                ("202610", "day", 41_858),
                ("202611", "day", 423),
                ("202612", "day", 3),
                ("202610/202611", "day", 90),
                ("202610/202612", "day", 3),
            ]
        )

        rows = official.reconcile_volume(ticks, volume, contract="TX")

        assert {(r["expiry"], r["session"]): r["difference"] for r in rows} == {
            ("202610", "day"): 0,
            ("202611", "day"): 0,
            ("202612", "day"): 0,
            ("202610/202611", "day"): 0,
            ("202610/202612", "day"): 0,
        }

    def test_a_session_with_no_official_volume_has_no_ratio(self) -> None:
        rows = official.reconcile_volume([_tick_total("202611", "day", 3)], [], contract="TX")

        assert rows == [
            {"expiry": "202611", "session": "day", "ticks": 3, "official": 0, "difference": 3, "ratio": None}
        ]


class TestCli:
    def _args(self, tmp_path: Path, **overrides: object):
        import argparse

        parser = argparse.ArgumentParser()
        official.add_arguments(parser)
        namespace = parser.parse_args(["parse", "--root", str(tmp_path)])
        for name, value in overrides.items():
            setattr(namespace, name, value)
        return namespace

    def test_parse_without_a_dataset_is_a_bad_argument(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        assert official.run_cli(self._args(tmp_path)) == 2
        assert "BAD ARGUMENTS" in capsys.readouterr().out

    def test_parse_reports_the_files_it_wrote(self, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
        _tick_zip(tmp_path / "taifex/fut_ticks/2026-10-05.zip", FUT_TICK_CSV)
        _manifest_with(tmp_path, "taifex/fut_ticks/2026-10-05.zip")

        assert official.run_cli(self._args(tmp_path, dataset="fut_ticks")) == 0

        assert json.loads(capsys.readouterr().out)["parsed"] == ["parsed/fut_ticks/2026-10-05.parquet"]

    def test_reconcile_prints_the_official_file_hash_and_the_rows(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        _tick_zip(tmp_path / "taifex/fut_ticks/2026-10-05.zip", FUT_TICK_CSV)
        _manifest_with(tmp_path, "taifex/fut_ticks/2026-10-05.zip")
        official.parse_stored(tmp_path, "fut_ticks")
        snapshot = tmp_path / "snapshot.json"
        snapshot.write_bytes(FUT_JSON)

        code = official.run_cli(
            self._args(tmp_path, action="reconcile", day="2026-10-05", official_file=str(snapshot), contract="TX")
        )

        out = json.loads(capsys.readouterr().out)
        assert code == 0
        assert out["official_sha256"] == official.sha256_hex(FUT_JSON)
        assert {row["session"] for row in out["rows"]} == {"day", "night"}

    def test_reconcile_without_parsed_ticks_says_what_to_run_first(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        snapshot = tmp_path / "snapshot.json"
        snapshot.write_bytes(FUT_JSON)

        code = official.run_cli(self._args(tmp_path, action="reconcile", day="2026-10-05", official_file=str(snapshot)))

        assert code == 2
        assert "parse --dataset fut_ticks" in capsys.readouterr().out
