import types
from pathlib import Path

import numpy as np

from hft_platform.alpha.validation import run_gate_a, run_gate_b


def test_run_gate_a_passes_with_alias_fields(tmp_path: Path):
    path = tmp_path / "feed.npy"
    arr = np.zeros(
        8,
        dtype=[
            ("best_bid", "i8"),
            ("best_ask", "i8"),
            ("bid_depth", "f8"),
            ("ask_depth", "f8"),
            ("qty", "f8"),
        ],
    )
    np.save(path, arr)

    manifest = types.SimpleNamespace(
        data_fields=("bid_px", "ask_px", "bid_qty", "ask_qty", "trade_vol", "current_mid"),
        complexity="O(1)",
    )
    report = run_gate_a(manifest, [str(path)])
    assert report.passed
    assert report.details["missing_fields"] == []


def test_run_gate_a_fails_when_required_fields_missing(tmp_path: Path):
    path = tmp_path / "feed.npy"
    arr = np.zeros(8, dtype=[("px", "i8"), ("qty", "f8")])
    np.save(path, arr)

    manifest = types.SimpleNamespace(
        data_fields=("bid_px", "ask_px"),
        complexity="O(N)",
    )
    report = run_gate_a(manifest, [str(path)])
    assert not report.passed
    assert "bid_px" in report.details["missing_fields"]
    assert "ask_px" in report.details["missing_fields"]


def test_run_gate_b_skip(tmp_path: Path):
    report = run_gate_b(alpha_id="ofi_mc", project_root=tmp_path, skip_tests=True, timeout_s=1)
    assert report.passed
    assert report.details["skipped"] is True


def test_run_gate_b_failure(monkeypatch, tmp_path: Path):
    class _Proc:
        returncode = 1
        stdout = "failed tests"
        stderr = "trace"

    monkeypatch.setattr("subprocess.run", lambda *a, **k: _Proc())
    report = run_gate_b(alpha_id="ofi_mc", project_root=tmp_path, skip_tests=False, timeout_s=1)
    assert not report.passed
    assert report.details["returncode"] == 1
