"""Panel replay semantics vs snapshot_builder batch-flush rules (spec §7)."""

from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pytest

from research.candidate_loop import panels
from research.candidate_loop.panels import (
    DEPTH_CLEAR_EVENT,
    DEPTH_EVENT,
    TRADE_EVENT,
    Panel,
    build_panel,
    fetch_dir_coverage,
    replay_to_panel,
)

EVENT_DTYPE = np.dtype(
    [
        ("ev", "<u8"),
        ("exch_ts", "<i8"),
        ("local_ts", "<i8"),
        ("px", "<f8"),
        ("qty", "<f8"),
        ("order_id", "<u8"),
        ("ival", "<i8"),
        ("fval", "<f8"),
    ]
)

BID = 1 << 29
ASK = 1 << 28
EXCH_LOCAL = (1 << 31) | (1 << 30)  # real exports set status bits; replay ignores them


def _ev(base: int, side: int = 0) -> int:
    return base | side | EXCH_LOCAL


def _stream(rows: list[tuple[int, int, int, float, float]]) -> np.ndarray:
    """rows = [(ev, exch_ts, local_ts, px, qty), ...]"""
    out = np.zeros(len(rows), dtype=EVENT_DTYPE)
    for i, (ev, ets, lts, px, qty) in enumerate(rows):
        out[i] = (ev, ets, lts, px, qty, 0, 0, 0.0)
    return out


def _l5_batch(ts: int, lts: int, bids: list[tuple[float, float]], asks: list[tuple[float, float]]) -> list:
    rows = [(_ev(DEPTH_EVENT, BID), ts, lts, p, q) for p, q in bids]
    rows += [(_ev(DEPTH_EVENT, ASK), ts, lts, p, q) for p, q in asks]
    return rows


class TestBatchFlushSemantics:
    def test_one_row_per_exch_ts_batch(self) -> None:
        data = _stream(
            _l5_batch(1000, 1001, [(100.0, 5.0)], [(101.0, 3.0)])
            + _l5_batch(2000, 2002, [(100.0, 6.0)], [(101.0, 2.0)])
        )
        cols = replay_to_panel(data, tick_size=1.0)
        assert cols["exch_ts"].tolist() == [1000, 2000]

    def test_second_batch_replaces_levels_not_accumulates(self) -> None:
        data = _stream(
            _l5_batch(1000, 1000, [(100.0, 5.0), (99.0, 4.0)], [(101.0, 3.0)])
            + _l5_batch(2000, 2000, [(100.0, 7.0)], [(101.0, 2.0)])
        )
        cols = replay_to_panel(data, tick_size=1.0)
        # Batch 2 re-emitted only one bid level -> L2 must be gone, not carried.
        assert cols["bid_qty_1"][1] == 7.0
        assert np.isnan(cols["bid_px_2"][1])
        assert cols["bid_qty_2"][1] == 0.0

    def test_single_sided_batch_carries_other_side_forward(self) -> None:
        data = _stream(
            _l5_batch(1000, 1000, [(100.0, 5.0)], [(101.0, 3.0)])
            + _l5_batch(2000, 2000, [(100.0, 9.0)], [])  # bid-only batch
        )
        cols = replay_to_panel(data, tick_size=1.0)
        assert cols["bid_qty_1"][1] == 9.0
        assert cols["ask_px_1"][1] == 101.0  # carried forward
        assert cols["ask_qty_1"][1] == 3.0

    def test_qty_zero_phantom_clear_events_are_dropped(self) -> None:
        data = _stream(_l5_batch(1000, 1000, [(100.0, 5.0), (99.5, 0.0)], [(101.0, 3.0)]))
        cols = replay_to_panel(data, tick_size=1.0)
        assert cols["bid_px_1"][0] == 100.0
        assert np.isnan(cols["bid_px_2"][0])

    def test_bids_sorted_descending_asks_ascending_top5(self) -> None:
        bids = [(96.0 + i, 1.0 + i) for i in range(6)]  # 96..101, six levels
        asks = [(102.0 + i, 1.0 + i) for i in range(6)]
        data = _stream(_l5_batch(1000, 1000, bids, asks))
        cols = replay_to_panel(data, tick_size=1.0)
        assert [cols[f"bid_px_{i}"][0] for i in range(1, 6)] == [101.0, 100.0, 99.0, 98.0, 97.0]
        assert [cols[f"ask_px_{i}"][0] for i in range(1, 6)] == [102.0, 103.0, 104.0, 105.0, 106.0]

    def test_clear_event_resets_both_sides(self) -> None:
        data = _stream(
            _l5_batch(1000, 1000, [(100.0, 5.0)], [(101.0, 3.0)]) + [(_ev(DEPTH_CLEAR_EVENT), 2000, 2000, 0.0, 0.0)]
        )
        cols = replay_to_panel(data, tick_size=1.0)
        assert np.isnan(cols["bid_px_1"][1]) and np.isnan(cols["ask_px_1"][1])

    def test_local_ts_is_last_event_of_batch(self) -> None:
        data = _stream(_l5_batch(1000, 1005, [(100.0, 5.0)], []) + _l5_batch(1000, 1009, [], [(101.0, 3.0)]))
        cols = replay_to_panel(data, tick_size=1.0)
        assert cols["local_ts"].tolist() == [1009]

    def test_empty_input_returns_empty(self) -> None:
        assert replay_to_panel(np.zeros(0, dtype=EVENT_DTYPE), tick_size=1.0) == {}


class TestTradesAndDerivedColumns:
    def test_cumulative_trade_qty_by_side(self) -> None:
        data = _stream(
            _l5_batch(1000, 1000, [(100.0, 5.0)], [(101.0, 3.0)])
            + [(_ev(TRADE_EVENT, BID), 2000, 2000, 101.0, 2.0)]
            + [(_ev(TRADE_EVENT, ASK), 3000, 3000, 100.0, 4.0)]
            + [(_ev(TRADE_EVENT, BID), 3000, 3000, 101.0, 1.0)]
        )
        cols = replay_to_panel(data, tick_size=1.0)
        assert cols["trade_buy_qty"].tolist() == [0.0, 2.0, 3.0]
        assert cols["trade_sell_qty"].tolist() == [0.0, 0.0, 4.0]

    def test_trade_only_batch_carries_book_forward(self) -> None:
        data = _stream(
            _l5_batch(1000, 1000, [(100.0, 5.0)], [(101.0, 3.0)]) + [(_ev(TRADE_EVENT, BID), 2000, 2000, 101.0, 2.0)]
        )
        cols = replay_to_panel(data, tick_size=1.0)
        assert cols["bid_px_1"][1] == 100.0
        assert cols["ask_px_1"][1] == 101.0

    def test_mid_microprice_spread_known_values(self) -> None:
        data = _stream(_l5_batch(1000, 1000, [(100.0, 6.0)], [(102.0, 2.0)]))
        cols = replay_to_panel(data, tick_size=1.0)
        assert cols["mid"][0] == 101.0
        # microprice = (bid*ask_qty + ask*bid_qty)/(bid_qty+ask_qty)
        assert cols["microprice"][0] == pytest.approx((100.0 * 2.0 + 102.0 * 6.0) / 8.0)
        assert cols["spread_ticks"][0] == 2.0

    def test_microprice_nan_when_book_empty(self) -> None:
        data = _stream([(_ev(TRADE_EVENT, BID), 1000, 1000, 100.0, 1.0)])
        cols = replay_to_panel(data, tick_size=1.0)
        assert np.isnan(cols["microprice"][0])
        assert np.isnan(cols["mid"][0])


class _StubResult:
    def __init__(self, rows: list) -> None:
        self.result_rows = rows


class _StubClient:
    def __init__(self, rows: list | None = None, error: Exception | None = None) -> None:
        self._rows = rows or []
        self._error = error

    def query(self, sql: str, parameters: dict | None = None) -> _StubResult:
        if self._error is not None:
            raise self._error
        return _StubResult(self._rows)


class TestDirCoverage:
    def test_coverage_fraction_from_clickhouse(self) -> None:
        coverage, source = fetch_dir_coverage(_StubClient(rows=[(96, 100)]), "TXFD6", "2026-04-13")
        assert coverage == pytest.approx(0.96)
        assert source == "ch"

    def test_ch_error_fails_closed_to_zero(self) -> None:
        coverage, source = fetch_dir_coverage(_StubClient(error=ConnectionError("down")), "TXFD6", "2026-04-13")
        assert coverage == 0.0
        assert source.startswith("ch_error:")

    def test_day_with_no_trades_fails_closed(self) -> None:
        coverage, source = fetch_dir_coverage(_StubClient(rows=[(0, 0)]), "TXFD6", "2026-04-13")
        assert coverage == 0.0
        assert source == "ch_no_trades"


class TestPanelCache:
    def _write_npz(self, tmp_path: Path) -> Path:
        data = _stream(
            _l5_batch(1_000_000, 1_000_500, [(100.0, 5.0)], [(101.0, 3.0)])
            + _l5_batch(2_000_000, 2_000_500, [(100.0, 6.0)], [(101.0, 2.0)])
        )
        npz = tmp_path / "TXFT9_2026-01-02_l2.hftbt.npz"
        np.savez_compressed(npz, data=data)
        Path(str(npz) + ".meta.json").write_text(json.dumps({"data_fingerprint": "abc123", "generator": "test_gen"}))
        return npz

    def test_build_writes_cache_and_meta(self, tmp_path: Path) -> None:
        npz = self._write_npz(tmp_path)
        panel = build_panel(
            npz, "TXFT9", "2026-01-02", 1.0, tmp_path / "cache", dir_coverage=0.97, dir_coverage_source="ch"
        )
        assert isinstance(panel, Panel)
        assert panel.n_rows == 2
        assert panel.meta["dir_clean"] is True
        assert panel.meta["data_fingerprint"] == "abc123"
        assert panel.meta["source_generator"] == "test_gen"
        assert panel.meta["local_ts_equals_exch_ts_fraction"] == 0.0
        assert (tmp_path / "cache" / "TXFT9_2026-01-02.panel.npz").exists()

    def test_second_build_hits_cache_without_replay(self, tmp_path: Path, monkeypatch) -> None:
        npz = self._write_npz(tmp_path)
        cache = tmp_path / "cache"
        build_panel(npz, "TXFT9", "2026-01-02", 1.0, cache, dir_coverage=0.97)

        def _boom(*args: object, **kwargs: object) -> None:
            raise AssertionError("replay should not run on cache hit")

        monkeypatch.setattr(panels, "replay_to_panel", _boom)
        panel = build_panel(npz, "TXFT9", "2026-01-02", 1.0, cache, dir_coverage=0.97)
        assert panel.n_rows == 2

    def test_fingerprint_change_rebuilds(self, tmp_path: Path) -> None:
        npz = self._write_npz(tmp_path)
        cache = tmp_path / "cache"
        build_panel(npz, "TXFT9", "2026-01-02", 1.0, cache, dir_coverage=0.5)
        Path(str(npz) + ".meta.json").write_text(json.dumps({"data_fingerprint": "def456", "generator": "test_gen"}))
        panel = build_panel(npz, "TXFT9", "2026-01-02", 1.0, cache, dir_coverage=0.5)
        assert panel.meta["data_fingerprint"] == "def456"

    def test_cache_hit_refreshes_dir_coverage(self, tmp_path: Path) -> None:
        npz = self._write_npz(tmp_path)
        cache = tmp_path / "cache"
        build_panel(npz, "TXFT9", "2026-01-02", 1.0, cache, dir_coverage=0.5, dir_coverage_source="ch")
        panel = build_panel(npz, "TXFT9", "2026-01-02", 1.0, cache, dir_coverage=0.99, dir_coverage_source="ch")
        assert panel.meta["dir_coverage"] == 0.99
        assert panel.meta["dir_clean"] is True

    def test_no_dir_coverage_fails_closed_in_meta(self, tmp_path: Path) -> None:
        npz = self._write_npz(tmp_path)
        panel = build_panel(npz, "TXFT9", "2026-01-02", 1.0, tmp_path / "cache")
        assert panel.meta["dir_coverage"] == 0.0
        assert panel.meta["dir_clean"] is False
        assert panel.meta["dir_coverage_source"] == "not_queried"


GOLDEN_NPZ = (
    Path(__file__).resolve().parents[4] / "research" / "data" / "raw" / "txfd6" / "TXFD6_2026-04-13_l2.hftbt.npz"
)


@pytest.mark.skipif(not GOLDEN_NPZ.exists(), reason="golden-day NPZ not present")
class TestGoldenDayCrossCheck:
    """Panel L1 must agree with snapshot_builder's grid snapshots (spec §17.5)."""

    def test_l1_matches_snapshot_builder_on_sampled_grid(self) -> None:
        from research.tools.regime_lab.snapshot_builder import (
            SnapshotConfig,
            replay_to_snapshots,
        )

        data, _ = panels.load_l2_events(GOLDEN_NPZ)
        data = data[:500_000]  # bounded runtime; ~thousands of batches
        cols = replay_to_panel(data, tick_size=1.0)
        snap, n = replay_to_snapshots(
            data,
            contract_id=np.zeros(data.size, dtype=np.int32),
            is_roll_boundary=np.zeros(data.size, dtype=bool),
            cfg=SnapshotConfig(sample_period_ns=1_000_000_000, drop_warmup_seconds=0.0),
        )
        assert n > 100
        # snapshot_builder labels buckets by START but the state is as-of the
        # bucket END: last panel row with exch_ts strictly before start+period.
        cutoff = snap["exch_ts_ns"] + 1_000_000_000
        idx = np.searchsorted(cols["exch_ts"], cutoff, side="left") - 1
        sample = slice(10, n, max(1, n // 200))
        for grid_i in range(*sample.indices(n)):
            row = idx[grid_i]
            if row < 0:
                continue
            pb, sb = cols["bid_px_1"][row], snap["best_bid_px"][grid_i]
            pa, sa = cols["ask_px_1"][row], snap["best_ask_px"][grid_i]
            assert (np.isnan(pb) and np.isnan(sb)) or pb == sb
            assert (np.isnan(pa) and np.isnan(sa)) or pa == sa
            assert cols["bid_qty_1"][row] == snap["bid_qty_l1"][grid_i]
            assert cols["ask_qty_1"][row] == snap["ask_qty_l1"][grid_i]
