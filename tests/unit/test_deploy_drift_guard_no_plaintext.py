"""The drift guard must not persist container env VALUES.

``scripts/deploy_drift_guard.py`` snapshots each container's ``HFT_`` / ``CLICKHOUSE_`` / ``REDIS_`` /
``SHIOAJI_`` / ``SYMBOLS_`` variables to compare a deploy against a baseline. It used to write the values
themselves into ``outputs/deploy_guard`` (snapshots, check reports, the pre-sync snapshot) with default
permissions. These tests drive the real ``snapshot`` / ``check`` / ``prepare`` commands with a fake
``docker`` and assert on the VALUES (distinctive sentinels) in every byte written, not on field names.
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import stat
import sys
import tarfile
from pathlib import Path
from types import SimpleNamespace

import pytest

SECRET_KEY = "sj-key-ZX81QPLMsentinel"
SECRET_PASSWORD = "ch-pw-7Qm2sentinel-A"
SECRET_PASSWORD_ROTATED = "ch-pw-7Qm2sentinel-B"  # same length: a length-only mask could not see the change
CHAT_ID = "-100998877665sentinel"
MODE_VALUE = "sentinel-live-mode"
ENV_FILE_SENTINEL = "dotenv-sentinel-4417"
PLAIN_VALUES = (SECRET_KEY, SECRET_PASSWORD, SECRET_PASSWORD_ROTATED, CHAT_ID, MODE_VALUE, ENV_FILE_SENTINEL)


def _load_module():
    script_path = Path(__file__).resolve().parents[2] / "scripts" / "deploy_drift_guard.py"
    spec = importlib.util.spec_from_file_location("deploy_drift_guard_plain", script_path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _engine_env(password: str = SECRET_PASSWORD) -> dict[str, str]:
    return {
        "SHIOAJI_API_KEY": SECRET_KEY,
        "CLICKHOUSE_PASSWORD": password,
        "HFT_TELEGRAM_CHAT_ID": CHAT_ID,
        "HFT_MODE": MODE_VALUE,
    }


@pytest.fixture
def guard(monkeypatch: pytest.MonkeyPatch):
    mod = _load_module()
    state = {"env": _engine_env()}

    def fake_run_cmd(argv, cwd=None, timeout_s=20):
        if argv[:3] == ["docker", "compose", "ps"]:
            rows = [{"Service": "hft-engine", "Name": "hft-engine-1", "State": "running"}]
            return mod.CmdResult(0, json.dumps(rows), "")
        if argv[:3] == ["docker", "compose", "config"]:
            return mod.CmdResult(0, "services: {}\n", "")
        if argv[:2] == ["docker", "inspect"]:
            env = [f"{k}={v}" for k, v in state["env"].items()] + ["PATH=/usr/bin"]
            return mod.CmdResult(0, json.dumps([{"Config": {"Image": "img:1", "Env": env}}]), "")
        return mod.CmdResult(1, "", "not available in this test")

    monkeypatch.setattr(mod, "_run_cmd", fake_run_cmd)
    mod.state = state
    return mod


@pytest.fixture
def project(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    (root / "config").mkdir(parents=True)
    (root / "config" / "x.yaml").write_text("x: 1\n", encoding="utf-8")
    (root / "docker-compose.yml").write_text("services: {}\n", encoding="utf-8")
    (root / ".env").write_text(f"CLICKHOUSE_PASSWORD={ENV_FILE_SENTINEL}\n", encoding="utf-8")
    return root


def _args(project: Path, out: Path, command: str, **extra) -> SimpleNamespace:
    base = {
        "command": command,
        "project_root": str(project),
        "output_dir": str(out),
        "include_path": ["config", "docker-compose.yml", ".env"],
        "env_prefix": [],
        "service": [],
    }
    base.update(extra)
    return SimpleNamespace(**base)


def _snapshot(guard, project: Path, out: Path, label: str = "base") -> Path:
    assert guard._run_snapshot(_args(project, out, "snapshot", label=label)) == 0
    return sorted((out / "snapshots").glob("*.json"))[-1]


def _check(guard, project: Path, out: Path, baseline: Path) -> tuple[int, dict]:
    args = _args(
        project,
        out,
        "check",
        baseline=str(baseline),
        allow_dirty_worktree=True,
        allow_runtime_env_diff=False,
        allow_warn_exit_zero=False,
    )
    rc = guard._run_check(args)
    report = json.loads(sorted((out / "checks").glob("*.json"))[-1].read_text(encoding="utf-8"))
    return rc, report["result"]


def _text_tree(root: Path) -> str:
    """Every byte the guard wrote, except the backup tarball (it holds ``.env`` on purpose)."""
    parts = []
    for p in sorted(root.rglob("*")):
        if p.is_file() and not p.name.endswith(".tar.gz"):
            parts.append(p.read_text(encoding="utf-8", errors="replace"))
    return "\n".join(parts)


def _runtime_check(result: dict) -> dict:
    return next(c for c in result["checks"] if c["id"] == "runtime_env_same")


def test_snapshot_does_not_persist_env_values(guard, project, tmp_path):
    out = tmp_path / "out"
    snap = _snapshot(guard, project, out)

    written = _text_tree(out)
    for value in PLAIN_VALUES:
        assert value not in written
    # names stay, so a drift report can say WHICH variable moved
    engine = json.loads(snap.read_text(encoding="utf-8"))["compose"]["runtime_env"]["hft-engine"]
    assert set(engine["env_digests"]) == set(_engine_env())
    assert "env" not in engine
    # an unsalted hash of the plaintext lines is brute-forceable for low-entropy values (account ids)
    plain_lines = "\n".join(f"{k}={v}" for k, v in sorted(_engine_env().items()))
    assert engine["env_sha256"] != hashlib.sha256(plain_lines.encode()).hexdigest()


def test_unchanged_env_passes_the_runtime_check(guard, project, tmp_path):
    out = tmp_path / "out"
    baseline = _snapshot(guard, project, out)

    _rc, result = _check(guard, project, out, baseline)

    assert _runtime_check(result)["status"] == guard.STATUS_PASS
    assert result["runtime_diff"] == {}


def test_rotated_secret_is_reported_by_name_without_values(guard, project, tmp_path, capsys):
    out = tmp_path / "out"
    baseline = _snapshot(guard, project, out)
    guard.state["env"] = _engine_env(password=SECRET_PASSWORD_ROTATED)

    rc, result = _check(guard, project, out, baseline)

    assert rc == 2
    assert _runtime_check(result)["status"] == guard.STATUS_FAIL
    diff = result["runtime_diff"]["hft-engine"]
    assert diff["reason"] == "env_diff"
    assert diff["changed"] == ["CLICKHOUSE_PASSWORD"]
    assert diff["added"] == [] and diff["removed"] == []
    shown = _text_tree(out) + capsys.readouterr().out
    for value in PLAIN_VALUES:
        assert value not in shown


def test_added_and_removed_variables_are_reported_by_name(guard, project, tmp_path):
    out = tmp_path / "out"
    baseline = _snapshot(guard, project, out)
    env = _engine_env()
    del env["HFT_MODE"]
    env["HFT_NEW_FLAG"] = "1"
    guard.state["env"] = env

    _rc, result = _check(guard, project, out, baseline)

    diff = result["runtime_diff"]["hft-engine"]
    assert diff["added"] == ["HFT_NEW_FLAG"]
    assert diff["removed"] == ["HFT_MODE"]
    assert diff["changed"] == []


def test_baseline_taken_with_another_digest_key_fails_closed(guard, project, tmp_path):
    out = tmp_path / "out"
    baseline = _snapshot(guard, project, out)
    (out / ".digest_key").unlink()  # the next run mints a different key

    rc, result = _check(guard, project, out, baseline)

    assert rc == 2
    assert result["runtime_diff"]["hft-engine"]["reason"] == "digest_key_differs"
    assert _runtime_check(result)["status"] == guard.STATUS_FAIL


def test_digest_key_is_created_once_and_reused(guard, project, tmp_path):
    out = tmp_path / "out"
    first = json.loads(_snapshot(guard, project, out, "one").read_text(encoding="utf-8"))
    key_text = (out / ".digest_key").read_text(encoding="utf-8").strip()
    second = json.loads(_snapshot(guard, project, out, "two").read_text(encoding="utf-8"))

    engine1 = first["compose"]["runtime_env"]["hft-engine"]
    engine2 = second["compose"]["runtime_env"]["hft-engine"]
    assert engine1["digest_key_id"] == engine2["digest_key_id"]
    assert engine1["env_digests"] == engine2["env_digests"]
    assert len(key_text) == 64
    assert key_text not in _text_tree(out / "snapshots")  # the key itself is never written next to its digests


def _legacy_baseline(path: Path) -> None:
    """The shape the guard wrote before: container env values in plaintext."""
    legacy_env = _engine_env()
    payload = {
        "git": {"available": True, "head": "abc", "branch": "main", "tracked_dirty_count": 0, "untracked_count": 0},
        "files": {"combined_sha256": "h"},
        "compose": {
            "config_sha256": "c",
            "runtime_env": {
                "hft-engine": {
                    "container": "hft-engine-1",
                    "state": "running",
                    "image": "img:1",
                    "env": legacy_env,
                    "env_sha256": hashlib.sha256(
                        "\n".join(f"{k}={v}" for k, v in sorted(legacy_env.items())).encode()
                    ).hexdigest(),
                },
                "wal-loader": {"image": "img:1", "env": {"HFT_MODE": MODE_VALUE}, "env_sha256": "w"},
            },
        },
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload), encoding="utf-8")


def test_legacy_plaintext_baseline_fails_without_echoing_values(guard, project, tmp_path, capsys):
    out = tmp_path / "out"
    legacy = tmp_path / "legacy" / "base.json"
    _legacy_baseline(legacy)

    rc, result = _check(guard, project, out, legacy)

    assert rc == 2
    assert result["runtime_diff"]["hft-engine"]["reason"] == "legacy_plaintext_baseline"
    assert _runtime_check(result)["status"] == guard.STATUS_FAIL
    shown = _text_tree(out) + capsys.readouterr().out
    for value in PLAIN_VALUES:
        assert value not in shown


def test_service_missing_entries_carry_no_env(guard):
    baseline = {
        "git": {"available": True, "head": "a", "branch": "m", "tracked_dirty_count": 0, "untracked_count": 0},
        "files": {"combined_sha256": "h"},
        "compose": {
            "config_sha256": "c",
            "runtime_env": {
                "hft-engine": {"image": "img:1", "env": _engine_env(), "env_sha256": "e", "container": "x"},
            },
        },
    }
    current = {**baseline, "compose": {"config_sha256": "c", "runtime_env": {}}}

    report = guard._compare_snapshots(baseline, current)

    entry = report["runtime_diff"]["hft-engine"]
    assert entry["reason"] == "service_missing"
    assert entry["baseline_present"] is True and entry["current_present"] is False
    rendered = json.dumps(report)
    for value in PLAIN_VALUES:
        assert value not in rendered


def test_outputs_are_private_even_under_a_permissive_umask(guard, project, tmp_path):
    out = tmp_path / "out"
    previous = os.umask(0o000)
    try:
        baseline = _snapshot(guard, project, out)
        _check(guard, project, out, baseline)
        assert (
            guard._run_prepare(
                _args(
                    project,
                    out,
                    "prepare",
                    change_id="CHG-1",
                    rollback_tag=None,
                    rollback_branch=None,
                    create_git_ref=False,
                )
            )
            == 0
        )
    finally:
        os.umask(previous)

    loose = []
    for p in [out, *sorted(out.rglob("*"))]:
        mode = stat.S_IMODE(p.stat().st_mode)
        if mode & 0o077:
            loose.append((p.relative_to(tmp_path).as_posix(), oct(mode)))
    assert loose == []
    assert (out / ".digest_key").is_file()


def test_backup_tarball_keeps_dotenv_for_rollback_and_is_private(guard, project, tmp_path):
    out = tmp_path / "out"
    assert (
        guard._run_prepare(
            _args(
                project,
                out,
                "prepare",
                change_id="CHG-2",
                rollback_tag=None,
                rollback_branch=None,
                create_git_ref=False,
            )
        )
        == 0
    )

    tar_path = next((out / "pre_sync").glob("CHG-2_*/backup_CHG-2.tar.gz"))
    with tarfile.open(tar_path, "r:gz") as tar:
        assert ".env" in tar.getnames()  # rollback fidelity: restoring the tree restores .env
    assert stat.S_IMODE(tar_path.stat().st_mode) & 0o077 == 0
    pre_sync = json.loads(next((out / "pre_sync").glob("CHG-2_*/pre_sync_snapshot.json")).read_text("utf-8"))
    engine = pre_sync["compose"]["runtime_env"]["hft-engine"]
    assert "env" not in engine
    for value in PLAIN_VALUES:
        assert value not in json.dumps(pre_sync)


def test_digest_depends_on_key_and_on_variable_name(guard):
    key_a, key_b = b"a" * 32, b"b" * 32

    assert guard._env_digest(key_a, "CLICKHOUSE_PASSWORD", "v") != guard._env_digest(key_b, "CLICKHOUSE_PASSWORD", "v")
    assert guard._env_digest(key_a, "CLICKHOUSE_PASSWORD", "v") != guard._env_digest(key_a, "REDIS_PASSWORD", "v")
    assert guard._env_digest(key_a, "CLICKHOUSE_PASSWORD", "v") == guard._env_digest(key_a, "CLICKHOUSE_PASSWORD", "v")


def test_corrupt_digest_key_refuses_instead_of_hashing_with_nothing(guard, project, tmp_path, capsys):
    out = tmp_path / "out"
    out.mkdir()
    (out / ".digest_key").write_text("not-hex\n", encoding="utf-8")

    with pytest.raises(SystemExit) as exc:
        guard._run_snapshot(_args(project, out, "snapshot", label="base"))

    assert exc.value.code == 2
    assert "digest key" in capsys.readouterr().out
    assert not (out / "snapshots").exists()


def test_existing_loose_output_file_is_tightened_on_rewrite(guard, tmp_path):
    target = tmp_path / "out" / "report.json"
    target.parent.mkdir()
    target.write_text("{}", encoding="utf-8")
    target.chmod(0o666)

    guard._write_json(target, {"ok": True})

    assert stat.S_IMODE(target.stat().st_mode) == 0o600
