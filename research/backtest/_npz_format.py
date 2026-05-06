"""Canonical npz-format detector for the research backtest pipeline.

Codex adversarial-review 2026-05-06 finding 2 (HIGH) reframed: the
``_resolve_feature_mode -> "lob_feature"`` path silently degraded on
event-dtype-without-L2 input because ``deep_depth_momentum_x1000`` (FE-v3 idx
20) returns 0 on thin books. ``_check_hftbacktest_v2_data_format`` further
mis-checked for ``DEPTH_SNAPSHOT_EVENT``, which no real corpus file carries.

Canonical contract (see ``docs/runbooks/npz-formats-2026-05-06.md``):

* :data:`NpzFormat.HFTBT_EVENT_L5` -- ``*_l2.hftbt.npz`` produced by
  ``research/data/ck_export/export_golden.py``. Standard
  ``hftbacktest.types.event_dtype`` rows with depth events covering L1-L5.
  Sidecar ``.meta.json`` declares ``depth_levels >= 2``.
* :data:`NpzFormat.HFTBT_EVENT_L1` -- ``hftbt.npz`` produced by
  ``research/backtest/hft_native_runner.py::ensure_hftbt_npz``. Same dtype
  but only L1 information (the source ``research.npy`` is L1).
* :data:`NpzFormat.LEGACY_RESEARCH` -- ``*_ticks.npy`` / ``*_bidask.npy``
  pre-CK-export structured arrays. Not npz; needs conversion via
  ``ensure_hftbt_npz``.
* :data:`NpzFormat.UNKNOWN` -- everything else.

The detector is **detect-and-reject**: when an FE-v3 alpha's runner asks
for the L5-capable format and gets L1 input, raise
:class:`NpzFormatMismatchError` so the data layer is forced to fix the
input rather than silently emit zero MLDM through Gate C.
"""

from __future__ import annotations

import json
from enum import Enum
from pathlib import Path

import numpy as np


class NpzFormat(str, Enum):
    """Recognised on-disk shapes for the research backtest data pipeline."""

    HFTBT_EVENT_L5 = "hftbt_event_l5"
    HFTBT_EVENT_L1 = "hftbt_event_l1"
    LEGACY_RESEARCH = "legacy_research"
    UNKNOWN = "unknown"


class NpzFormatMismatchError(ValueError):
    """Raised when ``assert_format`` sees a different format than declared.

    The error carries both the *expected* and *detected* :class:`NpzFormat`
    so callers (and CI logs) can attribute the failure cleanly.
    """

    def __init__(self, path: str, expected: NpzFormat, detected: NpzFormat) -> None:
        self.path = str(path)
        self.expected = expected
        self.detected = detected
        super().__init__(
            f"NPZ format mismatch at {self.path}: expected={expected.value}, "
            f"detected={detected.value}"
        )


# hftbacktest.types.event_dtype field names (verified against the file
# header schema; see docs/runbooks/npz-formats-2026-05-06.md).
_HFTBT_EVENT_FIELDS: frozenset[str] = frozenset(
    {"ev", "exch_ts", "local_ts", "px", "qty", "order_id", "ival", "fval"}
)


def _read_meta_depth_levels(path: Path) -> int | None:
    sidecar = path.with_suffix(path.suffix + ".meta.json")
    if not sidecar.exists():
        return None
    try:
        meta = json.loads(sidecar.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    raw = meta.get("depth_levels")
    if not isinstance(raw, (int, float)):
        return None
    return int(raw)


def _is_event_dtype(arr: np.ndarray) -> bool:
    """An array carries the hftbacktest event_dtype iff every required field
    is present in its structured-dtype names. Extra fields are tolerated."""
    names = arr.dtype.names
    if not names:
        return False
    return _HFTBT_EVENT_FIELDS.issubset(set(names))


def detect_npz_format(path: str | Path) -> NpzFormat:
    """Inspect the path (and its sidecar, if any) and classify the format.

    Detection rules:

    * Suffix ``_l2.hftbt.npz`` AND a sidecar declaring ``depth_levels >= 2``
      AND the structured array carries hftbacktest event_dtype fields ->
      :data:`NpzFormat.HFTBT_EVENT_L5`.
    * Filename ``hftbt.npz`` (sibling of legacy research.npy) AND structured
      array carries hftbacktest event_dtype fields ->
      :data:`NpzFormat.HFTBT_EVENT_L1`.
    * Suffix ``_ticks.npy`` or ``_bidask.npy`` ->
      :data:`NpzFormat.LEGACY_RESEARCH`.
    * Otherwise -> :data:`NpzFormat.UNKNOWN`.

    The function returns ``UNKNOWN`` rather than raising for missing files,
    malformed npz archives, or untaggable shapes -- ``assert_format`` is
    where the contract is enforced.
    """
    p = Path(path)
    name = p.name

    if name.endswith("_ticks.npy") or name.endswith("_bidask.npy"):
        return NpzFormat.LEGACY_RESEARCH

    if not p.exists():
        return NpzFormat.UNKNOWN

    is_l5_candidate = name.endswith("_l2.hftbt.npz")
    is_l1_candidate = name == "hftbt.npz" or (
        name.endswith(".hftbt.npz") and not is_l5_candidate
    )

    if not (is_l5_candidate or is_l1_candidate):
        return NpzFormat.UNKNOWN

    try:
        loaded = np.load(p, allow_pickle=False)
    except Exception:
        return NpzFormat.UNKNOWN

    try:
        if isinstance(loaded, np.lib.npyio.NpzFile):
            if "data" not in loaded:
                return NpzFormat.UNKNOWN
            arr = np.asarray(loaded["data"])
        else:
            arr = np.asarray(loaded)
    finally:
        if hasattr(loaded, "close"):
            loaded.close()

    if not _is_event_dtype(arr):
        return NpzFormat.UNKNOWN

    if is_l5_candidate:
        depth = _read_meta_depth_levels(p)
        if depth is not None and depth >= 2:
            return NpzFormat.HFTBT_EVENT_L5
        # Filename promised L5 but the sidecar doesn't confirm.  Treat as
        # L1 (downgraded) so callers asking for L5 get a clear mismatch
        # rather than a silent zero-MLDM degradation.
        return NpzFormat.HFTBT_EVENT_L1

    # is_l1_candidate
    return NpzFormat.HFTBT_EVENT_L1


def assert_format(path: str | Path, expected: NpzFormat) -> None:
    """Raise :class:`NpzFormatMismatchError` if detected != expected.

    No-op on success.
    """
    detected = detect_npz_format(path)
    if detected != expected:
        raise NpzFormatMismatchError(str(path), expected, detected)
