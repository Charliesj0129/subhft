"""The autouse ``_isolate_alpha_audit_sinks`` fixture keeps ledgers out of the checkout.

Without it, a clean-checkout run of the alpha unit tests appended fixture rows
(including a Gate D KILL for ``ofi_mc``) to the real research ledgers.
"""

from __future__ import annotations

import os
from pathlib import Path

from hft_platform.alpha import audit, kill_ledger, sub_gate_audit
from hft_platform.alpha.kill_ledger import KillRecord

_REPO_ROOT = Path(__file__).resolve().parents[2]


def _is_inside(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
    except ValueError:
        return False
    return True


def test_audit_fallback_dir_points_into_tmp_path(tmp_path):
    assert _is_inside(audit._FALLBACK_DIR, tmp_path)
    assert not _is_inside(audit._FALLBACK_DIR, _REPO_ROOT)


def test_audit_enabled_cache_is_reset_per_test():
    assert audit._ENABLED is None


def test_kill_ledger_append_lands_in_tmp_path(tmp_path):
    record = KillRecord(alpha_id="isolation_probe", gate="D", reason="fixture isolation probe")

    assert kill_ledger.append_kill(record) is True

    path = kill_ledger._jsonl_path()
    assert _is_inside(path, tmp_path)
    assert path.read_text(encoding="utf-8").count("isolation_probe") == 1


def test_kill_ledger_dedupe_cache_does_not_leak_between_tests(tmp_path):
    # Same record as the previous test: a leaked dedupe cache would report a
    # duplicate and refuse to write into this test's fresh ledger.
    record = KillRecord(alpha_id="isolation_probe", gate="D", reason="fixture isolation probe")

    assert kill_ledger.append_kill(record) is True
    assert _is_inside(kill_ledger._jsonl_path(), tmp_path)


def test_sub_gate_audit_path_points_into_tmp_path(tmp_path):
    assert _is_inside(sub_gate_audit._jsonl_path(), tmp_path)


def test_audit_fallback_write_lands_in_tmp_path(tmp_path):
    audit._write_fallback("alpha_gate_log", {"alpha_id": "isolation_probe"})

    written = audit._FALLBACK_DIR / "alpha_gate_log.jsonl"
    assert _is_inside(written, tmp_path)
    assert written.exists()
    assert os.environ["HFT_ALPHA_AUDIT_FALLBACK_DIR"] == str(audit._FALLBACK_DIR)
