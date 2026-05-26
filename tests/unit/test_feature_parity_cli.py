"""Tests for the `hft feature parity` CLI command."""

from __future__ import annotations

import argparse
import json

import pytest

from hft_platform.cli import _feature


def _run(**kwargs) -> argparse.Namespace:
    ns = argparse.Namespace(feature_set=None, require_rust=False)
    for k, v in kwargs.items():
        setattr(ns, k, v)
    return ns


def test_feature_parity_ok_emits_json_and_exits_zero(capsys) -> None:
    # Happy path: real self-test over available backends should pass.
    _feature.cmd_feature_parity(_run())
    out = capsys.readouterr().out
    # run_self_test() may emit compact single-line structlog warnings to stdout before the
    # indented result JSON; locate the result block (begins with "{\n" from json.dumps(indent=2))
    # and decode just that object, ignoring any surrounding log lines.
    start = out.index("{\n")
    payload, _ = json.JSONDecoder().raw_decode(out[start:])
    assert payload["ok"] is True
    assert payload["feature_set_id"] == "lob_shared_v3"
    assert payload["promoted_feature_ids"][0] == "mid_price_x2"


def test_feature_parity_require_rust_fails_when_unavailable(monkeypatch, capsys) -> None:
    def _fake_self_test(*, feature_set=None):
        return {
            "ok": True,
            "feature_set_id": "lob_shared_v3",
            "schema_version": 3,
            "n_frames": 10,
            "rust_available": False,
            "promoted_feature_ids": ["mid_price_x2"],
            "comparisons": [],
        }

    monkeypatch.setattr("hft_platform.feature.parity.run_self_test", _fake_self_test)
    with pytest.raises(SystemExit) as exc:
        _feature.cmd_feature_parity(_run(require_rust=True))
    assert exc.value.code == 1
    payload = json.loads(capsys.readouterr().out.strip())
    assert payload["ok"] is False
    assert "rust backend required" in payload["error"]


def test_feature_parity_divergence_exits_nonzero(monkeypatch) -> None:
    def _fake_self_test(*, feature_set=None):
        return {
            "ok": False,
            "feature_set_id": "lob_shared_v3",
            "schema_version": 3,
            "n_frames": 10,
            "rust_available": True,
            "promoted_feature_ids": ["mid_price_x2"],
            "comparisons": [
                {
                    "pair": "python vs rust",
                    "ok": False,
                    "first_divergence": {"feature_id": "ofi_l1_ema8", "expected": 1, "actual": 9},
                }
            ],
        }

    monkeypatch.setattr("hft_platform.feature.parity.run_self_test", _fake_self_test)
    with pytest.raises(SystemExit) as exc:
        _feature.cmd_feature_parity(_run())
    assert exc.value.code == 1
