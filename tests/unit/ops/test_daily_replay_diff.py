"""Loop_v1 L11 — daily replay diff Prometheus textfile formatter tests."""

from __future__ import annotations

import importlib.util
import json
from datetime import date
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
SCRIPT_PATH = REPO_ROOT / "scripts" / "ops" / "daily_replay_diff.py"


def _load_module():
    spec = importlib.util.spec_from_file_location("daily_replay_diff", SCRIPT_PATH)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def mod():
    return _load_module()


def test_eligible_session_emits_match_pct_gauge(mod):
    report = {
        "eligibility_status": "eligible",
        "match_pct": 99.0,
        "n_live_intents": 10,
        "n_replayed_intents": 10,
        "n_market_events": 6307,
        "first_divergence_idx": None,
    }
    out = mod._format_prom(report, loop_id="r47_tmf_v1", strategy_id="R47_MAKER_TMF", phase="sim")
    assert "# HELP hft_replay_match_pct" in out
    assert "# TYPE hft_replay_match_pct gauge" in out
    assert "hft_replay_match_pct{" in out
    assert 'loop_id="r47_tmf_v1"' in out
    assert 'phase="sim"' in out
    assert 'eligibility="eligible"' in out
    assert "} 99.0000" in out


def test_pre_recorder_session_emits_negative_one_sentinel(mod):
    report = {
        "eligibility_status": "pre_recorder",
        "match_pct": None,
        "n_live_intents": 0,
        "n_replayed_intents": 0,
        "n_market_events": 6307,
        "first_divergence_idx": None,
    }
    out = mod._format_prom(report, loop_id="r47_tmf_v1", strategy_id="R47_MAKER_TMF", phase="sim")
    # match_pct=None should serialize to -1 sentinel.
    assert "} -1.0000" in out
    assert 'eligibility="pre_recorder"' in out
    # first_divergence_idx None should serialize to -1.
    assert "hft_replay_first_divergence_idx" in out
    lines = [line for line in out.splitlines() if line.startswith("hft_replay_first_divergence_idx{")]
    assert lines
    assert lines[0].rstrip().endswith(" -1")


def test_divergence_count_is_replay_minus_live(mod):
    report = {
        "eligibility_status": "eligible",
        "match_pct": 95.0,
        "n_live_intents": 100,
        "n_replayed_intents": 95,
        "n_market_events": 5000,
        "first_divergence_idx": 17,
    }
    out = mod._format_prom(report, loop_id="r47_tmf_v1", strategy_id="R47_MAKER_TMF", phase="shadow")
    div_lines = [line for line in out.splitlines() if line.startswith("hft_replay_divergence_count{")]
    assert len(div_lines) == 1
    assert div_lines[0].rstrip().endswith(" -5")  # 95 - 100


def test_n_intents_emits_both_sources(mod):
    report = {
        "eligibility_status": "eligible",
        "match_pct": 100.0,
        "n_live_intents": 42,
        "n_replayed_intents": 42,
        "n_market_events": 1000,
        "first_divergence_idx": None,
    }
    out = mod._format_prom(report, loop_id="r47_tmf_v1", strategy_id="R47_MAKER_TMF", phase="live")
    intent_lines = [line for line in out.splitlines() if line.startswith("hft_replay_n_intents{")]
    assert len(intent_lines) == 2
    live = [line for line in intent_lines if 'source="live"' in line][0]
    replay = [line for line in intent_lines if 'source="replayed"' in line][0]
    assert live.rstrip().endswith(" 42")
    assert replay.rstrip().endswith(" 42")


def test_phase_label_is_passed_through(mod):
    report = {
        "eligibility_status": "eligible",
        "match_pct": 99.5,
        "n_live_intents": 1,
        "n_replayed_intents": 1,
        "n_market_events": 1,
        "first_divergence_idx": None,
    }
    for phase in ("sim", "shadow", "live"):
        out = mod._format_prom(report, loop_id="r47_tmf_v1", strategy_id="R47_MAKER_TMF", phase=phase)
        assert f'phase="{phase}"' in out


def test_read_report_returns_none_when_missing(mod, tmp_path: Path):
    out_root = tmp_path / "outputs"
    session = date(2026, 5, 5)
    assert mod._read_report(out_root, session) is None


def test_read_report_round_trips(mod, tmp_path: Path):
    session = date(2026, 5, 5)
    out_dir = tmp_path / session.isoformat()
    out_dir.mkdir(parents=True)
    payload = {"match_pct": 99.0, "eligibility_status": "eligible"}
    (out_dir / "report.json").write_text(json.dumps(payload))
    got = mod._read_report(tmp_path, session)
    assert got == payload


def test_resolve_session_date_parses_iso(mod):
    assert mod._resolve_session_date("2026-05-05") == date(2026, 5, 5)


def test_resolve_session_date_rejects_invalid(mod):
    with pytest.raises(ValueError):
        mod._resolve_session_date("2026/05/05")


def test_settings_for_uses_loop_and_strategy(mod):
    s = mod._settings_for("r47_tmf_v1", "R47_MAKER_TMF")
    assert s["loop_id"] == "r47_tmf_v1"
    assert s["strategy"]["id"] == "R47_MAKER_TMF"
    # Default module/class must point at the production R47 strategy unless
    # the operator overrides via env.
    assert "r47_maker" in s["strategy"]["module"]
    assert s["strategy"]["class"] == "R47MakerStrategy"


def test_write_prom_atomic_replace(mod, tmp_path: Path):
    target = tmp_path / "metrics" / "hft_replay.prom"
    mod._write_prom("hft_replay_match_pct{} 99.0\n", str(target))
    assert target.exists()
    assert "hft_replay_match_pct" in target.read_text()
    # Re-write to confirm idempotency / replace semantics.
    mod._write_prom("hft_replay_match_pct{} 100.0\n", str(target))
    assert "100.0" in target.read_text()
    # No leftover .tmp file from atomic replace.
    assert not (target.parent / "hft_replay.prom.tmp").exists()
