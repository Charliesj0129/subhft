"""Replay parity is blocking at Shadow exit (ADR-003, research v2 PR8).

A Gate C ``replay_parity`` deferred to ``shadow_exit`` reaches promotion as an
obligation. It is discharged only by ``report.json`` files from ``hft run
--mode replay`` that compared this strategy's live intents and matched at least
the obligation's ``min_match_pct``. Until then the canary's weight cannot rise
and it cannot graduate; a typed-in percentage is not evidence. Reports may be
given at promotion (Gate E certifies early) or to ``canary evaluate``.

The reports used here come from the real replay runner.
"""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path

import pytest
import yaml

from hft_platform.alpha import audit, kill_ledger, shadow_parity
from hft_platform.alpha._validation_profile import ValidationProfile
from hft_platform.alpha.canary import CanaryMonitor
from hft_platform.alpha.promotion import PromotionConfig, promote_alpha
from hft_platform.cli._parser import build_parser
from hft_platform.replay.cli_runner import HARNESS_VERSION, run_replay_session
from tests.unit.replay.test_cli_runner import (
    _build_fixture,
    _factory,
    _FakeCKClient,
    _live_intents_canonical,
    _market_rows,
)

_CHECK = "replay_parity_shadow_exit"
_OBLIGATION = {"gate": "replay_parity_audit", "stage": "shadow_exit", "min_match_pct": 95.0, "strategy_id": "ECHO"}
_REPLAY_SETTINGS = {
    "loop_id": "echo_v1",
    "strategy": {"id": "ECHO", "module": "tests.unit.replay.test_cli_runner", "class": "_EchoStrategy"},
}
_LIVE = {
    "slippage_bps": 0.0,
    "drawdown_contribution": 0.0,
    "execution_error_rate": 0.0,
    "sessions_live": 99,
    "sharpe_live": 10.0,
}
_DROP = object()


@pytest.fixture(autouse=True)
def _isolated_ledger(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("HFT_ALPHA_KILL_LEDGER_PATH", str(tmp_path / "_kill_ledger.jsonl"))
    monkeypatch.setenv("HFT_ALPHA_AUDIT_ENABLED", "0")
    audit._ENABLED = None  # noqa: SLF001 -- re-read env on next call
    kill_ledger._reset_cache_for_tests()


def _replay_report(tmp_path: Path, day: date, *, offset: int = 0) -> Path:
    """Run ``hft run --mode replay``'s runner over a fixture with recorded live intents."""
    prices = [1_000_000, 1_000_100, 1_000_200, 1_000_300]
    fixture = _build_fixture(tmp_path / f"wal_{day.isoformat()}.tar.gz", _market_rows(prices))
    out_root = tmp_path / "outputs" / "replay"
    rc = run_replay_session(
        _REPLAY_SETTINGS,
        session_date=day,
        fixture_path=fixture,
        out_root=out_root,
        ck_client=_FakeCKClient(_live_intents_canonical(prices, offset=offset)),
        strategy_factory_override=_factory,
    )
    assert rc == 0
    return out_root / day.isoformat() / "report.json"


def _edited_report(tmp_path: Path, name: str, **changes) -> Path:
    payload = json.loads(_replay_report(tmp_path, date(2026, 4, 21)).read_text())
    for key, value in changes.items():
        if value is _DROP:
            payload.pop(key, None)
        else:
            payload[key] = value
    path = tmp_path / f"{name}.json"
    path.write_text(json.dumps(payload))
    return path


def _scorecard(*, with_parity: bool) -> dict:
    payload = {
        "sharpe_oos": 1.6,
        "max_drawdown": -0.05,
        "turnover": 0.5,
        "correlation_pool_max": 0.3,
        "latency_profile": "sim_p95_v2026-02-26",
        "strategy_type": "taker",
        "cost_uncertainty": {"passed": True, "metrics": {"p95_lower_bound_pts": 0.5}},
    }
    if with_parity:
        payload["replay_parity"] = {"match_pct": 99.0}
    return payload


def _promote(
    tmp_path: Path,
    reports: tuple[Path | str, ...] = (),
    *,
    with_parity: bool = False,
    force: bool = False,
    strategy_id: str | None = "ECHO",
):
    alpha_dir = tmp_path / "research" / "alphas" / "alpha_v"
    alpha_dir.mkdir(parents=True, exist_ok=True)
    sc_path = alpha_dir / "scorecard.json"
    sc_path.write_text(json.dumps(_scorecard(with_parity=with_parity)))
    (alpha_dir / "meta.json").write_text(json.dumps({"run_id": "run-v", "gate_status": {"gate_c": True}}))
    profile = ValidationProfile(
        name="t",
        is_strict=True,
        blocking_sub_gates=("sharpe_threshold", "replay_parity"),
        deferrable={"replay_parity": "shadow_exit"},
        not_applicable_gates={"gate_f": "rust_paused"},
    )
    return promote_alpha(
        PromotionConfig(
            alpha_id="alpha_v",
            owner="charlie",
            project_root=str(tmp_path),
            scorecard_path=str(sc_path),
            min_sharpe_oos=1.0,
            shadow_sessions=5,
            enable_rust_readiness_gate=True,
            validation_profile=profile,
            replay_parity_report_paths=tuple(str(p) for p in reports),
            replay_strategy_id=strategy_id,
            force=force,
            force_reason="test override" if force else "",
        )
    )


def _decision(result) -> dict:
    return json.loads(Path(result.promotion_decision_path).read_text())


def _parity_check(result) -> dict:
    return _decision(result)["gate_e_checks"]["checks"][_CHECK]


def _top_tier_canary(tmp_path: Path, result) -> tuple[Path, CanaryMonitor]:
    """The promoted canary at the top weight tier, where a strong run would graduate."""
    promo_path = Path(result.promotion_config_path)
    payload = yaml.safe_load(promo_path.read_text())
    payload["weight"] = 0.10
    promo_path.write_text(yaml.safe_dump(payload))
    return promo_path, CanaryMonitor(promotions_dir=str(tmp_path / "config" / "strategy_promotions"))


# --- Shadow exit: the canary holds until replay reports certify parity ------


def test_canary_with_live_intents_and_no_parity_report_cannot_graduate(tmp_path: Path):
    result = _promote(tmp_path)
    assert result.approved is True
    _, monitor = _top_tier_canary(tmp_path, result)
    held = monitor.evaluate("alpha_v", _LIVE)
    assert held.state == "canary"
    assert held.checks["unmet_obligations"] == ["replay_parity_audit: no replay parity report"]


def test_canary_does_not_take_a_typed_in_match_pct_as_evidence(tmp_path: Path):
    _, monitor = _top_tier_canary(tmp_path, _promote(tmp_path))
    held = monitor.evaluate("alpha_v", {**_LIVE, "replay_parity_match_pct": 100.0})
    assert held.state == "canary"
    assert held.checks["unmet_obligations"] == ["replay_parity_audit: no replay parity report"]


def test_canary_replay_reports_certify_the_obligation_and_apply_records_it(tmp_path: Path):
    promo_path, monitor = _top_tier_canary(tmp_path, _promote(tmp_path))
    reports = [str(_replay_report(tmp_path, date(2026, 4, 21))), str(_replay_report(tmp_path, date(2026, 4, 22)))]
    status = monitor.evaluate("alpha_v", {**_LIVE, "replay_parity_reports": reports})
    assert status.state == "graduated"
    (certified,) = status.checks["certified_obligations"]
    assert certified["certified_by"] == "canary_evaluate"
    assert [r["session"] for r in certified["reports"]] == ["2026-04-21", "2026-04-22"]
    assert all(len(r["sha256"]) == 64 and r["match_pct"] == 100.0 for r in certified["reports"])

    monitor.apply_decision(status)
    persisted = yaml.safe_load(promo_path.read_text())
    assert persisted["obligations"] == []
    assert persisted["certified_obligations"] == [certified]


def test_canary_escalation_records_the_certification_once(tmp_path: Path):
    result = _promote(tmp_path)
    promo_path = Path(result.promotion_config_path)
    monitor = CanaryMonitor(promotions_dir=str(tmp_path / "config" / "strategy_promotions"))
    report = str(_replay_report(tmp_path, date(2026, 4, 21)))
    first = monitor.evaluate("alpha_v", {**_LIVE, "replay_parity_reports": [report]})
    assert first.state == "escalated"
    monitor.apply_decision(first)
    # Nothing is owed any more: the next step needs no report.
    second = monitor.evaluate("alpha_v", _LIVE)
    assert second.state == "graduated"
    assert "certified_obligations" not in second.checks
    monitor.apply_decision(second)
    persisted = yaml.safe_load(promo_path.read_text())
    assert persisted["obligations"] == []
    assert len(persisted["certified_obligations"]) == 1


def test_canary_divergent_replay_report_holds_the_canary(tmp_path: Path):
    _, monitor = _top_tier_canary(tmp_path, _promote(tmp_path))
    report = str(_replay_report(tmp_path, date(2026, 4, 21), offset=50))
    held = monitor.evaluate("alpha_v", {**_LIVE, "replay_parity_reports": [report]})
    assert held.state == "canary"
    (reason,) = held.checks["unmet_obligations"]
    assert reason.startswith("replay_parity_audit: ")
    assert "below 95.0" in reason


@pytest.mark.parametrize("reports", ["outputs/replay/2026-04-21/report.json", [1, 2]])
def test_canary_malformed_report_list_holds_the_canary(tmp_path: Path, reports):
    _, monitor = _top_tier_canary(tmp_path, _promote(tmp_path))
    held = monitor.evaluate("alpha_v", {**_LIVE, "replay_parity_reports": reports})
    assert held.state == "canary"
    assert held.checks["unmet_obligations"] == ["replay_parity_audit: replay_parity_reports malformed"]


def test_canary_empty_report_list_holds_the_canary(tmp_path: Path):
    _, monitor = _top_tier_canary(tmp_path, _promote(tmp_path))
    held = monitor.evaluate("alpha_v", {**_LIVE, "replay_parity_reports": []})
    assert held.state == "canary"
    assert "no replay parity report" in held.checks["unmet_obligations"][0]


def test_canary_reports_must_name_the_obligation_strategy(tmp_path: Path):
    _, monitor = _top_tier_canary(tmp_path, _promote(tmp_path, strategy_id="OTHER"))
    report = str(_replay_report(tmp_path, date(2026, 4, 21)))
    held = monitor.evaluate("alpha_v", {**_LIVE, "replay_parity_reports": [report]})
    assert held.state == "canary"
    assert "strategy_id 'ECHO' is not 'OTHER'" in held.checks["unmet_obligations"][0]


# --- Promotion: Gate E records the obligation, or certifies it early --------


def test_promotion_without_reports_carries_the_obligation_to_the_canary(tmp_path: Path):
    result = _promote(tmp_path)
    assert result.approved is True
    check = _parity_check(result)
    assert check["outcome"] == "not_applicable"
    assert check["required"] is False
    decision = _decision(result)
    assert decision["obligations"] == [_OBLIGATION]
    assert decision["certified_obligations"] == []
    canary_yaml = yaml.safe_load(Path(result.promotion_config_path).read_text())
    assert canary_yaml["obligations"] == [_OBLIGATION]
    assert canary_yaml["certified_obligations"] == []


def test_promotion_replay_reports_certify_the_obligation_and_free_the_canary(tmp_path: Path):
    reports = (_replay_report(tmp_path, date(2026, 4, 21)), _replay_report(tmp_path, date(2026, 4, 22)))
    result = _promote(tmp_path, reports)
    assert result.approved is True
    assert _parity_check(result)["outcome"] == "pass"
    decision = _decision(result)
    assert decision["obligations"] == []
    (certified,) = decision["certified_obligations"]
    assert certified["certified_by"] == "promotion_gate_e"
    assert [r["session"] for r in certified["reports"]] == ["2026-04-21", "2026-04-22"]

    promo_path, monitor = _top_tier_canary(tmp_path, result)
    assert yaml.safe_load(promo_path.read_text())["certified_obligations"] == decision["certified_obligations"]
    assert monitor.evaluate("alpha_v", _LIVE).state == "graduated"


def test_promotion_divergent_replay_report_fails_gate_e(tmp_path: Path):
    result = _promote(tmp_path, (_replay_report(tmp_path, date(2026, 4, 21), offset=50),))
    assert result.approved is False
    assert result.gate_e_passed is False
    assert result.promotion_config_path is None
    check = _parity_check(result)
    assert check["outcome"] == "fail"
    assert "below 95.0" in check["detail"]


@pytest.mark.parametrize(
    ("changes", "problem"),
    [
        ({"error": "strategy_unbuildable: id='ECHO'"}, "replay error"),
        ({"harness_version": "slice-c.v1"}, "harness 'slice-c.v1'"),
        ({"eligibility_status": "pre_recorder"}, "eligibility 'pre_recorder'"),
        ({"strategy_id": "OTHER"}, "strategy_id 'OTHER' is not 'ECHO'"),
        ({"session": ""}, "no session"),
        # IntentDiff reports 100% when both streams are empty.
        ({"n_live_intents": 0, "n_replayed_intents": 0, "match_pct": 100.0}, "no live intents compared"),
        ({"n_live_intents": True}, "no live intents compared"),
        ({"n_live_intents": _DROP}, "no live intents compared"),
        ({"match_pct": None}, "no numeric match_pct"),
        ({"match_pct": "NaN"}, "no numeric match_pct"),
        ({"match_pct": True}, "no numeric match_pct"),
    ],
)
def test_report_that_cannot_show_parity_fails_closed(tmp_path: Path, changes: dict, problem: str):
    result = _promote(tmp_path, (_edited_report(tmp_path, "edited", **changes),))
    assert result.approved is False
    check = _parity_check(result)
    assert check["outcome"] == "fail"
    assert problem in check["detail"]
    assert _decision(result)["obligations"] == [_OBLIGATION]


@pytest.mark.parametrize(("content", "problem"), [("not json", "unreadable"), ("[1, 2]", "not a JSON object")])
def test_unparseable_report_fails_closed(tmp_path: Path, content: str, problem: str):
    path = tmp_path / "bad.json"
    path.write_text(content)
    check = _parity_check(_promote(tmp_path, (path,)))
    assert check["pass"] is False
    assert problem in check["detail"]


def test_missing_report_file_fails_closed(tmp_path: Path):
    check = _parity_check(_promote(tmp_path, ("outputs/replay/2026-04-21/report.json",)))
    assert check["pass"] is False
    assert "unreadable: FileNotFoundError" in check["detail"]


def test_one_failing_report_fails_the_set(tmp_path: Path):
    good = _replay_report(tmp_path, date(2026, 4, 22))
    bad = _edited_report(tmp_path, "bad", match_pct=90.0)
    result = _promote(tmp_path, (good, bad))
    assert result.approved is False
    assert "match_pct 90.0 below 95.0" in _parity_check(result)["detail"]


def test_the_same_session_twice_fails(tmp_path: Path):
    report = _replay_report(tmp_path, date(2026, 4, 21))
    result = _promote(tmp_path, (report, report))
    assert result.approved is False
    assert "two reports for one session" in _parity_check(result)["detail"]


def test_reports_must_name_the_alpha_when_no_strategy_id_is_given(tmp_path: Path):
    result = _promote(tmp_path, (_replay_report(tmp_path, date(2026, 4, 21)),), strategy_id=None)
    assert result.approved is False
    assert "strategy_id 'ECHO' is not 'alpha_v'" in _parity_check(result)["detail"]


def test_parity_certified_at_gate_d_adds_no_shadow_exit_check(tmp_path: Path):
    result = _promote(tmp_path, with_parity=True)
    assert result.approved is True
    decision = _decision(result)
    assert _CHECK not in decision["gate_e_checks"]["checks"]
    assert decision["obligations"] == []
    assert decision["certified_obligations"] == []


def test_forced_promotion_with_a_failing_report_keeps_the_obligation(tmp_path: Path):
    result = _promote(tmp_path, (_replay_report(tmp_path, date(2026, 4, 21), offset=50),), force=True)
    assert result.approved is True
    forced_yaml = yaml.safe_load(Path(result.promotion_config_path).read_text())
    assert forced_yaml["enabled"] is False
    assert forced_yaml["obligations"] == [_OBLIGATION]
    assert forced_yaml["certified_obligations"] == []


def test_checklist_lists_the_shadow_exit_parity_item(tmp_path: Path):
    owed = {i.label: i for i in _promote(tmp_path).checklist.items}
    item = owed["Gate E: Replay parity (owed to Shadow exit, ADR-003)"]
    assert item.passed is True
    assert "the canary holds" in item.detail


def test_shadow_exit_harness_is_the_replay_runner_harness():
    assert shadow_parity.REPLAY_HARNESS == HARNESS_VERSION


def test_promote_and_canary_cli_collect_repeated_replay_parity_reports():
    parser = build_parser()
    promote = parser.parse_args(
        [
            "alpha",
            "promote",
            "--alpha-id",
            "alpha_v",
            "--owner",
            "charlie",
            "--replay-parity-report",
            "a/report.json",
            "--replay-parity-report",
            "b/report.json",
            "--replay-strategy-id",
            "ECHO",
        ]
    )
    assert promote.replay_parity_report == ["a/report.json", "b/report.json"]
    assert promote.replay_strategy_id == "ECHO"
    canary = parser.parse_args(
        ["alpha", "canary", "evaluate", "--alpha-id", "alpha_v", "--replay-parity-report", "a/report.json"]
    )
    assert canary.replay_parity_report == ["a/report.json"]
