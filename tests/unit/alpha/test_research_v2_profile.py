"""Research v2 PR2b: the v2 strict profile takes effect without relaxing anything.

Covers the ``vm_ul6_strict_v2`` profile against the frozen v1, the profile
loader's ``deferrable`` / ``not_applicable_gates``, the profile-wins (and
never-looser) threshold merge, Gate A's provenance and latency checks, Gate F
under a profile that declares it not applicable, and the pipeline's choice of
profile for governed runs.
"""

from __future__ import annotations

import json
import types
from pathlib import Path

import numpy as np
import pytest
import yaml

import research.pipeline as pipeline
from hft_platform.alpha._gate_a import run_gate_a
from hft_platform.alpha._gate_c import _merge_profile_thresholds
from hft_platform.alpha._validation_profile import ProfileValidationError, ValidationProfile, load_profile
from hft_platform.alpha._validation_types import ValidationConfig
from hft_platform.alpha.latency_profiles import load_profiles
from hft_platform.alpha.promotion import PromotionConfig, _evaluate_gate_f

_ROOT = Path(__file__).resolve().parents[3]
_PROFILES = _ROOT / "config" / "research" / "profiles"
_V1 = _PROFILES / "vm_ul6_strict.yaml"
_V2 = _PROFILES / "vm_ul6_strict_v2.yaml"

# The only pipeline_overrides v2 changes, each a repaired reference.
_V2_OVERRIDE_CHANGES = {
    "latency_profile_id",
    "submit_ack_latency_ms",
    "modify_ack_latency_ms",
    "maker_fee_bps",
    "enforce_rust_benchmark_gate",
}


def _yaml(path: Path) -> dict:
    return yaml.safe_load(path.read_text())


def test_v2_profile_keeps_v1_thresholds_and_blocking_list():
    v1, v2 = _yaml(_V1), _yaml(_V2)
    assert v2["thresholds"] == v1["thresholds"]
    assert v2["blocking_sub_gates"] == v1["blocking_sub_gates"]
    assert v2["is_strict"] is True
    assert v2["require_real_equity"] == v1["require_real_equity"]
    assert v2["allowed_equity_sources"] == v1["allowed_equity_sources"]
    assert v2["pipeline_baseline_defaults"] == v1["pipeline_baseline_defaults"]


def test_v2_profile_changes_only_the_repaired_pipeline_overrides():
    v1, v2 = _yaml(_V1)["pipeline_overrides"], _yaml(_V2)["pipeline_overrides"]
    assert set(v1) == set(v2)
    changed = {k for k in v1 if v1[k] != v2[k]}
    assert changed == _V2_OVERRIDE_CHANGES


def test_v2_profile_never_lowers_latency_or_assumes_a_rebate():
    v1, v2 = _yaml(_V1)["pipeline_overrides"], _yaml(_V2)["pipeline_overrides"]
    for key in ("submit_ack_latency_ms", "modify_ack_latency_ms", "cancel_ack_latency_ms"):
        assert v2[key] >= v1[key], key
    assert v2["local_decision_pipeline_latency_us"] >= v1["local_decision_pipeline_latency_us"]
    assert v2["maker_fee_bps"] >= 0.0


def test_v2_latency_profile_id_resolves_exactly_and_v1_did_not():
    profiles = load_profiles()
    assert _yaml(_V2)["pipeline_overrides"]["latency_profile_id"] in profiles
    assert _yaml(_V1)["pipeline_overrides"]["latency_profile_id"] not in profiles


def test_v2_profile_loads_with_parity_deferred_and_gate_f_not_applicable():
    profile = load_profile(_V2)
    assert profile.name == "vm_ul6_strict_v2"
    assert profile.deferrable == {"replay_parity": "shadow_exit"}
    assert profile.not_applicable_gates == {"gate_f": "rust_paused"}
    v1 = load_profile(_V1)
    assert v1.deferrable == {}
    assert v1.not_applicable_gates == {}


def _write_profile(tmp_path: Path, **extra) -> Path:
    body = {"name": "p", "is_strict": True, "blocking_sub_gates": ["replay_parity", "min_sample_size"], **extra}
    path = tmp_path / "p.yaml"
    path.write_text(yaml.safe_dump(body))
    return path


@pytest.mark.parametrize(
    ("extra", "match"),
    [
        ({"deferrable": {"replay_parity": "live"}}, "unknown stage"),
        ({"deferrable": {"max_drawdown": "shadow_exit"}}, "not in blocking_sub_gates"),
        ({"deferrable": ["replay_parity"]}, "mapping of strings"),
        ({"not_applicable_gates": {"gate_d": "because"}}, "unknown gate"),
    ],
    ids=["unknown_stage", "non_blocking_gate", "not_a_mapping", "unknown_na_gate"],
)
def test_profile_loader_rejects_invalid_deferral_declarations(tmp_path: Path, extra, match):
    with pytest.raises(ProfileValidationError, match=match):
        load_profile(_write_profile(tmp_path, **extra))


def test_profile_thresholds_win_but_never_loosen_a_gate():
    profile = ValidationProfile(
        name="t",
        is_strict=True,
        thresholds={
            "taker": {
                "sharpe_oos_min": 1.8,  # stricter than the run: wins
                "max_drawdown_pct": 40,  # looser than the run: ignored
                "winning_day_pct_min": 50,  # looser than the run: ignored
                "min_oos_days": 30,  # new key: added
            }
        },
    )
    merged = _merge_profile_thresholds(
        {"sharpe_oos_min": 1.0, "max_drawdown_pct": 20.0, "winning_day_pct_min": 55.0},
        profile,
        strategy_type="taker",
    )
    assert merged == {
        "sharpe_oos_min": 1.8,
        "max_drawdown_pct": 20.0,
        "winning_day_pct_min": 55.0,
        "min_oos_days": 30,
    }
    assert _merge_profile_thresholds({"sharpe_oos_min": 1.0}, None, strategy_type="taker") == {"sharpe_oos_min": 1.0}


def _gate_a_fixture(tmp_path: Path) -> tuple[Path, Path]:
    data_root = tmp_path / "research" / "data" / "raw"
    data_root.mkdir(parents=True, exist_ok=True)
    path = data_root / "feed.npy"
    np.save(path, np.zeros(8, dtype=[("best_bid", "i8"), ("best_ask", "i8"), ("qty", "f8")]))
    meta = {
        "dataset_id": "feed",
        "source_type": "real",
        "owner": "o",
        "schema_version": 1,
        "rows": 8,
        "fields": ["best_bid", "best_ask", "qty"],
    }
    path.with_suffix(path.suffix + ".meta.json").write_text(json.dumps(meta), encoding="utf-8")
    return data_root, path


def _manifest(**extra) -> types.SimpleNamespace:
    return types.SimpleNamespace(
        alpha_id="a", data_fields=("bid_px", "ask_px", "trade_vol"), complexity="O(1)", paper_refs=("120",), **extra
    )


def test_gate_a_checks_provenance_at_high_data_ul(tmp_path: Path):
    """UL6 asks for more evidence than UL1; it must not switch provenance off."""
    data_root, path = _gate_a_fixture(tmp_path)
    cfg = ValidationConfig(
        alpha_id="a",
        data_paths=[str(path)],
        enforce_data_governance=True,
        require_data_meta=True,
        allowed_data_roots=(str(data_root),),
        required_data_provenance_fields=("source", "generator", "seed"),
        data_ul=6,
        enforce_latency_profile=False,
    )
    report = run_gate_a(_manifest(), [str(path)], config=cfg, root=tmp_path)
    assert report.passed is False
    invalid = report.details["data_governance"]["invalid_data_metadata"][str(path)]
    assert "missing_provenance:source" in invalid


@pytest.mark.parametrize(
    ("source_type", "seed_required"),
    [("real", False), ("REAL ", False), ("synthetic", True), (None, True)],
)
def test_gate_a_seed_is_required_except_for_real_data(tmp_path: Path, source_type, seed_required):
    """Recorded market data has no generator seed; every other dataset still needs one."""
    data_root, path = _gate_a_fixture(tmp_path)
    meta_path = path.with_suffix(path.suffix + ".meta.json")
    meta = json.loads(meta_path.read_text())
    meta.update(source="clickhouse", generator="export_l2_ticks", seed=None)
    if source_type is None:
        meta.pop("source_type")
    else:
        meta["source_type"] = source_type
    meta_path.write_text(json.dumps(meta), encoding="utf-8")
    cfg = ValidationConfig(
        alpha_id="a",
        data_paths=[str(path)],
        enforce_data_governance=True,
        require_data_meta=True,
        allowed_data_roots=(str(data_root),),
        required_data_provenance_fields=("source", "generator", "seed"),
        data_ul=6,
        enforce_latency_profile=False,
    )
    report = run_gate_a(_manifest(), [str(path)], config=cfg, root=tmp_path)
    invalid = report.details["data_governance"]["invalid_data_metadata"].get(str(path), [])
    assert ("missing_provenance:seed" in invalid) is seed_required
    assert "missing_provenance:source" not in invalid
    assert "missing_provenance:generator" not in invalid


@pytest.mark.parametrize(("latency_id", "passed"), [("no_such_profile_v0", False), ("sim_p95_v2026-02-26", True)])
def test_gate_a_enforced_latency_profile_must_resolve(tmp_path: Path, latency_id, passed):
    _, path = _gate_a_fixture(tmp_path)
    cfg = ValidationConfig(alpha_id="a", data_paths=[str(path)], enforce_latency_profile=True)
    report = run_gate_a(_manifest(latency_profile=latency_id), [str(path)], config=cfg, root=tmp_path)
    assert report.passed is passed


def test_gate_f_declared_not_applicable_is_reported_as_such(tmp_path: Path):
    profile = ValidationProfile(name="t", is_strict=True, not_applicable_gates={"gate_f": "rust_paused"})
    passed, checks = _evaluate_gate_f(
        PromotionConfig(alpha_id="a", owner="o", enable_rust_readiness_gate=True, validation_profile=profile),
        tmp_path,
    )
    assert passed is True
    assert checks["outcome"] == "not_applicable"
    assert checks["reason"] == "not_applicable:rust_paused"


def test_gate_f_without_declaration_still_requires_a_rust_module(tmp_path: Path):
    profile = ValidationProfile(name="t", is_strict=True)
    passed, checks = _evaluate_gate_f(
        PromotionConfig(alpha_id="a", owner="o", enable_rust_readiness_gate=True, validation_profile=profile),
        tmp_path,
    )
    assert passed is False
    assert checks["checks"]["rust_module_declared"]["pass"] is False


def _run_args(tmp_path: Path, *extra: str):
    return pipeline.build_parser().parse_args(
        ["run", "--alpha-id", "a", "--owner", "o", "--data", "d.npy", "--out-dir", str(tmp_path), *extra]
    )


def test_governed_run_defaults_to_v2_profile_and_hands_it_to_gate_c(monkeypatch, tmp_path: Path, governed_env: Path):
    seen: dict = {}

    def _validation(cfg):
        seen["profile"] = cfg.profile
        seen["run_mode"] = cfg.run_mode
        seen["experiments_dir"] = cfg.experiments_dir
        return types.SimpleNamespace(passed=False, scorecard_path="", to_dict=lambda: {"passed": False})

    monkeypatch.setattr(pipeline, "_run_factory_optimize", lambda *a, **k: 0)
    monkeypatch.setattr(pipeline, "_run_factory_index", lambda *a, **k: 0)
    monkeypatch.setattr(pipeline, "run_alpha_validation", _validation)

    pipeline._run_pipeline(_run_args(tmp_path), mode="strict")

    assert seen["profile"] is not None
    assert seen["profile"].name == "vm_ul6_strict_v2"
    assert seen["run_mode"] == "governed"
    assert seen["experiments_dir"] == str(governed_env)


def test_governed_run_refuses_the_exploratory_standard_profile(tmp_path: Path):
    with pytest.raises(ValueError, match="exploratory only"):
        pipeline._apply_validation_profile(
            _run_args(tmp_path, "--validation-profile", "standard"), strict_mode=True, notes=[]
        )


def test_triage_run_defaults_to_the_standard_profile(tmp_path: Path):
    assert pipeline._apply_validation_profile(_run_args(tmp_path), strict_mode=False, notes=[]) is None
