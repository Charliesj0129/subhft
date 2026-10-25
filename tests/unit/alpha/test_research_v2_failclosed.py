"""Research v2 PR3: config resolves from the repo, not the cwd, and fails closed.

Covers ``alpha.paths`` (repo-anchored defaults), exact latency profile lookup
(no prefix matching, a missing file raises), Gate C making a maker run INVALID
instead of running it at instant RTT, audit fallback paths read at write time,
and the RunStamp v0 written into ``meta.json``.
"""

from __future__ import annotations

import hashlib
import json
import types
from pathlib import Path

import numpy as np
import pytest

import research.pipeline as pipeline
from hft_platform.alpha import audit, kill_ledger, paths, sub_gate_audit
from hft_platform.alpha._gate_c import run_gate_c
from hft_platform.alpha._validation_types import ValidationConfig
from hft_platform.alpha.experiments import ExperimentTracker
from hft_platform.alpha.latency_profiles import load_profiles, resolve_profile
from hft_platform.alpha.provenance import RUN_STAMP_SCHEMA
from research.backtest import cost_models

_ROOT = Path(__file__).resolve().parents[3]


@pytest.fixture
def elsewhere(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Run from a directory that has no config/ or research/ of its own."""
    cwd = tmp_path / "elsewhere"
    cwd.mkdir()
    monkeypatch.chdir(cwd)
    return cwd


def test_repo_root_is_the_checkout_whatever_the_cwd(elsewhere: Path):
    assert paths.repo_root() == _ROOT
    assert paths.repo_path("config/research") == _ROOT / "config" / "research"
    assert paths.repo_path(elsewhere / "x") == elsewhere / "x"


def test_repo_root_without_a_checkout_raises(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(paths, "_find_repo_root", lambda start: None)
    with pytest.raises(paths.RepoRootNotFoundError):
        paths.repo_root()
    assert paths.repo_path_or_relative("config/research") == Path("config/research")


def test_research_config_defaults_resolve_from_another_cwd(elsewhere: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(cost_models, "_cache", {})
    assert "r47_maker_shioaji_p95_v2026-04-24_measured" in load_profiles()
    assert cost_models.load_cost_profile("TMF").instrument == "TMF"
    monkeypatch.delenv("HFT_ALPHA_KILL_LEDGER_PATH")
    monkeypatch.delenv("HFT_SUB_GATE_AUDIT_PATH")
    assert kill_ledger._jsonl_path() == _ROOT / "research" / "alphas" / "_kill_ledger.jsonl"
    assert sub_gate_audit._jsonl_path() == _ROOT / "research" / "audit" / "sub_gate_runs.jsonl"
    assert ExperimentTracker().base_dir == _ROOT / "research" / "experiments"


def test_cost_profile_cache_is_keyed_by_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    row = "commission_pts_per_side: {c}\ntax_pts_per_side: 0.1\npoint_value_nwd: 10\n"
    first, second = tmp_path / "a.yaml", tmp_path / "b.yaml"
    first.write_text("TMF:\n  " + row.format(c=1.0).replace("\n", "\n  "))
    second.write_text("TMF:\n  " + row.format(c=2.0).replace("\n", "\n  "))
    monkeypatch.setattr(cost_models, "_cache", {})
    monkeypatch.setattr(cost_models, "_CONFIG_PATH", first)
    assert cost_models.load_cost_profile("TMF").commission_pts_per_side == 1.0
    monkeypatch.setattr(cost_models, "_CONFIG_PATH", second)
    assert cost_models.load_cost_profile("TMF").commission_pts_per_side == 2.0


def test_pipeline_path_defaults_come_from_the_repo_root(elsewhere: Path):
    args = pipeline.build_parser().parse_args(["run", "--alpha-id", "a", "--owner", "o", "--data", "d.npy"])
    root = pipeline._fill_path_defaults(args)
    assert root == _ROOT
    assert args.out_dir == str(_ROOT / "outputs" / "research_pipeline")
    assert args.experiments_dir == str(_ROOT / "research" / "experiments")
    assert args.allowed_data_roots[0] == str(_ROOT / "research" / "data" / "raw")


def test_pipeline_explicit_paths_are_kept(elsewhere: Path):
    args = pipeline.build_parser().parse_args(
        ["run", "--alpha-id", "a", "--owner", "o", "--data", "d.npy", "--project-root", str(elsewhere)]
        + ["--out-dir", "mine", "--experiments-dir", "exp", "--allowed-data-roots", "data"]
    )
    assert pipeline._fill_path_defaults(args) == elsewhere
    assert (args.out_dir, args.experiments_dir, args.allowed_data_roots) == ("mine", "exp", ["data"])


def _profiles() -> dict:
    fields = {
        "submit_ack_latency_ms": 1.0,
        "modify_ack_latency_ms": 1.0,
        "cancel_ack_latency_ms": 1.0,
        "local_decision_pipeline_latency_us": 1,
    }
    return {"shioaji_sim_p95_v2026-03-04": dict(fields), "r47_maker_shioaji_p95_v2026-04-24_measured": dict(fields)}


@pytest.mark.parametrize("prefix", ["shioaji_sim_p95", "r47_maker_shioaji_p95_v2026-04-24"])
def test_latency_profile_prefix_is_not_a_match(prefix: str):
    with pytest.raises(KeyError, match="not found"):
        resolve_profile(prefix, _profiles())


def test_latency_profile_resolves_exact_ids_and_listed_aliases():
    assert resolve_profile("shioaji_sim_p95_v2026-03-04", _profiles())
    assert resolve_profile("sim_p95_v2026-03-04", _profiles())
    assert resolve_profile("v2026-04-24_measured", _profiles())


def test_latency_profiles_file_missing_or_empty_raises(tmp_path: Path):
    with pytest.raises(FileNotFoundError):
        load_profiles(tmp_path / "absent.yaml")
    empty = tmp_path / "empty.yaml"
    empty.write_text("profiles: {}\n")
    with pytest.raises(ValueError, match="no 'profiles' mapping"):
        load_profiles(empty)


class _MakerStrategy:
    def on_tick(self, *_a):
        return None

    def on_fill(self, *_a):
        return None


@pytest.mark.parametrize(
    ("latency_profile", "reason"),
    [
        ("", "latency_profile_missing"),
        ("no_such_profile_v0", "latency_profile_unresolved"),
        ("shioaji_sim_p95", "latency_profile_unresolved"),  # a prefix used to resolve
    ],
)
def test_maker_gate_c_without_usable_latency_is_invalid_before_any_data(tmp_path: Path, latency_profile, reason):
    def _no_clickhouse():
        raise AssertionError("latency must be resolved before any data access")

    def _latency_profile(**kw):
        return types.SimpleNamespace(**kw)

    maker_runtime = types.SimpleNamespace(
        load_cost_profile=None,
        queue_depletion_fill=None,
        clickhouse_source=_no_clickhouse,
        latency_profile=_latency_profile,
        maker_engine=None,
        result_store=None,
    )
    runtime = types.SimpleNamespace(
        hft_native_runner=None,
        ensure_hftbt_npz=None,
        backtest_config=None,
        walk_forward_config=None,
        compute_scorecard=None,
        load_maker_runtime=lambda: maker_runtime,
    )
    manifest = types.SimpleNamespace(
        alpha_id="maker_x", strategy_type="maker", instrument="TMF", latency_profile=latency_profile
    )
    alpha = types.SimpleNamespace(manifest=manifest, create_maker_strategy=_MakerStrategy)

    report, run_id, *_ = run_gate_c(
        alpha, ValidationConfig(alpha_id="maker_x", data_paths=[]), tmp_path, [], tmp_path, runtime=runtime
    )

    assert report.passed is False
    assert report.details["verdict"]["verdict"] == "INVALID"
    assert report.details["reason"] == reason
    assert report.details["gate_c_checks"][0]["outcome"] == "error"
    assert run_id == ""


def test_audit_fallback_dir_env_is_read_at_write_time(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(audit, "_FALLBACK_DIR", None)
    target = tmp_path / "late"
    monkeypatch.setenv("HFT_ALPHA_AUDIT_FALLBACK_DIR", str(target))
    audit._write_fallback("t", {"k": 1})
    assert json.loads((target / "t.jsonl").read_text())["k"] == 1


def test_audit_fallback_default_is_repo_anchored(elsewhere: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(audit, "_FALLBACK_DIR", None)
    monkeypatch.delenv("HFT_ALPHA_AUDIT_FALLBACK_DIR")
    assert audit._fallback_dir() == _ROOT / "research" / "experiments" / ".audit_fallback"


def test_log_run_writes_a_run_stamp_into_meta(tmp_path: Path):
    data = tmp_path / "feed.npy"
    np.save(data, np.arange(4))
    tracker = ExperimentTracker(tmp_path / "exp")
    meta_path = tracker.log_run(
        run_id="r1",
        alpha_id="a",
        config_hash="cfg123",
        data_paths=[str(data)],
        metrics={},
        gate_status={},
        scorecard_payload={},
        backtest_report_payload={},
    )
    stamp = json.loads(meta_path.read_text())["run_stamp"]
    assert stamp["schema"] == RUN_STAMP_SCHEMA
    assert stamp["datasets"] == {str(data): hashlib.sha256(data.read_bytes()).hexdigest()}
    assert stamp["config_hash"] == "cfg123"
    expected = hashlib.sha256((_ROOT / "src/hft_platform/alpha/provenance.py").read_bytes()).hexdigest()
    assert stamp["modules"]["src/hft_platform/alpha/provenance.py"] == expected
    assert len(stamp["modules_sha256"]) == 64
    assert isinstance(stamp["git_commit"], str) and len(stamp["git_commit"]) == 40
    assert isinstance(stamp["git_dirty"], bool)
    assert tracker.list_runs("a")[0].run_stamp == stamp


def test_run_stamp_marks_an_unreadable_dataset(tmp_path: Path):
    tracker = ExperimentTracker(tmp_path / "exp")
    meta_path = tracker.log_run(
        run_id="r2",
        alpha_id="a",
        config_hash="c",
        data_paths=[str(tmp_path / "gone.npy")],
        metrics={},
        gate_status={},
        scorecard_payload={},
        backtest_report_payload={},
    )
    assert json.loads(meta_path.read_text())["run_stamp"]["datasets"] == {str(tmp_path / "gone.npy"): None}


@pytest.mark.parametrize(
    ("latency_profile_id", "reason"),
    [
        ("", "latency_profile_missing"),
        ("no_such_profile_v0", "latency_profile_unresolved"),
        ("shioaji_sim_p95", "latency_profile_unresolved"),  # a prefix
    ],
)
def test_taker_gate_c_without_usable_latency_is_invalid_before_any_data(tmp_path: Path, latency_profile_id, reason):
    def _no_data(*_a, **_k):
        raise AssertionError("latency must be resolved before any data access")

    runtime = types.SimpleNamespace(
        hft_native_runner=_no_data,
        ensure_hftbt_npz=_no_data,
        backtest_config=_no_data,
        walk_forward_config=None,
        compute_scorecard=None,
        load_taker_runtime=_no_data,
    )
    manifest = types.SimpleNamespace(alpha_id="taker_x", strategy_type="taker", instrument="TMF")
    alpha = types.SimpleNamespace(manifest=manifest)
    cfg = ValidationConfig(alpha_id="taker_x", data_paths=["d.npy"], latency_profile_id=latency_profile_id)

    report, run_id, *_ = run_gate_c(alpha, cfg, tmp_path, ["d.npy"], tmp_path, runtime=runtime)

    assert report.passed is False
    assert report.details["engine_type"] == "taker"
    assert report.details["verdict"]["verdict"] == "INVALID"
    assert report.details["reason"] == reason
    assert report.details["gate_c_checks"][0]["name"] == "gate_c.latency_profile"
    assert report.details["gate_c_checks"][0]["outcome"] == "error"
    assert run_id == ""


def test_taker_gate_c_default_latency_profile_id_resolves():
    assert resolve_profile(ValidationConfig(alpha_id="a", data_paths=[]).latency_profile_id, load_profiles())
