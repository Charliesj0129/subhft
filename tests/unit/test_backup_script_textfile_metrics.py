"""scripts/clickhouse_backup.sh exports each run's outcome through node-exporter's textfile collector.

The backup runs in a one-shot ``docker compose exec`` subprocess, so the gauge
it sets never reaches a scrape target; BackupStale was gated off for that
reason, and the daily backup failed from 2026-10-13 to 2026-10-21 unseen. The
host script now writes ``hft_backup.prom`` after every run. These tests run the
real script with a stub ``docker`` on PATH.
"""

from __future__ import annotations

import os
import shutil
import stat
import subprocess
import time
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "clickhouse_backup.sh"


def _parse(prom: Path) -> dict[str, float]:
    values: dict[str, float] = {}
    for line in prom.read_text().splitlines():
        if line and not line.startswith("#"):
            name, value = line.split()
            values[name] = float(value)
    return values


@pytest.fixture
def run_script(tmp_path: Path):
    project = tmp_path / "project"
    (project / "scripts").mkdir(parents=True)
    shutil.copy(SCRIPT, project / "scripts" / "clickhouse_backup.sh")
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    docker = bin_dir / "docker"
    docker.write_text('#!/usr/bin/env bash\necho "$@" > "$FAKE_DOCKER_ARGS"\nexit "$FAKE_DOCKER_RC"\n')
    docker.chmod(docker.stat().st_mode | stat.S_IXUSR)
    textfile = tmp_path / "textfile"
    textfile.mkdir()

    def _run(docker_rc: int, textfile_dir: Path = textfile) -> subprocess.CompletedProcess[str]:
        env = {
            "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}",
            "HOME": str(tmp_path),
            "FAKE_DOCKER_RC": str(docker_rc),
            "FAKE_DOCKER_ARGS": str(tmp_path / "docker_args"),
            "HFT_BACKUP_TEXTFILE_DIR": str(textfile_dir),
        }
        return subprocess.run(
            ["bash", str(project / "scripts" / "clickhouse_backup.sh")],
            env=env,
            capture_output=True,
            text=True,
            timeout=20,
            check=False,
        )

    _run.textfile = textfile  # type: ignore[attr-defined]  # test helper attribute, read by the tests below
    _run.args_file = tmp_path / "docker_args"  # type: ignore[attr-defined]  # test helper attribute
    return _run


def test_backup_script_exports_success_when_the_backup_succeeds(run_script) -> None:
    before = time.time()
    result = run_script(0)

    assert result.returncode == 0, result.stderr
    values = _parse(run_script.textfile / "hft_backup.prom")
    assert values["hft_backup_cron_last_exit_code"] == 0
    assert before - 1 <= values["hft_backup_cron_last_run_timestamp_seconds"] <= time.time() + 1
    assert (
        values["hft_backup_cron_last_success_timestamp_seconds"] == values["hft_backup_cron_last_run_timestamp_seconds"]
    )
    assert run_script.args_file.read_text().startswith("compose exec -T ")


def test_backup_script_keeps_last_success_when_the_backup_fails(run_script) -> None:
    (run_script.textfile / "hft_backup.prom").write_text(
        "hft_backup_cron_last_run_timestamp_seconds 1000\n"
        "hft_backup_cron_last_exit_code 0\n"
        "hft_backup_cron_last_success_timestamp_seconds 1000\n"
    )

    result = run_script(1)

    assert result.returncode == 1
    values = _parse(run_script.textfile / "hft_backup.prom")
    assert values["hft_backup_cron_last_exit_code"] == 1
    assert values["hft_backup_cron_last_success_timestamp_seconds"] == 1000
    assert values["hft_backup_cron_last_run_timestamp_seconds"] > 1000


def test_backup_script_exports_zero_success_when_none_is_known(run_script) -> None:
    result = run_script(1)

    assert result.returncode == 1
    values = _parse(run_script.textfile / "hft_backup.prom")
    assert values["hft_backup_cron_last_success_timestamp_seconds"] == 0


def test_backup_script_leaves_only_the_prom_file(run_script) -> None:
    run_script(0)
    run_script(1)

    assert sorted(p.name for p in run_script.textfile.iterdir()) == ["hft_backup.prom"]


def test_backup_script_unwritable_textfile_dir_keeps_the_backup_exit_code(run_script, tmp_path: Path) -> None:
    result = run_script(0, textfile_dir=tmp_path / "missing")

    assert result.returncode == 0
    assert "not writable" in result.stderr
    assert not (tmp_path / "missing").exists()
