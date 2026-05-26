"""Promoted-family feature parity across Python / Rust / hftbacktest-shared paths.

Guards Feature Plane Productionization: research / replay / live must produce the
same feature schema, feature_ids, warmup/reset behavior, and promoted-family values
(within tolerance) for one shared sequence of raw LOB snapshots. Failures surface the
first divergence with symbol / timestamp / feature_id / expected / actual.
"""

from __future__ import annotations

import dataclasses

import numpy as np
import pytest

from hft_platform.feature.parity import (
    LobInputFrame,
    compare_paths,
    run_hftbacktest_shared,
    run_python_engine,
    run_rust_engine,
)
from hft_platform.feature.registry import (
    default_feature_registry,
    promoted_feature_ids,
    promoted_indices,
)

SYMBOL = "TXFR1"
TICK = 1_000  # 0.1 * 10_000 scaled price step


def _book(best_bid: int, best_ask: int, bid_qty: int, ask_qty: int) -> tuple[np.ndarray, np.ndarray]:
    """Build a 5-level book around the given top-of-book."""
    bids = np.array(
        [[best_bid - i * TICK, max(1, bid_qty - i * 3)] for i in range(5)],
        dtype=np.int64,
    )
    asks = np.array(
        [[best_ask + i * TICK, max(1, ask_qty - i * 3)] for i in range(5)],
        dtype=np.int64,
    )
    return bids, asks


def _build_frames() -> list[LobInputFrame]:
    """Deterministic sequence: warmup ramp, steady, one-sided, reset+re-warm."""
    frames: list[LobInputFrame] = []
    base_bid = 1_000_000
    ts = 1_000

    # Warmup ramp + steady book with moving top-of-book (exercises OFI/EMA state).
    for i in range(40):
        bb = base_bid + (i % 6) * TICK
        ba = bb + 2 * TICK
        bids, asks = _book(bb, ba, 50 + i, 40 + (i % 7))
        frames.append(LobInputFrame(SYMBOL, ts, bids, asks))
        ts += 125

    # One-sided thin ask book.
    for i in range(5):
        bb = base_bid + 3 * TICK
        ba = bb + TICK
        bids, asks = _book(bb, ba, 80, 1)
        frames.append(LobInputFrame(SYMBOL, ts, bids, asks))
        ts += 125

    # Gap-triggered reset, then re-warm from scratch.
    bb = base_bid + 2 * TICK
    ba = bb + 2 * TICK
    bids, asks = _book(bb, ba, 60, 55)
    frames.append(LobInputFrame(SYMBOL, ts, bids, asks, is_reset=True))
    ts += 125
    for i in range(10):
        bb = base_bid + (i % 4) * TICK
        ba = bb + 2 * TICK
        bids, asks = _book(bb, ba, 45 + i, 50 - i)
        frames.append(LobInputFrame(SYMBOL, ts, bids, asks))
        ts += 125

    return frames


@pytest.fixture(scope="module")
def frames() -> list[LobInputFrame]:
    return _build_frames()


@pytest.fixture(scope="module")
def feature_set():
    return default_feature_registry().get_default()


def test_promoted_family_is_minimal_microstructure_set(feature_set) -> None:
    ids = set(promoted_feature_ids(feature_set))
    expected = {
        "mid_price_x2",
        "spread_scaled",
        "microprice_x2",
        "depth_imbalance_ppm",
        "l1_imbalance_ppm",
        "ofi_l1_raw",
        "ofi_l1_cum",
        "ofi_l1_ema8",
        "spread_ema8_scaled",
        "depth_imbalance_ema8_ppm",
    }
    assert ids == expected
    # Promoted family must live within the v1 (first 16) indices the Rust kernel covers.
    assert max(promoted_indices(feature_set)) < 16


def test_promoted_family_schema_identical_across_paths(frames, feature_set) -> None:
    py = run_python_engine(frames)
    hb = run_hftbacktest_shared(frames)
    assert len(py) == len(hb) > 0
    assert py[0].feature_ids == hb[0].feature_ids
    assert py[0].feature_ids == feature_set.feature_ids
    rust = run_rust_engine(frames)
    if rust is not None:
        assert rust[0].feature_ids == py[0].feature_ids


def test_python_hftbacktest_shared_parity_exact(frames, feature_set) -> None:
    """Same Python kernel via direct live wiring vs adapter wiring → exact match."""
    report = compare_paths(
        {"python": run_python_engine(frames), "hftbt_shared": run_hftbacktest_shared(frames)},
        feature_set=feature_set,
    )
    report.raise_if_failed()
    assert report.n_frames > 0


def test_python_rust_promoted_parity_within_tolerance(frames, feature_set) -> None:
    rust = run_rust_engine(frames)
    if rust is None:
        pytest.skip("Rust extension not available")
    report = compare_paths(
        {"python": run_python_engine(frames), "rust": rust},
        feature_set=feature_set,
    )
    report.raise_if_failed()


def test_reset_rewarm_parity(frames) -> None:
    """warmup_ready_mask progression (incl. post-reset re-warm) matches across paths."""
    py = run_python_engine(frames)
    hb = run_hftbacktest_shared(frames)
    py_masks = [f.warmup_ready_mask for f in py]
    hb_masks = [f.warmup_ready_mask for f in hb]
    assert py_masks == hb_masks
    # The reset must visibly drop warmup readiness then climb back (not stuck full).
    assert min(py_masks) < max(py_masks)
    rust = run_rust_engine(frames)
    if rust is not None:
        assert [f.warmup_ready_mask for f in rust] == py_masks


def test_divergence_report_pinpoints_first_mismatch(frames, feature_set) -> None:
    """Inject a perturbation and confirm the report names the right coordinates."""
    py = run_python_engine(frames)
    # Find the index of microprice_x2 and corrupt it on frame 7 of a copied path.
    mp_idx = feature_set.index_by_id["microprice_x2"]
    corrupted = list(py)
    bad_frame = py[7]
    bad_values = list(bad_frame.values)
    bad_values[mp_idx] = bad_values[mp_idx] + 5  # exceeds tolerance 0
    corrupted[7] = dataclasses.replace(bad_frame, values=tuple(bad_values))

    report = compare_paths({"python": py, "perturbed": corrupted}, feature_set=feature_set)
    assert not report.ok
    div = report.first_divergence
    assert div is not None
    assert div.frame_index == 7
    assert div.feature_id == "microprice_x2"
    assert div.index == mp_idx
    assert div.symbol == bad_frame.symbol
    assert div.timestamp == bad_frame.timestamp
    assert div.abs_diff == 5
    assert div.tolerance == 0
    # The human-readable message carries every coordinate.
    msg = report.format()
    assert "microprice_x2" in msg
    assert str(bad_frame.timestamp) in msg
