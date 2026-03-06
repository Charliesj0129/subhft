from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from types import SimpleNamespace


def _load_module():
    script_path = Path(__file__).resolve().parents[2] / "scripts" / "release_channel_guard.py"
    spec = importlib.util.spec_from_file_location("release_channel_guard", script_path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")


def _prepare_artifacts(
    tmp_path: Path,
    *,
    change_id: str = "CHG-20260305-02",
    canary_overall: str = "pass",
    canary_trading_days: int = 5,
    drift_overall: str = "pass",
) -> dict[str, Path]:
    root = tmp_path
    output_dir = root / "outputs" / "deploy_guard"
    soak_dir = root / "outputs" / "soak_reports"

    artifact_dir = output_dir / "pre_sync" / f"{change_id}_20260305T010101Z"
    artifact_dir.mkdir(parents=True, exist_ok=True)
    (artifact_dir / f"backup_{change_id}.tar.gz").write_text("backup", encoding="utf-8")
    (artifact_dir / "rollback.sh").write_text("#!/usr/bin/env bash\n", encoding="utf-8")
    (artifact_dir / "change_template.md").write_text("# template\n", encoding="utf-8")

    _write_json(
        artifact_dir / "manifest.json",
        {
            "generated_at": "2026-03-05T01:01:01+00:00",
            "change_id": change_id,
            "artifact_dir": str(artifact_dir.resolve()),
            "backup_tar": f"backup_{change_id}.tar.gz",
            "rollback_script": "rollback.sh",
            "template": "change_template.md",
        },
    )

    _write_json(
        soak_dir / "canary" / "canary_2026-03-05_2026-03-05.json",
        {
            "generated_at": "2026-03-05T02:00:00+00:00",
            "result": {
                "overall": canary_overall,
                "trading_days": canary_trading_days,
            },
        },
    )

    _write_json(
        output_dir / "checks" / "check_20260305T020000Z.json",
        {
            "generated_at": "2026-03-05T02:10:00+00:00",
            "result": {
                "overall": drift_overall,
            },
        },
    )

    return {
        "output_dir": output_dir,
        "soak_dir": soak_dir,
        "manifest": artifact_dir / "manifest.json",
    }


def test_parser_supports_gate_and_promote():
    mod = _load_module()
    parser = mod._build_parser()

    gate = parser.parse_args(["gate", "--change-id", "CHG-1"])
    assert gate.command == "gate"
    assert gate.change_id == "CHG-1"

    promote = parser.parse_args(["promote", "--change-id", "CHG-2", "--apply"])
    assert promote.command == "promote"
    assert promote.change_id == "CHG-2"
    assert promote.apply is True


def test_gate_passes_and_writes_decision(tmp_path: Path):
    mod = _load_module()
    env = _prepare_artifacts(tmp_path)

    args = SimpleNamespace(
        command="gate",
        project_root=str(tmp_path),
        output_dir=str(env["output_dir"]),
        soak_dir=str(env["soak_dir"]),
        change_id="CHG-20260305-02",
        manifest=None,
        canary_report=None,
        drift_report=None,
        min_trading_days=5,
        max_report_age_hours=999999,
        allow_canary_warn=False,
        allow_drift_warn=False,
        allow_warn_exit_zero=False,
    )

    rc = mod._run_gate(args)
    assert rc == 0

    decisions = list((env["output_dir"] / "release_channel" / "decisions").glob("release_gate_*.json"))
    assert len(decisions) == 1
    decision = json.loads(decisions[0].read_text(encoding="utf-8"))
    assert decision["result"]["overall"] == mod.STATUS_PASS
    assert decision["result"]["recommendation"] == "promote"


def test_gate_fails_on_canary_warn_without_allow(tmp_path: Path):
    mod = _load_module()
    env = _prepare_artifacts(tmp_path, canary_overall="warn")

    args = SimpleNamespace(
        command="gate",
        project_root=str(tmp_path),
        output_dir=str(env["output_dir"]),
        soak_dir=str(env["soak_dir"]),
        change_id="CHG-20260305-02",
        manifest=None,
        canary_report=None,
        drift_report=None,
        min_trading_days=5,
        max_report_age_hours=999999,
        allow_canary_warn=False,
        allow_drift_warn=False,
        allow_warn_exit_zero=False,
    )

    rc = mod._run_gate(args)
    assert rc == 2


def test_promote_apply_writes_stable_record(tmp_path: Path):
    mod = _load_module()
    env = _prepare_artifacts(tmp_path)

    args = SimpleNamespace(
        command="promote",
        project_root=str(tmp_path),
        output_dir=str(env["output_dir"]),
        soak_dir=str(env["soak_dir"]),
        change_id="CHG-20260305-02",
        manifest=None,
        canary_report=None,
        drift_report=None,
        min_trading_days=5,
        max_report_age_hours=999999,
        allow_canary_warn=False,
        allow_drift_warn=False,
        apply=True,
        actor="ops-bot",
    )

    rc = mod._run_promote(args)
    assert rc == 0

    promotions = list((env["output_dir"] / "release_channel" / "promotions").glob("stable_*.json"))
    assert len(promotions) == 1
    promotion = json.loads(promotions[0].read_text(encoding="utf-8"))
    assert promotion["result"] == "promoted"
    assert promotion["actor"] == "ops-bot"
