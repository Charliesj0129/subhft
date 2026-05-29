"""End-to-end: candidate spec.yaml -> sub_gate_audit row.

Rounds 17–20 built each link of the spec-to-audit chain:
  * Round 17  sub_gate_audit row carries spec_provenance.
  * Round 18  extract_provenance projects a spec dict.
  * Round 19  audit_cli compare renders spec_provenance diffs.
  * Round 20  load_spec_provenance one-call helper.

This test closes the loop: take the shipped exemplar
(``research/alphas/_templates/spec.yaml``), inject its provenance
into a synthetic Gate-C result payload, run ``_invoke_sub_gates``
under the audit env flag, and assert the recorded JSONL row carries
the exemplar's data_range / cost_model_id / required_gates intact.

No production pipeline is touched — this proves the read/inject/
record chain works so any future pipeline caller can opt-in with
two lines:

    prov = load_spec_provenance(args.alpha_id)
    if prov is not None:
        result_payload["spec_provenance"] = prov
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from hft_platform.alpha import sub_gate_audit
from hft_platform.alpha._gate_c import _invoke_sub_gates
from hft_platform.alpha._validation_profile import ValidationProfile
from hft_platform.alpha.strategy_spec import load_spec_provenance


def _payload_with(prov: dict | None, *, run_id: str = "e2e_round21") -> dict:
    payload: dict = {
        "run_id": run_id,
        "config_hash": "abc",
        "instrument": "TMFD6",
        "strategy_name": "spec_e2e_demo",
        "engine": "maker_engine",
        "queue_model": "QueueDepletionFill",
        "calibration_profile_id": "uncalibrated",
        "data_source": "ck",
        "latency_profile": "shioaji_measured_p95",
        "pnl_pts": 0.0,
        "n_fills": 39,
        "n_trading_days": 31,
        "equity_curve": None,
        "pnl_per_fill": 0.0,
        "adverse_fill_pct": 0.30,
        "fill_rate_per_day": 1.26,
        "daily_pnl": [10.0] * 31,
    }
    if prov is not None:
        payload["spec_provenance"] = prov
    return payload


def _strict_profile() -> ValidationProfile:
    return ValidationProfile(
        name="round21_strict",
        is_strict=True,
        thresholds={
            "maker": {
                "min_fills": 300,
                "min_days": 60,
                "outlier_day_contribution_max_pct": 25.0,
            }
        },
        blocking_sub_gates=("min_sample_size",),
    )


@pytest.fixture
def _isolated_audit(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    path = tmp_path / "sub_gate_runs.jsonl"
    monkeypatch.setenv("HFT_SUB_GATE_AUDIT_PATH", str(path))
    monkeypatch.setenv("HFT_SUB_GATE_AUDIT_ENABLED", "1")
    sub_gate_audit._reset_cache_for_tests()
    return path


class TestSpecToAuditEndToEnd:
    def test_exemplar_provenance_flows_into_audit_row(
        self, _isolated_audit: Path
    ) -> None:
        prov = load_spec_provenance("_templates", root="research/alphas")
        assert prov is not None, "Round 13 exemplar must load"
        prof = _strict_profile()
        _invoke_sub_gates(
            strategy_type="maker",
            result_payload=_payload_with(prov),
            thresholds=prof.thresholds_for(strategy_type="maker"),
            profile=prof,
        )
        rows = sub_gate_audit.read_runs(run_id="e2e_round21")
        assert len(rows) == 1
        row_prov = rows[0]["spec_provenance"]
        # The exemplar's data_range / cost_model knobs must round-trip
        # through extract_provenance -> result_payload ->
        # _invoke_sub_gates -> audit JSONL unchanged.
        assert row_prov["data_range"] == prov["data_range"]
        assert row_prov["cost_model_id"] == prov["cost_model_id"]
        assert row_prov["required_gates"] == prov["required_gates"]
        # And the row should still classify R47-fingerprint sample size
        # as a sample-* triage (Round 6) — not killed — so provenance
        # threading does NOT change the validation verdict.
        assert rows[0]["triage_status"].startswith("sample_")

    def test_audit_row_omits_block_when_no_provenance_injected(
        self, _isolated_audit: Path
    ) -> None:
        prof = _strict_profile()
        _invoke_sub_gates(
            strategy_type="maker",
            result_payload=_payload_with(None, run_id="e2e_no_prov"),
            thresholds=prof.thresholds_for(strategy_type="maker"),
            profile=prof,
        )
        rows = sub_gate_audit.read_runs(run_id="e2e_no_prov")
        assert len(rows) == 1
        # No injection -> no spec_provenance key (Round 17 opt-in shape).
        assert "spec_provenance" not in rows[0]

    def test_two_runs_with_drifted_specs_show_provenance_diff(
        self,
        _isolated_audit: Path,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # Build two specs differing only in data_range; show the audit
        # rows reflect the drift end-to-end, suitable for `audit compare`.
        import yaml

        from hft_platform.alpha.strategy_spec import extract_provenance

        prov_a = extract_provenance(
            {
                "validation_plan": {
                    "data_range": "2026-01..2026-03",
                    "required_gates": ["min_sample_size"],
                },
                "cost_model": {
                    "latency_profile": "p95",
                    "fee_bps": 0.4,
                    "tax_bps": 2.0,
                    "slippage_pts": 0.5,
                },
            }
        )
        prov_b = dict(prov_a)
        prov_b["data_range"] = "2026-04..2026-05"

        prof = _strict_profile()
        for rid, prov in (("e2e_a", prov_a), ("e2e_b", prov_b)):
            _invoke_sub_gates(
                strategy_type="maker",
                result_payload=_payload_with(prov, run_id=rid),
                thresholds=prof.thresholds_for(strategy_type="maker"),
                profile=prof,
            )
        rows_a = sub_gate_audit.read_runs(run_id="e2e_a")
        rows_b = sub_gate_audit.read_runs(run_id="e2e_b")
        assert rows_a[0]["spec_provenance"]["data_range"] == "2026-01..2026-03"
        assert rows_b[0]["spec_provenance"]["data_range"] == "2026-04..2026-05"
        # cost_model_id stayed constant — that's what compare relies on
        # to attribute outcome diffs to data_range, not cost drift.
        assert (
            rows_a[0]["spec_provenance"]["cost_model_id"]
            == rows_b[0]["spec_provenance"]["cost_model_id"]
        )
