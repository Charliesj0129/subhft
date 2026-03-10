"""Unit tests for L2 (multi-level depth) support in ensure_hftbt_npz."""

from __future__ import annotations

from unittest.mock import patch

import numpy as np
import pytest

from research.backtest.hft_native_runner import (
    _has_l2_fields,
    _resolve_field,
    ensure_hftbt_npz,
)

# ---------------------------------------------------------------------------
# Dtype helpers
# ---------------------------------------------------------------------------
_L1_DTYPE = np.dtype(
    [
        ("bid_qty", "f8"),
        ("ask_qty", "f8"),
        ("bid_px", "f8"),
        ("ask_px", "f8"),
        ("volume", "f8"),
        ("local_ts", "i8"),
    ]
)

_L2_DTYPE = np.dtype(
    [
        ("bid_px_1", "f8"),
        ("bid_px_2", "f8"),
        ("bid_px_3", "f8"),
        ("bid_px_4", "f8"),
        ("bid_px_5", "f8"),
        ("ask_px_1", "f8"),
        ("ask_px_2", "f8"),
        ("ask_px_3", "f8"),
        ("ask_px_4", "f8"),
        ("ask_px_5", "f8"),
        ("bid_qty_1", "f8"),
        ("bid_qty_2", "f8"),
        ("bid_qty_3", "f8"),
        ("bid_qty_4", "f8"),
        ("bid_qty_5", "f8"),
        ("ask_qty_1", "f8"),
        ("ask_qty_2", "f8"),
        ("ask_qty_3", "f8"),
        ("ask_qty_4", "f8"),
        ("ask_qty_5", "f8"),
        ("volume", "f8"),
        ("local_ts", "i8"),
    ]
)


def _make_l1_npy(path: str, n: int = 10, *, volume: float = 0.0) -> None:
    """Create an L1 research.npy file."""
    arr = np.zeros(n, dtype=_L1_DTYPE)
    arr["bid_px"] = 99.9
    arr["ask_px"] = 100.1
    arr["bid_qty"] = 10.0
    arr["ask_qty"] = 5.0
    arr["volume"] = volume
    arr["local_ts"] = np.arange(n, dtype=np.int64) * 1_000_000
    np.save(path, arr)


def _make_l2_npy(
    path: str,
    n: int = 10,
    *,
    volume: float = 0.0,
    sparse_levels: bool = False,
) -> None:
    """Create an L2 research.npy with 5-level depth fields."""
    arr = np.zeros(n, dtype=_L2_DTYPE)
    for lvl in range(1, 6):
        arr[f"bid_px_{lvl}"] = 100.0 - lvl * 0.1
        arr[f"ask_px_{lvl}"] = 100.0 + lvl * 0.1
        arr[f"bid_qty_{lvl}"] = 10.0 + lvl
        arr[f"ask_qty_{lvl}"] = 5.0 + lvl
    if sparse_levels:
        # Zero out levels 4 and 5 (only 3 levels populated)
        for lvl in (4, 5):
            arr[f"bid_px_{lvl}"] = 0.0
            arr[f"ask_px_{lvl}"] = 0.0
    arr["volume"] = volume
    arr["local_ts"] = np.arange(n, dtype=np.int64) * 1_000_000
    np.save(path, arr)


# ---------------------------------------------------------------------------
# Helper function tests
# ---------------------------------------------------------------------------
class TestHasL2Fields:
    def test_detects_l2_bid_px_1(self) -> None:
        assert _has_l2_fields(("bid_px_1", "ask_px_1", "volume")) is True

    def test_detects_l2_bid_px_2(self) -> None:
        assert _has_l2_fields(("bid_px_2", "volume")) is True

    def test_false_for_l1_only(self) -> None:
        assert _has_l2_fields(("bid_px", "ask_px", "volume")) is False

    def test_false_for_empty(self) -> None:
        assert _has_l2_fields(()) is False


class TestResolveField:
    def test_resolves_first_match(self) -> None:
        assert _resolve_field(("bid_px_1", "other"), ("bid_px_1",)) == "bid_px_1"

    def test_returns_none_when_no_match(self) -> None:
        assert _resolve_field(("bid_px", "other"), ("bid_px_1",)) is None


# ---------------------------------------------------------------------------
# L1 backward compatibility
# ---------------------------------------------------------------------------
class TestEnsureHftbtNpzL1Compat:
    """Verify L1 behavior is unchanged when no L2 fields present."""

    def test_l1_generates_2_events_per_row_no_volume(self, tmp_path: object) -> None:
        """L1 without volume: exactly 2 events per row (bid + ask)."""
        npy = str(tmp_path / "research.npy")  # type: ignore[operator]
        n = 10
        _make_l1_npy(npy, n=n, volume=0.0)
        with patch("research.backtest.hft_native_runner._HFTBT_AVAILABLE", True):
            try:
                out = ensure_hftbt_npz(npy)
            except ImportError:
                pytest.skip("hftbacktest not installed")
        events = np.load(out, allow_pickle=False)["data"]
        assert len(events) == 2 * n

    def test_l1_generates_3_events_per_row_with_volume(self, tmp_path: object) -> None:
        """L1 with volume: 3 events per row (bid + ask + trade)."""
        npy = str(tmp_path / "research.npy")  # type: ignore[operator]
        n = 10
        _make_l1_npy(npy, n=n, volume=5.0)
        with patch("research.backtest.hft_native_runner._HFTBT_AVAILABLE", True):
            try:
                out = ensure_hftbt_npz(npy)
            except ImportError:
                pytest.skip("hftbacktest not installed")
        events = np.load(out, allow_pickle=False)["data"]
        assert len(events) == 3 * n


# ---------------------------------------------------------------------------
# L2 event emission
# ---------------------------------------------------------------------------
class TestEnsureHftbtNpzL2:
    """Tests for L2 multi-level depth event generation."""

    def test_l2_generates_10_events_per_row_no_volume(self, tmp_path: object) -> None:
        """L2 without volume: 10 depth events per row (5 bid + 5 ask)."""
        npy = str(tmp_path / "research.npy")  # type: ignore[operator]
        n = 5
        _make_l2_npy(npy, n=n, volume=0.0)
        with patch("research.backtest.hft_native_runner._HFTBT_AVAILABLE", True):
            try:
                out = ensure_hftbt_npz(npy)
            except ImportError:
                pytest.skip("hftbacktest not installed")
        events = np.load(out, allow_pickle=False)["data"]
        assert len(events) == 10 * n

    def test_l2_generates_11_events_per_row_with_volume(self, tmp_path: object) -> None:
        """L2 with volume: 11 events per row (5 bid + 5 ask + 1 trade)."""
        npy = str(tmp_path / "research.npy")  # type: ignore[operator]
        n = 5
        _make_l2_npy(npy, n=n, volume=3.0)
        with patch("research.backtest.hft_native_runner._HFTBT_AVAILABLE", True):
            try:
                out = ensure_hftbt_npz(npy)
            except ImportError:
                pytest.skip("hftbacktest not installed")
        events = np.load(out, allow_pickle=False)["data"]
        assert len(events) == 11 * n

    def test_l2_sparse_skips_zero_price_levels(self, tmp_path: object) -> None:
        """L2 sparse: levels 4,5 have price=0 -> only 6 events per row (3 bid + 3 ask)."""
        npy = str(tmp_path / "research.npy")  # type: ignore[operator]
        n = 5
        _make_l2_npy(npy, n=n, volume=0.0, sparse_levels=True)
        with patch("research.backtest.hft_native_runner._HFTBT_AVAILABLE", True):
            try:
                out = ensure_hftbt_npz(npy)
            except ImportError:
                pytest.skip("hftbacktest not installed")
        events = np.load(out, allow_pickle=False)["data"]
        # 3 bid + 3 ask = 6 per row
        assert len(events) == 6 * n

    def test_l2_first_row_uses_snapshot_flags(self, tmp_path: object) -> None:
        """First row of L2 data uses DEPTH_SNAPSHOT_EVENT flags."""
        npy = str(tmp_path / "research.npy")  # type: ignore[operator]
        _make_l2_npy(npy, n=3, volume=0.0)
        with patch("research.backtest.hft_native_runner._HFTBT_AVAILABLE", True):
            try:
                from hftbacktest.types import (
                    BUY_EVENT,
                    DEPTH_EVENT,
                    DEPTH_SNAPSHOT_EVENT,
                    EXCH_EVENT,
                    LOCAL_EVENT,
                    SELL_EVENT,
                )

                out = ensure_hftbt_npz(npy)
            except ImportError:
                pytest.skip("hftbacktest not installed")
        events = np.load(out, allow_pickle=False)["data"]
        snap_bid = int(DEPTH_SNAPSHOT_EVENT | EXCH_EVENT | LOCAL_EVENT | BUY_EVENT)
        snap_ask = int(DEPTH_SNAPSHOT_EVENT | EXCH_EVENT | LOCAL_EVENT | SELL_EVENT)
        depth_bid = int(DEPTH_EVENT | EXCH_EVENT | LOCAL_EVENT | BUY_EVENT)
        depth_ask = int(DEPTH_EVENT | EXCH_EVENT | LOCAL_EVENT | SELL_EVENT)

        # First 10 events (row 0): snapshot flags
        first_row_events = events[:10]
        for ev in first_row_events[:5]:  # bids
            assert int(ev["ev"]) == snap_bid
        for ev in first_row_events[5:]:  # asks
            assert int(ev["ev"]) == snap_ask

        # Second row (events 10-19): depth flags
        second_row_events = events[10:20]
        for ev in second_row_events[:5]:  # bids
            assert int(ev["ev"]) == depth_bid
        for ev in second_row_events[5:]:  # asks
            assert int(ev["ev"]) == depth_ask

    def test_l2_all_zeros_raises_valueerror(self, tmp_path: object) -> None:
        """L2 with all prices zero raises ValueError."""
        npy = str(tmp_path / "research.npy")  # type: ignore[operator]
        arr = np.zeros(5, dtype=_L2_DTYPE)
        arr["local_ts"] = np.arange(5, dtype=np.int64) * 1_000_000
        np.save(npy, arr)
        with patch("research.backtest.hft_native_runner._HFTBT_AVAILABLE", True):
            try:
                with pytest.raises(ValueError, match="No valid events"):
                    ensure_hftbt_npz(npy)
            except ImportError:
                pytest.skip("hftbacktest not installed")

    def test_l2_idempotent_when_sibling_exists(self, tmp_path: object) -> None:
        """If hftbt.npz sibling exists, returns immediately (same as L1)."""
        hbt = tmp_path / "hftbt.npz"  # type: ignore[operator]
        hbt.touch()
        npy = str(tmp_path / "research.npy")  # type: ignore[operator]
        result = ensure_hftbt_npz(npy)
        assert result == str(hbt)

    def test_l2_output_is_sibling(self, tmp_path: object) -> None:
        """Output hftbt.npz is in same directory as input .npy."""
        npy = str(tmp_path / "research.npy")  # type: ignore[operator]
        _make_l2_npy(npy, n=5)
        with patch("research.backtest.hft_native_runner._HFTBT_AVAILABLE", True):
            try:
                out = ensure_hftbt_npz(npy)
            except ImportError:
                pytest.skip("hftbacktest not installed")
        from pathlib import Path

        assert Path(out).parent == tmp_path  # type: ignore[arg-type]
        assert Path(out).name == "hftbt.npz"

    def test_l2_timestamps_monotonic(self, tmp_path: object) -> None:
        """All emitted events have monotonically non-decreasing timestamps."""
        npy = str(tmp_path / "research.npy")  # type: ignore[operator]
        _make_l2_npy(npy, n=10, volume=2.0)
        with patch("research.backtest.hft_native_runner._HFTBT_AVAILABLE", True):
            try:
                out = ensure_hftbt_npz(npy)
            except ImportError:
                pytest.skip("hftbacktest not installed")
        events = np.load(out, allow_pickle=False)["data"]
        ts_field = "local_ts" if "local_ts" in events.dtype.names else "exch_ts"
        assert np.all(np.diff(events[ts_field]) >= 0)

    def test_l2_trade_uses_l1_mid_price(self, tmp_path: object) -> None:
        """Trade event mid price is computed from L1 (level 1) bid/ask."""
        npy = str(tmp_path / "research.npy")  # type: ignore[operator]
        # 1 row with known prices
        arr = np.zeros(1, dtype=_L2_DTYPE)
        arr["bid_px_1"] = 99.0
        arr["ask_px_1"] = 101.0
        arr["bid_qty_1"] = 10.0
        arr["ask_qty_1"] = 10.0
        for lvl in range(2, 6):
            arr[f"bid_px_{lvl}"] = 99.0 - lvl
            arr[f"ask_px_{lvl}"] = 101.0 + lvl
            arr[f"bid_qty_{lvl}"] = 5.0
            arr[f"ask_qty_{lvl}"] = 5.0
        arr["volume"] = 100.0
        arr["local_ts"] = 1_000_000
        np.save(npy, arr)
        with patch("research.backtest.hft_native_runner._HFTBT_AVAILABLE", True):
            try:
                from hftbacktest.types import TRADE_EVENT

                out = ensure_hftbt_npz(npy)
            except ImportError:
                pytest.skip("hftbacktest not installed")
        events = np.load(out, allow_pickle=False)["data"]
        trade_events = [e for e in events if int(e["ev"]) & int(TRADE_EVENT)]
        assert len(trade_events) == 1
        expected_mid = (99.0 + 101.0) / 2.0
        assert trade_events[0]["px"] == pytest.approx(expected_mid)
