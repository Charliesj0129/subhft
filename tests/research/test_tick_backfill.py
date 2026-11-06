"""Offline tests for staging Shioaji historical ticks (research/data_pipeline/tick_backfill.py).

The broker is a scripted fake and time is a fake clock: nothing here logs in, sleeps or touches a
network or a ClickHouse. What is pinned is what the existing insert-straight-into-ClickHouse script
gets wrong: exact price scaling, exact nanosecond timestamps, one definition per column, no
overwrite, and a run that stops instead of hammering a source that is refusing it.
    uv run pytest tests/research/test_tick_backfill.py --no-cov -q
"""

from __future__ import annotations

import ast
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pyarrow.parquet as pq
import pytest

from research.data_pipeline import tick_backfill as tb
from research.data_pipeline.tick_backfill import BackfillRequest, BadTicks, Blocked

DAY = "2026-04-06"
EIGHT_HOURS_NS = 8 * 3600 * 1_000_000_000
TS0 = 1_775_466_000_123_456_789  # beyond 2**53: a float round trip would change it


def _request(symbol: str = "TXFD6", day: str = DAY) -> BackfillRequest:
    return BackfillRequest(symbol, "FUT", "future", day)


def _ticks(n: int = 3, **extra: Any) -> SimpleNamespace:
    return SimpleNamespace(
        ts=[TS0 + i * 1_000_000 for i in range(n)],
        close=[17500.0 + i for i in range(n)],
        volume=[1 + i for i in range(n)],
        **extra,
    )


class FakeTime:
    def __init__(self) -> None:
        self.now = 100.0
        self.slept: list[float] = []

    def clock(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.slept.append(seconds)
        self.now += seconds


class ScriptedSource:
    def __init__(self, answers: dict[tuple[str, str], Any], *, time: FakeTime | None = None, cost_s: float = 0.0):
        self.answers = answers
        self.calls: list[tuple[str, str]] = []
        self.time = time
        self.cost_s = cost_s

    def fetch(self, request: BackfillRequest) -> Any:
        self.calls.append((request.symbol, request.day))
        if self.time is not None:
            self.time.now += self.cost_s
        answer = self.answers[(request.symbol, request.day)]
        if isinstance(answer, BaseException):
            raise answer
        return answer


def _run(source: ScriptedSource, requests: list[BackfillRequest], out_dir: Path, time: FakeTime | None = None, **kw):
    time = time or FakeTime()
    return tb.run_backfill(source, requests, out_dir, clock=time.clock, sleep=time.sleep, **kw)


class TestScalePrice:
    @pytest.mark.parametrize(
        ("price", "expected"),
        [
            (17500.0, 17_500_000_000),
            (28.35, 28_350_000),
            (0.0001, 100),
            (-0.5, -500_000),
            (99999.9999, 99_999_999_900),
            (2.01, 2_010_000),
        ],
    )
    def test_scale_price_is_exact_for_prices_on_a_decimal_grid(self, price: float, expected: int) -> None:
        assert tb.scale_price(price) == expected

    def test_scale_price_does_not_truncate_where_multiplying_a_float_does(self) -> None:
        # The existing script uses int(price * 1_000_000); for this stock price it is one unit short.
        assert int(2.01 * 1_000_000) == 2_009_999
        assert tb.scale_price(2.01) == 2_010_000

    def test_scale_price_rounds_half_even_on_the_decimal_form(self) -> None:
        assert tb.scale_price(0.0000005) == 0
        assert tb.scale_price(0.0000015) == 2

    @pytest.mark.parametrize("bad", [None, True, float("nan"), float("inf"), float("-inf"), "abc", []])
    def test_scale_price_rejects_what_is_not_a_finite_number(self, bad: Any) -> None:
        with pytest.raises(BadTicks):
            tb.scale_price(bad)


class TestConvertTicks:
    def test_convert_keeps_the_response_and_derives_the_utc_stamp(self) -> None:
        columns = tb.convert_ticks(_request(), _ticks(3, tick_type=[1, 2, 0]))

        assert columns["symbol"] == ["TXFD6"] * 3
        assert columns["exchange"] == ["FUT"] * 3
        assert columns["instrument_type"] == ["future"] * 3
        assert columns["source"] == [tb.SOURCE_LABEL] * 3
        assert columns["seq"] == [0, 1, 2]
        assert columns["price_scaled"] == [17_500_000_000, 17_501_000_000, 17_502_000_000]
        assert columns["volume"] == [1, 2, 3]
        assert columns["tick_type"] == [1, 2, 0]
        assert columns["exch_ts"] == [value - EIGHT_HOURS_NS for value in columns["ts_local_ns"]]

    def test_convert_keeps_nanosecond_timestamps_exact(self) -> None:
        columns = tb.convert_ticks(_request(), _ticks(1))

        assert columns["ts_local_ns"] == [TS0]
        assert columns["exch_ts"] == [TS0 - EIGHT_HOURS_NS]
        assert float(TS0) != TS0  # the value really is beyond what a float holds

    def test_convert_writes_neither_a_trade_direction_nor_an_ingest_stamp(self) -> None:
        # The recorded rows carry the EMO classifier's trade_direction and a real receive time; a
        # backfilled row has neither, so it must not fill those columns with a different meaning.
        columns = tb.convert_ticks(_request(), _ticks(2, tick_type=[1, 2]))

        assert "trade_direction" not in columns
        assert "ingest_ts" not in columns

    def test_convert_leaves_absent_optional_columns_null(self) -> None:
        columns = tb.convert_ticks(_request(), _ticks(2))

        for name in ("tick_type", "bid_price_scaled", "ask_price_scaled", "bid_volume", "ask_volume"):
            assert columns[name] == [None, None]

    def test_convert_scales_the_quoted_prices_and_keeps_missing_entries_null(self) -> None:
        ticks = _ticks(2, bid_price=[17499.0, None], ask_price=[17501.0, 17502.0], bid_volume=[3, 4], ask_volume=[5, 6])

        columns = tb.convert_ticks(_request(), ticks)

        assert columns["bid_price_scaled"] == [17_499_000_000, None]
        assert columns["ask_price_scaled"] == [17_501_000_000, 17_502_000_000]
        assert columns["bid_volume"] == [3, 4]
        assert columns["ask_volume"] == [5, 6]

    def test_convert_accepts_a_whole_valued_float_volume_and_rejects_a_fractional_one(self) -> None:
        assert tb.convert_ticks(_request(), SimpleNamespace(ts=[TS0], close=[1.0], volume=[2.0]))["volume"] == [2]
        with pytest.raises(BadTicks, match="volume is not a whole number"):
            tb.convert_ticks(_request(), SimpleNamespace(ts=[TS0], close=[1.0], volume=[1.5]))

    def test_convert_rejects_a_boolean_where_a_count_belongs(self) -> None:
        with pytest.raises(BadTicks, match="volume is not a number"):
            tb.convert_ticks(_request(), SimpleNamespace(ts=[TS0], close=[1.0], volume=[True]))

    def test_convert_rejects_arrays_of_different_lengths(self) -> None:
        ticks = SimpleNamespace(ts=[TS0, TS0 + 1], close=[1.0], volume=[1, 1])

        with pytest.raises(BadTicks, match=r"close has 1 entries, ts has 2"):
            tb.convert_ticks(_request(), ticks)

    @pytest.mark.parametrize(
        "ticks",
        [
            SimpleNamespace(close=[1.0], volume=[1]),
            SimpleNamespace(ts=[TS0], volume=[1]),
            SimpleNamespace(ts=[TS0], close=[1.0]),
        ],
        ids=["no ts", "no close", "no volume"],
    )
    def test_convert_rejects_a_response_missing_a_required_array(self, ticks: SimpleNamespace) -> None:
        with pytest.raises(BadTicks):
            tb.convert_ticks(_request(), ticks)

    def test_convert_rejects_a_non_finite_close(self) -> None:
        with pytest.raises(BadTicks, match="not finite"):
            tb.convert_ticks(_request(), SimpleNamespace(ts=[TS0], close=[float("nan")], volume=[1]))


class TestStagedPath:
    def test_staged_path_is_day_then_symbol(self, tmp_path: Path) -> None:
        assert tb.staged_path(tmp_path, _request()) == tmp_path / DAY / "TXFD6.parquet"

    @pytest.mark.parametrize("day", ["20260406", "2026-4-6", "../x", "2026-02-30", ""])
    def test_staged_path_refuses_a_day_that_is_not_an_iso_date(self, tmp_path: Path, day: str) -> None:
        with pytest.raises(ValueError, match="day must be YYYY-MM-DD"):
            tb.staged_path(tmp_path, _request(day=day))

    @pytest.mark.parametrize("symbol", ["../x", "a/b", "", ".hidden", "..", "a b"])
    def test_staged_path_refuses_a_symbol_that_is_not_a_plain_name(self, tmp_path: Path, symbol: str) -> None:
        with pytest.raises(ValueError, match="plain name"):
            tb.staged_path(tmp_path, _request(symbol=symbol))

    def test_plan_marks_the_requests_already_staged_in_day_major_order(self, tmp_path: Path) -> None:
        tb.write_staged(
            tb.staged_path(tmp_path, _request("TMFD6", "2026-04-07")), tb.convert_ticks(_request(), _ticks(1))
        )

        plan = tb.plan_requests(
            tmp_path, ["2026-04-06", "2026-04-07"], ["TXFD6", "TMFD6"], exchange="FUT", instrument_type="future"
        )

        assert [(r.day, r.symbol, staged) for r, staged in plan] == [
            ("2026-04-06", "TXFD6", False),
            ("2026-04-06", "TMFD6", False),
            ("2026-04-07", "TXFD6", False),
            ("2026-04-07", "TMFD6", True),
        ]


class TestRun:
    def test_run_stages_one_file_with_typed_columns_and_provenance(self, tmp_path: Path) -> None:
        source = ScriptedSource({("TXFD6", DAY): _ticks(3, tick_type=[1, 2, 0])})

        report = _run(source, [_request()], tmp_path)

        path = tmp_path / DAY / "TXFD6.parquet"
        table = pq.read_table(path)
        assert [(o.status, o.rows) for o in report.outcomes] == [("staged", 3)]
        assert report.failed is False
        assert table.num_rows == 3
        assert str(table.schema.field("exch_ts").type) == "int64"
        assert str(table.schema.field("price_scaled").type) == "int64"
        assert table.column("ts_local_ns").to_pylist()[0] == TS0
        metadata = {key.decode(): value.decode() for key, value in table.schema.metadata.items()}
        assert metadata["source"] == tb.SOURCE_LABEL
        assert metadata["price_scale"] == "1000000"
        assert sorted(item.name for item in path.parent.iterdir()) == ["TXFD6.parquet"]

    def test_a_failed_write_leaves_nothing_under_the_final_name_so_the_next_run_asks_again(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def write_then_fail(table: Any, where: Any) -> None:
            Path(where).write_bytes(b"half a file")
            raise OSError("disk full")

        monkeypatch.setattr(pq, "write_table", write_then_fail)
        source = ScriptedSource({("TXFD6", DAY): _ticks(3)})

        report = _run(source, [_request()], tmp_path)

        assert [o.status for o in report.outcomes] == ["error"]
        assert not (tmp_path / DAY / "TXFD6.parquet").exists()
        monkeypatch.undo()
        again = _run(ScriptedSource({("TXFD6", DAY): _ticks(3)}), [_request()], tmp_path)
        assert [o.status for o in again.outcomes] == ["staged"]

    def test_staged_files_share_one_schema_with_or_without_the_quote_columns(self, tmp_path: Path) -> None:
        with_quotes = _ticks(
            2, bid_price=[17499.0, 17500.0], ask_price=[17501.0, 17502.0], bid_volume=[1, 2], ask_volume=[3, 4]
        )
        answers = {("A1", DAY): with_quotes, ("B1", DAY): _ticks(2)}

        _run(ScriptedSource(answers), [_request("A1"), _request("B1")], tmp_path)

        schemas = [pq.read_schema(tmp_path / DAY / f"{name}.parquet") for name in ("A1", "B1")]
        assert schemas[0].names == list(tb.STAGED_COLUMNS)
        assert [str(f.type) for f in schemas[0]] == [str(f.type) for f in schemas[1]]
        assert str(schemas[1].field("bid_price_scaled").type) == "int64"

    def test_write_staged_refuses_columns_that_are_not_the_staged_set(self, tmp_path: Path) -> None:
        columns = tb.convert_ticks(_request(), _ticks(1))
        columns["trade_direction"] = [0]

        with pytest.raises(ValueError, match="trade_direction"):
            tb.write_staged(tmp_path / "x.parquet", columns)
        assert list(tmp_path.iterdir()) == []

    def test_run_never_overwrites_a_staged_file_and_does_not_ask_for_it_again(self, tmp_path: Path) -> None:
        path = tb.staged_path(tmp_path, _request())
        path.parent.mkdir(parents=True)
        path.write_bytes(b"earlier answer")
        source = ScriptedSource({("TXFD6", DAY): _ticks(3)})

        report = _run(source, [_request()], tmp_path)

        assert [o.status for o in report.outcomes] == ["skipped_existing"]
        assert source.calls == []
        assert path.read_bytes() == b"earlier answer"

    @pytest.mark.parametrize("answer", [None, SimpleNamespace(ts=[], close=[], volume=[])], ids=["none", "no rows"])
    def test_run_stages_nothing_for_an_empty_answer(self, tmp_path: Path, answer: Any) -> None:
        report = _run(ScriptedSource({("TXFD6", DAY): answer}), [_request()], tmp_path)

        assert [o.status for o in report.outcomes] == ["empty"]
        assert report.failed is False
        assert list(tmp_path.iterdir()) == []

    def test_run_records_an_error_and_goes_on_to_the_next_request(self, tmp_path: Path) -> None:
        answers = {
            ("A1", DAY): _ticks(2),
            ("B1", DAY): RuntimeError("boom"),
            ("C1", DAY): _ticks(2),
        }
        requests = [_request("A1"), _request("B1"), _request("C1")]

        report = _run(ScriptedSource(answers), requests, tmp_path)

        assert [o.status for o in report.outcomes] == ["staged", "error", "staged"]
        assert report.outcomes[1].detail == "RuntimeError: boom"
        assert report.counts() == {"staged": 2, "error": 1}
        assert report.failed is True
        assert report.stopped_by is None

    def test_run_reports_a_response_that_cannot_be_converted_as_an_error(self, tmp_path: Path) -> None:
        bad = SimpleNamespace(ts=[TS0], close=[float("nan")], volume=[1])

        report = _run(ScriptedSource({("TXFD6", DAY): bad}), [_request()], tmp_path)

        assert report.outcomes[0].status == "error"
        assert report.outcomes[0].detail.startswith("BadTicks")
        assert list(tmp_path.iterdir()) == []

    def test_run_stops_after_the_allowed_run_of_consecutive_errors(self, tmp_path: Path) -> None:
        names = ["A1", "B1", "C1", "D1"]
        source = ScriptedSource({(name, DAY): RuntimeError("down") for name in names})

        report = _run(source, [_request(name) for name in names], tmp_path, max_consecutive_errors=3)

        assert source.calls == [("A1", DAY), ("B1", DAY), ("C1", DAY)]
        assert report.stopped_by == "consecutive_errors"
        assert report.failed is True

    def test_run_counts_errors_that_are_not_in_a_row_separately(self, tmp_path: Path) -> None:
        err = RuntimeError("x")
        pattern = [err, err, _ticks(1), err, err, None, err, err]
        names = [f"S{i}" for i in range(len(pattern))]
        source = ScriptedSource({(name, DAY): answer for name, answer in zip(names, pattern, strict=True)})

        report = _run(source, [_request(name) for name in names], tmp_path, max_consecutive_errors=3)

        assert report.stopped_by is None
        assert len(source.calls) == len(pattern)

    def test_run_stops_at_a_block_and_leaves_the_rest_unrequested(self, tmp_path: Path) -> None:
        answers = {("A1", DAY): _ticks(1), ("B1", DAY): Blocked("429"), ("C1", DAY): _ticks(1)}

        source = ScriptedSource(answers)
        report = _run(source, [_request("A1"), _request("B1"), _request("C1")], tmp_path)

        assert [o.status for o in report.outcomes] == ["staged", "blocked"]
        assert report.stopped_by == "blocked"
        assert report.failed is True
        assert source.calls == [("A1", DAY), ("B1", DAY)]
        assert not (tmp_path / DAY / "C1.parquet").exists()

    def test_run_refuses_a_bad_request_before_making_any_call(self, tmp_path: Path) -> None:
        source = ScriptedSource({("A1", DAY): _ticks(1)})

        with pytest.raises(ValueError, match="plain name"):
            _run(source, [_request("A1"), _request("../escape")], tmp_path)

        assert source.calls == []
        assert list(tmp_path.iterdir()) == []

    def test_run_waits_the_interval_between_calls_but_not_before_the_first(self, tmp_path: Path) -> None:
        time = FakeTime()
        names = ["A1", "B1", "C1"]
        source = ScriptedSource({(n, DAY): _ticks(1) for n in names}, time=time)

        _run(source, [_request(n) for n in names], tmp_path, time=time, min_interval_s=2.0)

        assert time.slept == [2.0, 2.0]

    def test_run_does_not_wait_when_the_call_itself_took_longer_than_the_interval(self, tmp_path: Path) -> None:
        time = FakeTime()
        names = ["A1", "B1"]
        source = ScriptedSource({(n, DAY): _ticks(1) for n in names}, time=time, cost_s=3.0)

        _run(source, [_request(n) for n in names], tmp_path, time=time, min_interval_s=2.0)

        assert time.slept == []

    def test_run_does_not_count_a_skipped_request_against_the_interval(self, tmp_path: Path) -> None:
        time = FakeTime()
        tb.write_staged(tb.staged_path(tmp_path, _request("B1")), tb.convert_ticks(_request("B1"), _ticks(1)))
        names = ["A1", "B1", "C1"]
        source = ScriptedSource({(n, DAY): _ticks(1) for n in names}, time=time)

        _run(source, [_request(n) for n in names], tmp_path, time=time, min_interval_s=2.0)

        assert source.calls == [("A1", DAY), ("C1", DAY)]
        assert time.slept == [2.0]

    def test_a_clean_run_of_staged_empty_and_skipped_requests_is_not_a_failure(self, tmp_path: Path) -> None:
        tb.write_staged(tb.staged_path(tmp_path, _request("B1")), tb.convert_ticks(_request("B1"), _ticks(1)))
        answers = {("A1", DAY): _ticks(1), ("C1", DAY): None}

        report = _run(ScriptedSource(answers), [_request(n) for n in ("A1", "B1", "C1")], tmp_path)

        assert report.counts() == {"staged": 1, "skipped_existing": 1, "empty": 1}
        assert report.failed is False


class TestBoundary:
    def test_the_module_imports_no_broker_sdk_client_or_network_library(self) -> None:
        source = Path(tb.__file__).read_text(encoding="utf-8")
        imported: set[str] = set()
        for node in ast.walk(ast.parse(source)):
            if isinstance(node, ast.Import):
                imported.update(alias.name.split(".")[0] for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                imported.add(node.module.split(".")[0])

        forbidden = {"shioaji", "clickhouse_connect", "clickhouse_driver", "urllib", "requests", "httpx", "socket"}
        assert imported.isdisjoint(forbidden)
        assert "pyarrow" in imported  # the check above is looking at the right file
