from __future__ import annotations

import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "research_data_rotate.sh"
DAY = 86400


def _age(path: Path, days: float) -> None:
    stamp = time.time() - days * DAY
    os.utime(path, (stamp, stamp))


def _stale_dir(path: Path, *names: str, days: float = 120) -> Path:
    """A directory whose files and own mtime are all ``days`` old."""
    path.mkdir(parents=True)
    for name in names or ("data.npy",):
        (path / name).write_text("x\n", encoding="utf-8")
        _age(path / name, days)
    _age(path, days)
    return path


def _manifest(root: Path, where: str, alpha: str, status: str) -> None:
    manifest = root / where / alpha / "manifest.yaml"
    manifest.parent.mkdir(parents=True)
    manifest.write_text(f"alpha_id: {alpha}\nstatus: {status}\n", encoding="utf-8")


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    (root / "scripts").mkdir(parents=True)
    shutil.copy(SCRIPT, root / "scripts" / "research_data_rotate.sh")
    data = root / "research" / "data"
    _stale_dir(data / "raw" / "day_old", "ticks.bin")
    (data / "raw" / "day_new").mkdir(parents=True)
    (data / "raw" / "day_new" / "ticks.bin").write_text("x\n", encoding="utf-8")
    processed = data / "processed"
    _stale_dir(processed / "smoke", "smoke_v1.npy")
    _stale_dir(processed / "no_manifest")
    _stale_dir(processed / "early_alpha")
    _manifest(root, "research/alphas", "early_alpha", "prototype")
    _stale_dir(processed / "gate_b_alpha")
    _manifest(root, "research/alphas", "gate_b_alpha", "GATE_B")
    _stale_dir(processed / "shadow_alpha")
    _manifest(root, "research/alphas", "shadow_alpha", "shadow_scaffolded")
    _stale_dir(processed / "broken_manifest")
    (root / "research" / "alphas" / "broken_manifest").mkdir(parents=True)
    (root / "research" / "alphas" / "broken_manifest" / "manifest.yaml").write_text(
        "status: [unclosed\n", encoding="utf-8"
    )
    _stale_dir(processed / "killed_alpha")
    _manifest(root, "research/archive", "killed_alpha", "KILLED")
    _stale_dir(processed / "archived_gate_c")
    _manifest(root, "research/archive", "archived_gate_c", "GATE_C")
    rewritten = _stale_dir(processed / "rewritten_in_place")
    (rewritten / "data.npy").write_text("rewritten today\n", encoding="utf-8")  # directory mtime stays old
    _age(rewritten, 120)
    _stale_dir(data / "synthetic" / "set_a", "old.npy", "new.npy", days=1)
    _age(data / "synthetic" / "set_a" / "old.npy", 5)
    _stale_dir(root / "research" / "experiments" / "runs" / "old_run", "result.json")
    return root


def _run(repo: Path, *args: str) -> subprocess.CompletedProcess[str]:
    env = {
        **os.environ,
        "ROTATE_PYTHON": sys.executable,
        "TEXTFILE_DIR": str(repo / "no-such-textfile-dir"),
    }
    return subprocess.run(
        ["bash", str(repo / "scripts" / "research_data_rotate.sh"), *args],
        cwd=repo,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )


def _present(repo: Path) -> set[str]:
    return {str(p.relative_to(repo)) for p in (repo / "research").rglob("*") if p.is_dir() or p.is_file()}


@pytest.mark.parametrize("args", [(), ("--dry-run",)])
def test_without_apply_nothing_is_archived_deleted_or_created(repo: Path, args: tuple[str, ...]) -> None:
    before = _present(repo)

    proc = _run(repo, *args)

    assert proc.returncode == 0, proc.stderr
    assert _present(repo) == before  # includes the archive directory that --apply would create
    assert "mode: DRY-RUN" in proc.stdout
    assert "[DRY-RUN] rm -rf" in proc.stdout


@pytest.mark.parametrize("args", [("--dryrun",), ("-n",), ("apply",), ("--apply", "--dry-run"), ("--dry-run", "x")])
def test_an_unrecognised_or_extra_argument_is_refused_and_deletes_nothing(repo: Path, args: tuple[str, ...]) -> None:
    before = _present(repo)

    proc = _run(repo, *args)

    assert proc.returncode == 2
    assert "usage:" in proc.stderr
    assert _present(repo) == before


def test_apply_archives_old_raw_data_and_keeps_recent(repo: Path) -> None:
    proc = _run(repo, "--apply")

    assert proc.returncode == 0, proc.stderr
    assert "mode: APPLY" in proc.stdout
    assert not (repo / "research/data/raw/day_old").exists()
    assert (repo / "research/data/raw/day_new/ticks.bin").exists()
    assert (repo / "research/data/archive/day_old.tar.gz").stat().st_size > 0


def test_apply_removes_only_stale_directories_of_alphas_below_gate_b(repo: Path) -> None:
    proc = _run(repo, "--apply")
    processed = repo / "research/data/processed"

    assert proc.returncode == 0, proc.stderr
    assert not (processed / "no_manifest").exists()
    assert not (processed / "early_alpha").exists()
    assert not (processed / "killed_alpha").exists()  # status read from research/archive/
    assert (processed / "gate_b_alpha/data.npy").exists()
    assert (processed / "archived_gate_c/data.npy").exists()  # a manifest under research/archive/ protects too
    assert "PROTECTED" in proc.stdout and "alpha status: GATE_B" in proc.stdout


def test_an_unrecognised_or_unreadable_alpha_status_protects_the_directory(repo: Path) -> None:
    proc = _run(repo, "--apply")
    processed = repo / "research/data/processed"

    assert (processed / "shadow_alpha/data.npy").exists()
    assert "alpha status: SHADOW_SCAFFOLDED" in proc.stdout
    assert (processed / "broken_manifest/data.npy").exists()
    assert "alpha status: UNKNOWN" in proc.stdout


def test_a_file_rewritten_in_place_keeps_a_directory_whose_own_mtime_is_old(repo: Path) -> None:
    proc = _run(repo, "--apply")

    assert (repo / "research/data/processed/rewritten_in_place/data.npy").read_text(
        encoding="utf-8"
    ) == "rewritten today\n"
    assert "a file inside was written within 90 days" in proc.stdout


def test_the_smoke_sample_is_kept_even_when_stale(repo: Path) -> None:
    _run(repo, "--apply")

    assert (repo / "research/data/processed/smoke/smoke_v1.npy").exists()


def test_the_alpha_status_lookup_failing_protects_instead_of_deleting(repo: Path) -> None:
    env = {**os.environ, "ROTATE_PYTHON": "/no/such/python", "TEXTFILE_DIR": str(repo / "none")}

    proc = subprocess.run(
        ["bash", str(repo / "scripts" / "research_data_rotate.sh"), "--apply"],
        cwd=repo,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )

    assert proc.returncode == 0, proc.stderr
    assert (repo / "research/data/processed/no_manifest/data.npy").exists()
    assert "alpha status: UNKNOWN" in proc.stdout


def test_experiment_runs_are_reported_but_never_deleted(repo: Path) -> None:
    proc = _run(repo, "--apply")

    assert proc.returncode == 0, proc.stderr
    assert (repo / "research/experiments/runs/old_run/result.json").exists()
    assert "[REPORT-ONLY]" in proc.stdout and "old_run" in proc.stdout


def test_apply_keeps_only_the_newest_synthetic_file(repo: Path) -> None:
    _run(repo, "--apply")

    synthetic = repo / "research/data/synthetic/set_a"
    assert sorted(p.name for p in synthetic.iterdir()) == ["new.npy"]
