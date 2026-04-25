"""M2 (2026-04-25): bootstrap-time orphan tmpfile cleanup pass.

Production evidence: 8 ``tmp*.tmp`` files aged 5–13 days persisted in
``/app/.wal/`` because the previous ``except: unlink; raise`` cleanup
pattern only fires on exception, not on SIGKILL or thread death between
``mkstemp`` and ``os.rename``. The new ``finally:`` cleanup prevents NEW
orphans, and the bootstrap sweep removes legacy orphans on next start.

Tests:
1. Sweep removes synthesized orphan ``tmp*.tmp`` files older than the
   configured age threshold.
2. Sweep does NOT touch files younger than the threshold (peer-process
   safety).
3. Sweep is a no-op when wal_dir does not exist.
4. Sweep ignores non-matching filenames (only ``tmp*.tmp`` pattern).
"""

from __future__ import annotations

import os
import time

import pytest

from hft_platform.recorder.worker import _sweep_wal_orphan_tmpfiles


@pytest.fixture()
def wal_dir(tmp_path):
    d = tmp_path / "wal"
    d.mkdir()
    return d


def _make_orphan(d, name: str, age_s: float, size: int = 1024) -> str:
    p = d / name
    p.write_bytes(b"x" * size)
    mtime = time.time() - age_s
    os.utime(p, (mtime, mtime))
    return str(p)


def test_sweep_removes_old_orphan_tmpfiles(wal_dir) -> None:
    """Files matching tmp*.tmp older than max_age_s must be unlinked."""
    p1 = _make_orphan(wal_dir, "tmpabcd1234.tmp", age_s=600)  # 10 minutes old
    p2 = _make_orphan(wal_dir, "tmpzzzz9999.tmp", age_s=86400)  # 1 day old
    p3 = _make_orphan(wal_dir, "tmpfresh001.tmp", age_s=10)  # 10 seconds — keep

    cleaned = _sweep_wal_orphan_tmpfiles(str(wal_dir), max_age_s=300.0)

    assert cleaned == 2, f"Expected 2 cleaned, got {cleaned}"
    assert not os.path.exists(p1), "Old orphan should have been unlinked"
    assert not os.path.exists(p2), "Old orphan should have been unlinked"
    assert os.path.exists(p3), "Fresh orphan must NOT be unlinked (peer safety)"


def test_sweep_ignores_non_matching_filenames(wal_dir) -> None:
    """Only ``tmp*.tmp`` files are touched; .jsonl / .bak / random must survive."""
    keep = [
        _make_orphan(wal_dir, "batch_1234567890_99.jsonl", age_s=86400),
        _make_orphan(wal_dir, "manifest.txt.bak", age_s=86400),
        _make_orphan(wal_dir, "data_1234.json", age_s=86400),
        _make_orphan(wal_dir, "tmp_no_suffix", age_s=86400),  # missing .tmp suffix
        _make_orphan(wal_dir, "prefix_tmp.tmp", age_s=86400),  # wrong prefix
    ]
    target = _make_orphan(wal_dir, "tmpXYZ.tmp", age_s=86400)

    cleaned = _sweep_wal_orphan_tmpfiles(str(wal_dir), max_age_s=60.0)

    assert cleaned == 1, f"Expected exactly 1 file cleaned, got {cleaned}"
    assert not os.path.exists(target), "Matching tmp*.tmp should be unlinked"
    for path in keep:
        assert os.path.exists(path), f"Non-matching file must survive sweep: {path}"


def test_sweep_noop_on_missing_dir(tmp_path) -> None:
    """Calling sweep on a non-existent wal_dir must NOT raise."""
    missing = str(tmp_path / "does_not_exist")
    assert _sweep_wal_orphan_tmpfiles(missing) == 0


def test_sweep_with_default_max_age(wal_dir) -> None:
    """Default max_age_s=300; verify a ~5min boundary works as expected."""
    p_just_over = _make_orphan(wal_dir, "tmpover.tmp", age_s=301)
    p_just_under = _make_orphan(wal_dir, "tmpunder.tmp", age_s=299)

    cleaned = _sweep_wal_orphan_tmpfiles(str(wal_dir))  # default 300

    assert cleaned == 1
    assert not os.path.exists(p_just_over)
    assert os.path.exists(p_just_under)
