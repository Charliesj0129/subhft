"""Duplicate-delivery removal: multiplicity estimate, ceil rule, fallbacks, exporter wiring."""

from __future__ import annotations

import json
from pathlib import Path

from research.data_pipeline import _write_day_outputs, dedup, rows_to_l2_and_ticks

NS = 1_000_000_000
T0 = (1_780_000_000 // 86400) * 86400 * NS  # a UTC midnight, so hour and day groups are predictable


def _keys(count: int, copies: int, *, start_s: int = 0, step_ns: int = NS) -> list[tuple[int, int, int]]:
    """``count`` distinct keys, each delivered ``copies`` times."""
    out: list[tuple[int, int, int]] = []
    for index in range(count):
        out.extend([(T0 + start_s * NS + index * step_ns, 21_000_000_000 + index, 1)] * copies)
    return out


class TestMultiplicityEstimate:
    def test_the_most_common_copy_count_is_the_multiplicity(self) -> None:
        assert dedup.estimate_multiplicity([2, 2, 2, 4, 6]) == 2

    def test_a_tie_goes_to_the_smaller_multiplicity(self) -> None:
        assert dedup.estimate_multiplicity([1, 1, 2, 2]) == 1

    def test_the_multiplicity_is_capped(self) -> None:
        assert dedup.estimate_multiplicity([30] * 10) == dedup.MAX_MULTIPLICITY

    def test_nothing_to_estimate_from_means_no_duplication(self) -> None:
        assert dedup.estimate_multiplicity([]) == 1


class TestKeepCounts:
    def test_a_clean_stretch_loses_nothing(self) -> None:
        keys = _keys(50, 1)

        keep, stats = dedup.keep_counts(keys)

        assert sum(keep.values()) == 50
        assert stats.removed == 0

    def test_a_pair_delivered_twice_keeps_one_row(self) -> None:
        keys = _keys(50, 2)

        keep, stats = dedup.keep_counts(keys)

        assert sum(keep.values()) == 50
        assert stats.removed == 50

    def test_four_copies_keep_one_row(self) -> None:
        keep, _ = dedup.keep_counts(_keys(50, 4))

        assert set(keep.values()) == {1}

    def test_two_real_trades_at_one_key_keep_two_rows(self) -> None:
        """c == 2m means two events: collapsing every identical key to one row loses this volume."""
        keys = _keys(49, 2) + [(T0 + 99 * NS, 21_000_000_500, 1)] * 4

        keep, _ = dedup.keep_counts(keys)

        assert keep[(T0 + 99 * NS, 21_000_000_500, 1)] == 2
        assert sum(keep.values()) == 49 + 2

    def test_a_stray_single_copy_is_still_one_event(self) -> None:
        keys = _keys(49, 2) + [(T0 + 99 * NS, 21_000_000_500, 1)]

        keep, _ = dedup.keep_counts(keys)

        assert keep[(T0 + 99 * NS, 21_000_000_500, 1)] == 1

    def test_buckets_estimate_their_own_multiplicity(self) -> None:
        """A step in the delivery rate (2x then 1x) must not leak across the boundary."""
        keys = _keys(40, 2, start_s=0) + _keys(40, 1, start_s=600)

        keep, stats = dedup.keep_counts(keys, bucket_seconds=300)

        assert sum(keep.values()) == 40 + 40
        assert stats.multiplicity_keys == {2: 40, 1: 40}

    def test_a_thin_bucket_uses_its_hour_then_its_day(self) -> None:
        busy = _keys(60, 2, start_s=0)
        thin = _keys(3, 2, start_s=1200)  # three keys: too few to estimate from
        far = _keys(3, 2, start_s=5 * 3600)  # a different hour, also thin

        keep, stats = dedup.keep_counts(busy + thin + far, bucket_seconds=300)

        assert all(count == 1 for count in keep.values())
        assert stats.keys_by_level["hour"] == 3
        assert stats.keys_by_level["day"] == 3

    def test_with_too_little_data_anywhere_nothing_is_removed(self) -> None:
        keys = _keys(5, 2)

        keep, stats = dedup.keep_counts(keys)

        assert sum(keep.values()) == 10
        assert stats.keys_by_level == {"none": 5}

    def test_the_input_order_does_not_change_the_result(self) -> None:
        keys = _keys(30, 2) + _keys(30, 1, start_s=900)

        forward, _ = dedup.keep_counts(keys)
        backward, _ = dedup.keep_counts(list(reversed(keys)))

        assert forward == backward


def _tick(ts: int, price: int = 21_000_000_000, volume: int = 1, local: int = 0) -> tuple:
    return ("Tick", ts, local or ts, [], [], [], [], price, volume)


class TestTickRows:
    def test_order_and_non_tick_rows_are_preserved_and_the_first_copies_survive(self) -> None:
        book = ("BidAsk", T0, T0, [20_999_000_000], [21_000_000_000], [3], [3], 0, 0)
        rows: list[tuple] = [book]
        for index in range(30):
            ts = T0 + (index + 1) * NS
            rows += [_tick(ts, local=ts + 1), _tick(ts, local=ts + 2)]

        kept, stats = dedup.dedup_tick_rows(rows)

        assert kept[0] == book
        assert [row[2] for row in kept[1:]] == [T0 + (index + 1) * NS + 1 for index in range(30)]
        assert stats.rows_in == 60 and stats.rows_out == 30 and stats.removed == 30

    def test_the_report_payload_is_json_serialisable(self) -> None:
        _, stats = dedup.dedup_tick_rows([_tick(T0 + index * NS) for index in range(30)])

        payload = json.loads(json.dumps(stats.to_payload()))

        assert payload["rule"] == dedup.RULE_ID and payload["removed"] == 0


class TestExporter:
    def _doubled_day(self) -> list[tuple]:
        rows: list[tuple] = [("BidAsk", T0, T0, [20_999_000_000], [21_000_000_000], [3], [4], 0, 0)]
        for index in range(30):
            ts = T0 + (index + 1) * NS
            rows += [_tick(ts, 21_000_000_000 + index * 1_000_000), _tick(ts, 21_000_000_000 + index * 1_000_000)]
        return rows

    def test_a_doubled_tick_stream_exports_each_trade_once(self) -> None:
        report: dict = {}

        _, ticks, removed = rows_to_l2_and_ticks(self._doubled_day(), dedup_report=report)

        assert len(ticks) == 30
        assert removed == 30
        assert report["rule"] == dedup.RULE_ID and report["tick"]["removed"] == 30

    def test_content_dedup_can_be_switched_off_and_says_so(self) -> None:
        report: dict = {}

        _, ticks, _ = rows_to_l2_and_ticks(self._doubled_day(), content_dedup=False, dedup_report=report)

        assert len(ticks) == 60
        assert report["rule"] == "window_only"

    def test_interleaved_copies_of_two_snapshots_at_one_exchange_time_keep_two(self) -> None:
        a = ("BidAsk", T0, T0, [100_000_000], [101_000_000], [3], [4], 0, 0)
        b = ("BidAsk", T0, T0 + 1, [100_000_000], [101_000_000], [5], [4], 0, 0)
        rows = [a, b, (*a[:2], T0 + 2, *a[3:]), (*b[:2], T0 + 3, *b[3:])]
        report: dict = {}

        events, _, removed = rows_to_l2_and_ticks(rows, dedup_report=report)

        assert removed == 2 and report["bidask_removed"] == 2
        assert len(events) == 2 * 3  # two snapshots of (clear, bid, ask)

    def test_the_sidecar_records_the_rule_and_what_it_removed(self, tmp_path: Path) -> None:
        report: dict = {}
        events, ticks, removed = rows_to_l2_and_ticks(self._doubled_day(), dedup_report=report)

        _write_day_outputs(
            symbol="TMFF6",
            date="2026-02-04",
            out_dir=tmp_path,
            events=events,
            ticks=ticks,
            dedup_removed=removed,
            dedup_report=report,
            owner="test",
            overwrite=False,
        )

        meta = json.loads((tmp_path / "tmff6" / "TMFF6_2026-02-04_ticks.npy.meta.json").read_text())
        l2_meta = json.loads((tmp_path / "tmff6" / "TMFF6_2026-02-04_l2.hftbt.npz.meta.json").read_text())
        assert meta["dedup_rule"] == l2_meta["dedup_rule"] == dedup.RULE_ID
        assert meta["dedup"]["tick"]["removed"] == 30
        assert l2_meta["dedup_removed"] == 30

    def test_a_sidecar_written_without_a_report_says_unrecorded(self, tmp_path: Path) -> None:
        events, ticks, removed = rows_to_l2_and_ticks(self._doubled_day())

        _write_day_outputs(
            symbol="TMFF6",
            date="2026-02-04",
            out_dir=tmp_path,
            events=events,
            ticks=ticks,
            dedup_removed=removed,
            owner="test",
            overwrite=False,
        )

        meta = json.loads((tmp_path / "tmff6" / "TMFF6_2026-02-04_ticks.npy.meta.json").read_text())
        assert meta["dedup_rule"] == "unrecorded"
