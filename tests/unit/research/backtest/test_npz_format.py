"""Tests for ``research/backtest/_npz_format.py``.

Five canonical cases per ``docs/runbooks/npz-formats-2026-05-06.md``:

1. ``*_l2.hftbt.npz`` with sidecar depth>=2 detects as HFTBT_EVENT_L5.
2. ``hftbt.npz`` (no sidecar) detects as HFTBT_EVENT_L1.
3. ``*_ticks.npy`` / ``*_bidask.npy`` detects as LEGACY_RESEARCH.
4. ``assert_format`` raises ``NpzFormatMismatchError`` with both expected
   and detected fields populated.
5. Unrecognised filename / malformed dtype detects as UNKNOWN.
"""

from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pytest

from research.backtest._npz_format import (
    NpzFormat,
    NpzFormatMismatchError,
    assert_format,
    detect_npz_format,
)


# hftbacktest event_dtype, declared inline so the test is independent of
# whether hftbacktest is importable in the test environment.
_EVENT_DTYPE = np.dtype(
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


def _write_event_npz(path: Path, n: int = 4) -> None:
    arr = np.zeros(n, dtype=_EVENT_DTYPE)
    arr["ev"] = 3489660929  # DEPTH | EXCH | LOCAL | SELL — verified value
    arr["exch_ts"] = np.arange(n, dtype=np.int64) * 1_000_000
    arr["local_ts"] = arr["exch_ts"]
    arr["px"] = 36000.0 + np.arange(n)
    arr["qty"] = 1.0
    np.savez_compressed(path, data=arr)


def _write_meta(path: Path, depth_levels: int) -> None:
    """Write a sidecar at ``<path>.meta.json``."""
    sidecar = path.with_suffix(path.suffix + ".meta.json")
    sidecar.write_text(
        json.dumps(
            {
                "dataset_id": path.stem,
                "depth_levels": depth_levels,
                "price_scale_applied": 1_000_000.0,
            }
        )
    )


# ---------------------------------------------------------------------------
# Case 1: HFTBT_EVENT_L5 (CK export with sidecar)
# ---------------------------------------------------------------------------


def test_l2_hftbt_npz_with_l5_sidecar_detects_event_l5(tmp_path: Path) -> None:
    npz = tmp_path / "TMFD6_2026-04-14_l2.hftbt.npz"
    _write_event_npz(npz)
    _write_meta(npz, depth_levels=5)

    assert detect_npz_format(npz) is NpzFormat.HFTBT_EVENT_L5


def test_l2_hftbt_npz_without_sidecar_downgrades_to_l1(tmp_path: Path) -> None:
    """Filename promises L5 but sidecar absent -> downgrade to L1 so callers
    asking for L5 get a clean mismatch rather than silent zero-MLDM."""
    npz = tmp_path / "TMFD6_2026-04-14_l2.hftbt.npz"
    _write_event_npz(npz)

    assert detect_npz_format(npz) is NpzFormat.HFTBT_EVENT_L1


# ---------------------------------------------------------------------------
# Case 2: HFTBT_EVENT_L1 (legacy ensure_hftbt_npz output)
# ---------------------------------------------------------------------------


def test_hftbt_npz_detects_event_l1(tmp_path: Path) -> None:
    npz = tmp_path / "hftbt.npz"
    _write_event_npz(npz)

    assert detect_npz_format(npz) is NpzFormat.HFTBT_EVENT_L1


# ---------------------------------------------------------------------------
# Case 3: LEGACY_RESEARCH
# ---------------------------------------------------------------------------


def test_ticks_npy_detects_legacy_research(tmp_path: Path) -> None:
    p = tmp_path / "TMFD6_2026-03-19_ticks.npy"
    arr = np.zeros(4, dtype=[("bid_px", "<f8"), ("ask_px", "<f8")])
    np.save(p, arr)

    assert detect_npz_format(p) is NpzFormat.LEGACY_RESEARCH


def test_bidask_npy_detects_legacy_research(tmp_path: Path) -> None:
    p = tmp_path / "TMFD6_2026-03-19_bidask.npy"
    arr = np.zeros(4, dtype=[("bid_px", "<f8"), ("ask_px", "<f8")])
    np.save(p, arr)

    assert detect_npz_format(p) is NpzFormat.LEGACY_RESEARCH


def test_legacy_research_detection_does_not_require_existence(tmp_path: Path) -> None:
    """The suffix-based legacy classification must not depend on file existence
    -- the runner needs to discriminate before deciding whether to convert."""
    nonexistent = tmp_path / "TMFD6_2026-03-19_ticks.npy"

    assert detect_npz_format(nonexistent) is NpzFormat.LEGACY_RESEARCH


# ---------------------------------------------------------------------------
# Case 4: UNKNOWN
# ---------------------------------------------------------------------------


def test_arbitrary_npz_without_event_fields_detects_unknown(tmp_path: Path) -> None:
    npz = tmp_path / "random.hftbt.npz"
    arr = np.zeros(4, dtype=[("foo", "<i8"), ("bar", "<f8")])
    np.savez_compressed(npz, data=arr)

    # Arbitrary structured dtype lacking the event_dtype fields; classified
    # as L1 candidate by name but downgraded to UNKNOWN by dtype check.
    assert detect_npz_format(npz) is NpzFormat.UNKNOWN


def test_unrecognised_filename_detects_unknown(tmp_path: Path) -> None:
    other = tmp_path / "something_else.npz"
    arr = np.zeros(4, dtype=_EVENT_DTYPE)
    np.savez_compressed(other, data=arr)

    assert detect_npz_format(other) is NpzFormat.UNKNOWN


def test_missing_file_detects_unknown(tmp_path: Path) -> None:
    """For non-legacy filenames, a missing file is UNKNOWN (not LEGACY)."""
    npz = tmp_path / "TMFD6_2026-04-14_l2.hftbt.npz"

    assert detect_npz_format(npz) is NpzFormat.UNKNOWN


# ---------------------------------------------------------------------------
# Case 5: assert_format mismatch error
# ---------------------------------------------------------------------------


def test_assert_format_raises_on_mismatch(tmp_path: Path) -> None:
    """The L5 path must hard-fail when given an L1 input -- this is the
    contract that prevents zero-MLDM silent degradation in Gate C."""
    npz = tmp_path / "hftbt.npz"
    _write_event_npz(npz)

    with pytest.raises(NpzFormatMismatchError) as exc_info:
        assert_format(npz, NpzFormat.HFTBT_EVENT_L5)

    err = exc_info.value
    assert err.expected is NpzFormat.HFTBT_EVENT_L5
    assert err.detected is NpzFormat.HFTBT_EVENT_L1
    assert "hftbt.npz" in str(err)


def test_assert_format_passes_on_match(tmp_path: Path) -> None:
    npz = tmp_path / "TMFD6_2026-04-14_l2.hftbt.npz"
    _write_event_npz(npz)
    _write_meta(npz, depth_levels=5)

    # Should not raise
    assert_format(npz, NpzFormat.HFTBT_EVENT_L5)
