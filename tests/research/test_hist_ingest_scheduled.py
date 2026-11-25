from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[2] / "research/data_pipeline/hist_ingest/run_scheduled.sh"


def run_job(tmp_path, job, *, ingest_status=0, output="ingest_done calls=1", ready_status=0, docker_status=0):
    root = tmp_path / "repo"
    target = root / "research/data_pipeline/hist_ingest/run_scheduled.sh"
    target.parent.mkdir(parents=True)
    target.write_text(SCRIPT.read_text())
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    python = root / ".venv/bin/python"
    python.parent.mkdir(parents=True)
    python.write_text("""#!/usr/bin/env bash
printf '%s\\n' "$*" >> "$FAKE_CALLS"
if [[ "$*" == *hist_ingest.readiness* ]]; then
  if [[ "$FAKE_READY_STATUS" != 0 ]]; then
    echo 'clickhouse_not_ready: HTTP /ping unavailable after 120s' >&2
  fi
  exit "$FAKE_READY_STATUS"
fi
echo "$FAKE_OUTPUT"
exit "$FAKE_INGEST_STATUS"
""")
    python.chmod(0o755)
    docker = bin_dir / "docker"
    docker.write_text(
        '#!/usr/bin/env bash\n[[ "$FAKE_DOCKER_STATUS" == 0 ]] || exit "$FAKE_DOCKER_STATUS"\necho clickhouse\n'
    )
    docker.chmod(0o755)
    sleep = bin_dir / "sleep"
    sleep.write_text("#!/usr/bin/env bash\nexit 99\n")
    sleep.chmod(0o755)
    calls, logs = tmp_path / "calls", tmp_path / "logs"
    env_file = tmp_path / "fake.env"
    env_file.write_text("HFT_CLICKHOUSE_HOST=fake-db\nHFT_CLICKHOUSE_PORT=18123\n")
    env = {
        "PATH": f"{bin_dir}:{os.defpath}",
        "HOME": str(tmp_path),
        "HIST_LOG_DIR": str(logs),
        "HIST_ENV_FILE": str(env_file),
        "HIST_MAX_ROUNDS": "1",
        "FAKE_CALLS": str(calls),
        "FAKE_OUTPUT": output,
        "FAKE_INGEST_STATUS": str(ingest_status),
        "FAKE_READY_STATUS": str(ready_status),
        "FAKE_DOCKER_STATUS": str(docker_status),
    }
    result = subprocess.run(["bash", str(target), job], env=env, capture_output=True, text=True, timeout=10)
    log = "\n".join(p.read_text() for p in logs.glob("*.log"))
    return result, calls.read_text().splitlines() if calls.exists() else [], log, env_file


def test_scheduled_readiness_precedes_both_fubon_invocations(tmp_path):
    result, calls, _, env_file = run_job(tmp_path, "fubon-kbars")
    assert result.returncode == 0
    assert len(calls) == 3
    assert "hist_ingest.readiness" in calls[0]
    assert all(f"--env-file {env_file}" in call for call in calls)
    assert "--products TXF,MXF --since 2017-06-01" in calls[1]
    assert "--products TMF --since 2024-07-29" in calls[2]


def test_scheduled_readiness_failure_stops_before_ingestion(tmp_path):
    result, calls, log, _ = run_job(tmp_path, "shioaji-ticks", ready_status=1)
    assert result.returncode == 1
    assert len(calls) == 1
    assert "hist_ingest.readiness" in calls[0]
    assert log.splitlines() == ["clickhouse_not_ready: HTTP /ping unavailable after 120s"]
    assert "Traceback" not in result.stderr + log


def test_scheduled_readiness_waits_despite_docker_desktop_startup_lag(tmp_path):
    result, calls, _, _ = run_job(tmp_path, "fubon-kbars", docker_status=1)
    assert result.returncode == 0
    assert len(calls) == 3
    assert "hist_ingest.readiness" in calls[0]


@pytest.mark.parametrize(
    "output", ["Traceback: Connection refused localhost:8123", "connect_failed error=login_failed", "ingest_aborted"]
)
def test_scheduled_ingest_failure_returns_nonzero_without_quota_classification(tmp_path, output):
    result, calls, log, _ = run_job(tmp_path, "shioaji-ticks", ingest_status=1, output=output)
    assert result.returncode == 1
    assert len(calls) == 2
    assert "stop: quota" not in log
    assert "stop: ingest failed" in log


def test_scheduled_quota_guard_stops_successful_round_without_other_products(tmp_path):
    result, calls, log, _ = run_job(tmp_path, "shioaji-ticks", output="ingest_done PENDING_QUOTA=1")
    assert result.returncode == 0
    assert len(calls) == 2
    assert "stop: quota" in log


def test_scheduled_filtered_sdk_output_keeps_successful_fubon_invocations(tmp_path):
    result, calls, log, _ = run_job(tmp_path, "fubon-kbars", output="Response Code: 0\nInfo: ready")
    assert result.returncode == 0
    assert len(calls) == 3
    assert "Response Code" not in log
    assert "Info:" not in log
    assert "end fubon-kbars" in log
