"""Offline tests for the integrity gates over parsed official tick files.

Every fixture goes through the module's own producers (``write_parquet``, ``summarize_ticks``),
so a gate is tested against the files the parser really writes. No test touches the network.
    uv run pytest tests/research/test_official_checks.py --no-cov -q
"""

from __future__ import annotations

import argparse
import json
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

import pytest

from research.data_pipeline import official, official_checks

DAY = "2026-10-05"
DAY_SECOND = 9 * 3600


def _fut(product: str = "TX", **overrides: Any) -> dict[str, Any]:
    row: dict[str, Any] = {
        "date": DAY,
        "product": product,
        "expiry": "202610",
        "second": DAY_SECOND,
        "price": 22000.0,
        "contracts": 1,
        "near_price": None,
        "far_price": None,
        "auction": False,
        "spread": False,
    }
    row.update(overrides)
    return row


def _opt(**overrides: Any) -> dict[str, Any]:
    row: dict[str, Any] = {
        "date": DAY,
        "product": "TXO",
        "strike": 22000.0,
        "expiry": "202610",
        "right": "C",
        "second": DAY_SECOND,
        "price": 12.5,
        "contracts": 3,
        "auction": False,
    }
    row.update(overrides)
    return row


def _write_day(
    root: Path,
    rows: Sequence[Mapping[str, Any]],
    *,
    day: str = DAY,
    dataset: str = "fut_ticks",
    summary: Any = None,
    parquet: bool = True,
) -> None:
    folder = root / "parsed" / dataset
    folder.mkdir(parents=True, exist_ok=True)
    if parquet:
        tagged = [
            {**row, "trading_day": day, "session": official.tick_session(day, row["date"], row["second"])}
            for row in rows
        ]
        official.write_parquet(tagged, folder / f"{day}.parquet")
    if summary is not False:
        body = official.summarize_ticks(rows, day) if summary is None else summary
        text = body if isinstance(body, str) else json.dumps(body)
        (folder / f"{day}.summary.json").write_text(text, "utf-8")


def _checks(findings: Sequence[Mapping[str, Any]]) -> list[str]:
    return sorted(item["check"] for item in findings)


def _run(root: Path, **kwargs: Any) -> dict[str, Any]:
    return official_checks.check_dataset(root, kwargs.pop("dataset", "fut_ticks"), **kwargs)


class TestPriceGates:
    def test_a_clean_index_futures_day_has_no_findings(self, tmp_path: Path) -> None:
        _write_day(tmp_path, [_fut("TX"), _fut("MTX", price=22001.0, contracts=4), _fut("TMF", price=21999.0)])

        report = _run(tmp_path)

        assert report["findings"] == []
        assert report["failed"] is False
        assert report["rows_checked"] == 3
        assert report["in_scope_rows"] == 3

    def test_a_fractional_price_on_an_index_future_fails(self, tmp_path: Path) -> None:
        _write_day(tmp_path, [_fut(), _fut(price=22000.5), _fut(price=22001.0)])

        report = _run(tmp_path)

        assert [(item["check"], item["severity"], item["rows"]) for item in report["findings"]] == [
            ("non_integer_price", "fail", 1)
        ]
        assert report["failed"] is True

    @pytest.mark.parametrize("price", [float("nan"), float("inf")])
    def test_a_non_finite_price_fails_as_non_integer(self, tmp_path: Path, price: float) -> None:
        _write_day(tmp_path, [_fut(), _fut(price=price)])

        assert _checks(_run(tmp_path)["findings"]) == ["non_integer_price"]

    def test_fractional_prices_outside_the_product_scope_are_ignored(self, tmp_path: Path) -> None:
        _write_day(tmp_path, [_fut("TX"), _fut("CAF", price=22.5)])

        assert _run(tmp_path)["findings"] == []

    def test_a_widened_scope_flags_the_product_added_to_it(self, tmp_path: Path) -> None:
        _write_day(tmp_path, [_fut("TX"), _fut("CAF", price=22.5)])

        report = _run(tmp_path, products=("TX", "CAF"))

        assert [item["check"] for item in report["findings"]] == ["non_integer_price"]

    def test_spread_legs_are_not_held_to_whole_point_prices(self, tmp_path: Path) -> None:
        _write_day(tmp_path, [_fut(), _fut(expiry="202610/202611", spread=True, price=12.5)])

        assert _run(tmp_path)["findings"] == []

    @pytest.mark.parametrize("price", [0.0, -5.0])
    def test_a_price_at_or_below_zero_on_a_single_leg_fails(self, tmp_path: Path, price: float) -> None:
        _write_day(tmp_path, [_fut(), _fut(price=price)])

        report = _run(tmp_path)

        assert [(item["check"], item["rows"]) for item in report["findings"]] == [("price_not_positive", 1)]

    def test_a_negative_spread_price_is_information_not_a_failure(self, tmp_path: Path) -> None:
        spread = {"expiry": "202610/202611", "spread": True}
        _write_day(tmp_path, [_fut(), _fut(price=-7.0, **spread), _fut(price=0.0, **spread), _fut(price=3.0, **spread)])

        report = _run(tmp_path)

        assert [(item["check"], item["severity"], item["rows"]) for item in report["findings"]] == [
            ("negative_spread_price", "info", 1)
        ]
        assert report["failed"] is False

    def test_a_missing_price_on_an_index_future_fails(self, tmp_path: Path) -> None:
        _write_day(tmp_path, [_fut(), _fut(price=None)])

        report = _run(tmp_path)

        assert [(item["check"], item["rows"]) for item in report["findings"]] == [("price_missing", 1)]


class TestContractGates:
    @pytest.mark.parametrize("contracts", [0, -2])
    def test_a_single_leg_with_no_positive_contracts_fails(self, tmp_path: Path, contracts: int) -> None:
        _write_day(tmp_path, [_fut(), _fut(contracts=contracts)])

        report = _run(tmp_path)

        assert [(item["check"], item["rows"]) for item in report["findings"]] == [("contracts_not_positive", 1)]

    def test_a_spread_row_with_no_positive_contracts_fails_too(self, tmp_path: Path) -> None:
        _write_day(tmp_path, [_fut(), _fut(expiry="202610/202611", spread=True, price=10.0, contracts=0)])

        assert _checks(_run(tmp_path)["findings"]) == ["contracts_not_positive"]

    def test_missing_contracts_fail_separately_from_non_positive_ones(self, tmp_path: Path) -> None:
        _write_day(tmp_path, [_fut(), _fut("MTX", contracts=None)])

        report = _run(tmp_path)

        assert [(item["check"], item["rows"]) for item in report["findings"]] == [("contracts_missing", 1)]


class TestSummaryReconciliation:
    def test_a_summary_with_the_wrong_contract_total_fails_and_names_the_group(self, tmp_path: Path) -> None:
        rows = [_fut(contracts=2), _fut(contracts=3)]
        wrong = [{**item, "contracts": item["contracts"] + 1} for item in official.summarize_ticks(rows, DAY)]
        _write_day(tmp_path, rows, summary=wrong)

        report = _run(tmp_path)

        finding = report["findings"][0]
        assert (finding["check"], finding["severity"], finding["rows"]) == ("summary_mismatch", "fail", 1)
        assert "TX" in finding["detail"] and "202610" in finding["detail"]
        assert "contracts" in finding["detail"]

    def test_a_summary_with_the_wrong_trade_count_fails(self, tmp_path: Path) -> None:
        rows = [_fut(), _fut()]
        wrong = [{**item, "trades": item["trades"] - 1} for item in official.summarize_ticks(rows, DAY)]
        _write_day(tmp_path, rows, summary=wrong)

        report = _run(tmp_path)

        assert [item["check"] for item in report["findings"]] == ["summary_mismatch"]
        assert "trades" in report["findings"][0]["detail"]

    def test_a_group_missing_from_the_summary_fails(self, tmp_path: Path) -> None:
        rows = [_fut("TX"), _fut("MTX")]
        kept = [item for item in official.summarize_ticks(rows, DAY) if item["product"] == "TX"]
        _write_day(tmp_path, rows, summary=kept)

        report = _run(tmp_path)

        assert [(item["check"], item["rows"]) for item in report["findings"]] == [("summary_mismatch", 1)]
        assert "MTX" in report["findings"][0]["detail"]

    def test_a_group_only_the_summary_has_fails(self, tmp_path: Path) -> None:
        rows = [_fut("TX")]
        extra = [*official.summarize_ticks(rows, DAY), *official.summarize_ticks([_fut("TMF")], DAY)]
        _write_day(tmp_path, rows, summary=extra)

        report = _run(tmp_path)

        assert [(item["check"], item["rows"]) for item in report["findings"]] == [("summary_mismatch", 1)]
        assert "TMF" in report["findings"][0]["detail"]

    def test_the_detail_lists_at_most_five_groups_but_counts_them_all(self, tmp_path: Path) -> None:
        products = [f"P{index}" for index in range(8)]
        rows = [_fut(product) for product in products]
        _write_day(tmp_path, rows, summary=[])

        finding = _run(tmp_path, products=tuple(products))["findings"][0]

        assert finding["rows"] == 8
        assert "P4" in finding["detail"]
        assert "P5" not in finding["detail"]
        assert "+3 more" in finding["detail"]

    def test_a_missing_summary_file_fails(self, tmp_path: Path) -> None:
        _write_day(tmp_path, [_fut()], summary=False)

        report = _run(tmp_path)

        assert [item["check"] for item in report["findings"]] == ["summary_missing"]
        assert report["failed"] is True

    @pytest.mark.parametrize("body", ["{not json", '{"product": "TX"}', '[{"product": "TX"}]', "[1, 2]"])
    def test_an_unreadable_summary_fails(self, tmp_path: Path, body: str) -> None:
        _write_day(tmp_path, [_fut()], summary=body)

        assert [item["check"] for item in _run(tmp_path)["findings"]] == ["summary_unreadable"]

    def test_a_summary_with_no_parquet_fails_unless_the_day_was_empty(self, tmp_path: Path) -> None:
        _write_day(tmp_path, [_fut()], parquet=False)

        report = _run(tmp_path)

        assert [item["check"] for item in report["findings"]] == ["parquet_missing"]

    def test_an_empty_day_is_information_only(self, tmp_path: Path) -> None:
        _write_day(tmp_path, [], parquet=False, summary=[])

        report = _run(tmp_path)

        assert [(item["check"], item["severity"]) for item in report["findings"]] == [("empty_day", "info")]
        assert report["failed"] is False


class TestScopeAndSchema:
    def test_a_day_with_nothing_in_scope_is_flagged_instead_of_passing_silently(self, tmp_path: Path) -> None:
        _write_day(tmp_path, [_fut("TX")])

        report = _run(tmp_path, products=("TXX",))

        assert [(item["check"], item["severity"]) for item in report["findings"]] == [("no_in_scope_rows", "info")]
        assert report["in_scope_rows"] == 0

    def test_a_missing_column_fails_instead_of_raising(self, tmp_path: Path) -> None:
        import pyarrow as pa
        import pyarrow.parquet as pq

        folder = tmp_path / "parsed" / "fut_ticks"
        folder.mkdir(parents=True)
        pq.write_table(pa.table({"product": ["TX"], "price": [1.0]}), folder / f"{DAY}.parquet")
        (folder / f"{DAY}.summary.json").write_text("[]", "utf-8")

        report = _run(tmp_path)

        assert [item["check"] for item in report["findings"]] == ["schema"]
        assert "contracts" in report["findings"][0]["detail"]

    def test_each_day_is_reported_under_its_own_date(self, tmp_path: Path) -> None:
        _write_day(tmp_path, [_fut()], day="2026-10-02")
        _write_day(tmp_path, [_fut(price=22000.5, date="2026-10-05")], day="2026-10-05")

        report = _run(tmp_path)

        assert report["days_checked"] == 2
        assert [(item["day"], item["check"]) for item in report["findings"]] == [("2026-10-05", "non_integer_price")]

    def test_an_unfinished_partial_file_is_not_a_day(self, tmp_path: Path) -> None:
        _write_day(tmp_path, [_fut()])
        (tmp_path / "parsed" / "fut_ticks" / "2026-10-06.parquet.partial").write_bytes(b"half a file")

        report = _run(tmp_path)

        assert (report["days_checked"], report["findings"]) == (1, [])

    def test_the_day_option_limits_the_run_to_that_day(self, tmp_path: Path) -> None:
        _write_day(tmp_path, [_fut()], day="2026-10-02")
        _write_day(tmp_path, [_fut(price=22000.5)], day="2026-10-05")

        report = _run(tmp_path, day="2026-10-02")

        assert report["days_checked"] == 1
        assert report["findings"] == []

    def test_an_unknown_day_is_a_value_error(self, tmp_path: Path) -> None:
        _write_day(tmp_path, [_fut()])

        with pytest.raises(ValueError, match="2026-01-01"):
            _run(tmp_path, day="2026-01-01")

    def test_a_directory_with_no_parsed_files_is_a_value_error_that_names_the_fix(self, tmp_path: Path) -> None:
        with pytest.raises(ValueError, match="parse --dataset fut_ticks"):
            _run(tmp_path)

    def test_an_empty_product_list_is_a_value_error_not_a_silent_pass(self, tmp_path: Path) -> None:
        _write_day(tmp_path, [_fut()])

        with pytest.raises(ValueError, match="no products given"):
            _run(tmp_path, products=())

    def test_an_unknown_dataset_is_a_value_error_even_when_files_exist_for_it(self, tmp_path: Path) -> None:
        _write_day(tmp_path, [_fut()], dataset="openapi")

        with pytest.raises(ValueError, match="unknown dataset 'openapi'"):
            official_checks.check_dataset(tmp_path, "openapi")


class TestOptionsDataset:
    def test_fractional_option_premiums_are_not_a_finding(self, tmp_path: Path) -> None:
        _write_day(tmp_path, [_opt(price=0.5), _opt(price=12.5, right="P")], dataset="opt_ticks")

        report = _run(tmp_path, dataset="opt_ticks")

        assert report["findings"] == []
        assert report["rows_checked"] == 2

    def test_an_option_summary_mismatch_still_fails(self, tmp_path: Path) -> None:
        rows = [_opt(contracts=3)]
        wrong = [{**item, "contracts": 99} for item in official.summarize_ticks(rows, DAY)]
        _write_day(tmp_path, rows, dataset="opt_ticks", summary=wrong)

        report = _run(tmp_path, dataset="opt_ticks")

        assert [item["check"] for item in report["findings"]] == ["summary_mismatch"]


class TestCheckAction:
    def _args(self, root: Path, *extra: str) -> argparse.Namespace:
        parser = argparse.ArgumentParser()
        official.add_arguments(parser)
        return parser.parse_args(["check", "--root", str(root), *extra])

    def test_check_exits_zero_and_prints_the_report_for_a_clean_day(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        _write_day(tmp_path, [_fut()])

        code = official.run_cli(self._args(tmp_path, "--dataset", "fut_ticks"))

        out = json.loads(capsys.readouterr().out)
        assert code == 0
        assert (out["failed"], out["days_checked"], out["rows_checked"]) == (False, 1, 1)

    def test_check_exits_one_when_a_gate_fails(self, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
        _write_day(tmp_path, [_fut(price=22000.5)])

        code = official.run_cli(self._args(tmp_path, "--dataset", "fut_ticks"))

        out = json.loads(capsys.readouterr().out)
        assert code == 1
        assert [item["check"] for item in out["findings"]] == ["non_integer_price"]

    def test_check_without_a_dataset_is_a_bad_argument(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        code = official.run_cli(self._args(tmp_path))

        assert code == 2
        assert "BAD ARGUMENTS" in capsys.readouterr().out

    def test_check_on_an_empty_root_says_what_to_run_first(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        code = official.run_cli(self._args(tmp_path, "--dataset", "fut_ticks"))

        assert code == 2
        assert "parse --dataset fut_ticks" in capsys.readouterr().out

    def test_the_products_option_sets_the_scope(self, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
        _write_day(tmp_path, [_fut("TX"), _fut("CAF", price=22.5)])

        default_code = official.run_cli(self._args(tmp_path, "--dataset", "fut_ticks"))
        capsys.readouterr()
        wide_code = official.run_cli(self._args(tmp_path, "--dataset", "fut_ticks", "--products", "TX, CAF"))

        assert (default_code, wide_code) == (0, 1)
        assert json.loads(capsys.readouterr().out)["products"] == ["TX", "CAF"]

    def test_the_day_option_limits_the_gates_to_one_parsed_day(self, tmp_path: Path) -> None:
        _write_day(tmp_path, [_fut()], day="2026-10-02")
        _write_day(tmp_path, [_fut(price=22000.5)], day="2026-10-05")

        clean = official.run_cli(self._args(tmp_path, "--dataset", "fut_ticks", "--day", "2026-10-02"))
        dirty = official.run_cli(self._args(tmp_path, "--dataset", "fut_ticks", "--day", "2026-10-05"))

        assert (clean, dirty) == (0, 1)

    def test_check_with_only_separators_in_products_is_a_bad_argument(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        _write_day(tmp_path, [_fut()])

        code = official.run_cli(self._args(tmp_path, "--dataset", "fut_ticks", "--products", " , "))

        assert code == 2
        assert "no products given" in capsys.readouterr().out

    def test_check_leaves_the_data_root_untouched(self, tmp_path: Path) -> None:
        _write_day(tmp_path, [_fut()])
        before = sorted(path.relative_to(tmp_path).as_posix() for path in tmp_path.rglob("*"))
        sizes = {path: path.stat().st_size for path in tmp_path.rglob("*") if path.is_file()}

        official.run_cli(self._args(tmp_path, "--dataset", "fut_ticks"))

        assert sorted(path.relative_to(tmp_path).as_posix() for path in tmp_path.rglob("*")) == before
        assert {path: path.stat().st_size for path in tmp_path.rglob("*") if path.is_file()} == sizes
