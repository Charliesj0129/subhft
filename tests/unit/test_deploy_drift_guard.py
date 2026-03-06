from __future__ import annotations

import importlib.util
import json
import sys
import tarfile
from pathlib import Path
from types import SimpleNamespace


def _load_module():
    script_path = Path(__file__).resolve().parents[2] / "scripts" / "deploy_drift_guard.py"
    spec = importlib.util.spec_from_file_location("deploy_drift_guard", script_path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_parse_compose_ps_supports_array_and_lines():
    mod = _load_module()

    arr = '[{"Service":"hft-engine","State":"running"}]'
    out_arr = mod._parse_compose_ps_json(arr)
    assert len(out_arr) == 1
    assert out_arr[0]["Service"] == "hft-engine"

    lines = '{"Service":"hft-engine","State":"running"}\n{"Service":"redis","State":"running"}\n'
    out_lines = mod._parse_compose_ps_json(lines)
    assert len(out_lines) == 2
    assert out_lines[1]["Service"] == "redis"


def test_collect_included_files_handles_dirs_and_missing(tmp_path):
    mod = _load_module()
    (tmp_path / "config").mkdir()
    (tmp_path / "config" / "a.yaml").write_text("a: 1\n", encoding="utf-8")
    (tmp_path / "docker-compose.yml").write_text("services: {}\n", encoding="utf-8")

    files, missing = mod._collect_included_files(tmp_path, ["config", "docker-compose.yml", "missing.txt"])
    rels = {p.relative_to(tmp_path).as_posix() for p in files}

    assert "config/a.yaml" in rels
    assert "docker-compose.yml" in rels
    assert missing == ["missing.txt"]


def test_compare_snapshots_detects_runtime_and_head_drift():
    mod = _load_module()

    baseline = {
        "git": {
            "available": True,
            "head": "abc",
            "branch": "main",
            "tracked_dirty_count": 0,
            "untracked_count": 0,
        },
        "files": {"combined_sha256": "h1"},
        "compose": {
            "config_sha256": "c1",
            "runtime_env": {
                "hft-engine": {
                    "env_sha256": "e1",
                    "image": "img:1",
                }
            },
        },
    }
    current = {
        "git": {
            "available": True,
            "head": "def",
            "branch": "main",
            "tracked_dirty_count": 0,
            "untracked_count": 0,
        },
        "files": {"combined_sha256": "h1"},
        "compose": {
            "config_sha256": "c1",
            "runtime_env": {
                "hft-engine": {
                    "env_sha256": "e2",
                    "image": "img:1",
                }
            },
        },
    }

    report = mod._compare_snapshots(baseline, current)
    assert report["overall"] == mod.STATUS_FAIL
    assert "hft-engine" in report["runtime_diff"]


def test_compare_snapshots_can_downgrade_to_warn_when_allowed():
    mod = _load_module()

    baseline = {
        "git": {
            "available": True,
            "head": "abc",
            "branch": "main",
            "tracked_dirty_count": 0,
            "untracked_count": 0,
        },
        "files": {"combined_sha256": "h1"},
        "compose": {
            "config_sha256": "c1",
            "runtime_env": {
                "hft-engine": {
                    "env_sha256": "e1",
                    "image": "img:1",
                }
            },
        },
    }
    current = {
        "git": {
            "available": True,
            "head": "abc",
            "branch": "main",
            "tracked_dirty_count": 1,
            "untracked_count": 0,
        },
        "files": {"combined_sha256": "h1"},
        "compose": {
            "config_sha256": "c1",
            "runtime_env": {
                "hft-engine": {
                    "env_sha256": "e2",
                    "image": "img:1",
                }
            },
        },
    }

    report = mod._compare_snapshots(
        baseline,
        current,
        allow_dirty_worktree=True,
        allow_runtime_env_diff=True,
    )
    assert report["overall"] == mod.STATUS_WARN


def test_create_backup_archive_keeps_relative_paths(tmp_path):
    mod = _load_module()

    (tmp_path / "config").mkdir()
    (tmp_path / "config" / "a.yaml").write_text("a: 1\n", encoding="utf-8")
    (tmp_path / "docker-compose.yml").write_text("services: {}\n", encoding="utf-8")

    files, _missing = mod._collect_included_files(tmp_path, ["config", "docker-compose.yml"])
    archive = tmp_path / "backup.tar.gz"
    mod._create_backup_archive(tmp_path, files, archive)

    assert archive.exists()
    with tarfile.open(archive, "r:gz") as tar:
        names = set(tar.getnames())
    assert "config/a.yaml" in names
    assert "docker-compose.yml" in names


def test_parser_supports_snapshot_check_prepare():
    mod = _load_module()
    parser = mod._build_parser()

    s = parser.parse_args(["snapshot", "--label", "baseline"])
    assert s.command == "snapshot"
    assert s.label == "baseline"

    c = parser.parse_args(["check", "--baseline", "outputs/deploy_guard/snapshots/base.json"])
    assert c.command == "check"
    assert c.baseline.endswith("base.json")

    p = parser.parse_args(["prepare", "--change-id", "CHG-20260305-01"])
    assert p.command == "prepare"
    assert p.change_id == "CHG-20260305-01"


def test_prepare_generates_artifacts(tmp_path):
    mod = _load_module()

    project_root = tmp_path / "repo"
    project_root.mkdir()
    (project_root / "config").mkdir()
    (project_root / "config" / "x.yaml").write_text("x: 1\n", encoding="utf-8")
    (project_root / "docker-compose.yml").write_text("services: {}\n", encoding="utf-8")
    (project_root / "Makefile").write_text("all:\n\t@echo ok\n", encoding="utf-8")

    args = SimpleNamespace(
        command="prepare",
        change_id="CHG-1",
        project_root=str(project_root),
        output_dir=str(tmp_path / "out"),
        include_path=["config", "docker-compose.yml", "Makefile"],
        env_prefix=[],
        service=[],
        rollback_tag=None,
        rollback_branch=None,
        create_git_ref=False,
    )

    # Avoid touching local docker/git in unit tests.
    mod._git_snapshot = lambda _root: {"available": False, "error": "mock"}
    mod._compose_snapshot = lambda _root, _prefixes, _services: {
        "errors": ["mock"],
        "ps": [],
        "config_sha256": None,
        "runtime_env": {},
    }

    rc = mod._run_prepare(args)
    assert rc == 0

    out_root = tmp_path / "out" / "pre_sync"
    artifacts = list(out_root.glob("CHG-1_*"))
    assert len(artifacts) == 1
    artifact_dir = artifacts[0]

    assert (artifact_dir / "pre_sync_snapshot.json").exists()
    assert (artifact_dir / "rollback.sh").exists()
    assert (artifact_dir / "change_template.md").exists()

    manifest = json.loads((artifact_dir / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["change_id"] == "CHG-1"
    assert manifest["tracked_files_count"] >= 3
