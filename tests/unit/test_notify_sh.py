"""``scripts/_notify.sh`` says when a notification was not sent (N93).

``notify_telegram`` returned 0 and printed nothing when the credentials were missing, and swallowed every curl failure
(``|| true`` with stderr sent to /dev/null). A governance script then reported "notified" with nobody told. The helper
still returns 0 by default (callers run under ``set -e``); it now writes the reason to stderr, never the token, and
``NOTIFY_STRICT=1`` turns the failure into a non-zero return.

A fake ``curl`` earlier on PATH stands in for the network; nothing here talks to Telegram.
"""

from __future__ import annotations

import os
import stat
import subprocess
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "_notify.sh"
TOKEN = "123456:SECRET-TOKEN-VALUE"
CHAT = "-100777"

_FAKE_CURL = """#!/usr/bin/env bash
printf '%s\\n' "$*" >> "$FAKE_CURL_LOG"
printf '%s' "${FAKE_CURL_STATUS-200}"
exit "${FAKE_CURL_EXIT-0}"
"""


@pytest.fixture()
def sandbox(tmp_path):
    bindir = tmp_path / "bin"
    bindir.mkdir()
    curl = bindir / "curl"
    curl.write_text(_FAKE_CURL)
    curl.chmod(curl.stat().st_mode | stat.S_IXUSR)
    deploy = tmp_path / "deploy"
    deploy.mkdir()
    return tmp_path, bindir, deploy


def _run(sandbox, *, env_extra=None, message="hello") -> tuple[subprocess.CompletedProcess, Path]:
    tmp_path, bindir, deploy = sandbox
    log = tmp_path / "curl.log"
    env = {
        "PATH": f"{bindir}:/usr/bin:/bin",
        "HOME": str(tmp_path),
        "DEPLOY_ROOT": str(deploy),
        "FAKE_CURL_LOG": str(log),
    }
    env.update(env_extra or {})
    result = subprocess.run(
        ["bash", "-c", f'source "{SCRIPT}"; notify_telegram "{message}"'],
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    return result, log


def _creds() -> dict[str, str]:
    return {"HFT_TELEGRAM_BOT_TOKEN": TOKEN, "HFT_TELEGRAM_CHAT_ID": CHAT}


def test_missing_credentials_are_reported_on_stderr_and_curl_is_not_called(sandbox):
    result, log = _run(sandbox)

    assert result.returncode == 0
    assert "NOT sent" in result.stderr
    assert "HFT_TELEGRAM_BOT_TOKEN" in result.stderr
    assert not log.exists(), "no request may be attempted without credentials"


def test_missing_credentials_name_the_deploy_root_when_there_is_no_env_file(sandbox):
    _, _, deploy = sandbox

    result, _ = _run(sandbox)

    assert f"no .env at {deploy}" in result.stderr


def test_missing_credentials_return_non_zero_in_strict_mode(sandbox):
    result, _ = _run(sandbox, env_extra={"NOTIFY_STRICT": "1"})

    assert result.returncode == 1
    assert "NOT sent" in result.stderr


def test_a_delivered_message_is_silent_and_returns_zero(sandbox):
    result, log = _run(sandbox, env_extra=_creds(), message="deploy done")

    assert result.returncode == 0
    assert result.stderr == ""
    sent = log.read_text()
    assert "sendMessage" in sent
    assert f"chat_id={CHAT}" in sent
    assert "text=deploy done" in sent


@pytest.mark.parametrize("status", ["401", "400", "500", ""])
def test_an_http_failure_is_reported_with_its_status_and_never_with_the_token(sandbox, status):
    result, _ = _run(sandbox, env_extra={**_creds(), "FAKE_CURL_STATUS": status})

    assert result.returncode == 0, "non-strict callers must keep running"
    assert "NOT delivered" in result.stderr
    assert f"HTTP {status or 'none'}" in result.stderr
    assert TOKEN not in result.stderr
    assert "api.telegram.org" not in result.stderr


def test_a_curl_failure_is_reported_with_its_exit_code(sandbox):
    result, _ = _run(sandbox, env_extra={**_creds(), "FAKE_CURL_EXIT": "6", "FAKE_CURL_STATUS": "000"})

    assert result.returncode == 0
    assert "curl exit 6" in result.stderr
    assert TOKEN not in result.stderr


def test_a_failed_send_returns_non_zero_in_strict_mode(sandbox):
    result, _ = _run(sandbox, env_extra={**_creds(), "FAKE_CURL_STATUS": "401", "NOTIFY_STRICT": "1"})

    assert result.returncode == 1
    assert "HTTP 401" in result.stderr


def test_a_delivered_message_stays_zero_in_strict_mode(sandbox):
    result, _ = _run(sandbox, env_extra={**_creds(), "NOTIFY_STRICT": "1"})

    assert result.returncode == 0
    assert result.stderr == ""


def test_credentials_in_the_deploy_root_env_file_are_picked_up(sandbox):
    _, _, deploy = sandbox
    (deploy / ".env").write_text(f"HFT_TELEGRAM_BOT_TOKEN={TOKEN}\nHFT_TELEGRAM_CHAT_ID={CHAT}\nOTHER=1\n")

    result, log = _run(sandbox)

    assert result.returncode == 0
    assert result.stderr == ""
    assert f"chat_id={CHAT}" in log.read_text()


def test_the_fake_curl_is_the_one_that_ran(sandbox):
    """Guards the harness: PATH must resolve ``curl`` to the stand-in, or the cases above would hit the network."""
    _, bindir, _ = sandbox
    resolved = subprocess.run(
        ["bash", "-c", "command -v curl"],
        env={"PATH": f"{bindir}:/usr/bin:/bin"},
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()

    assert resolved == str(bindir / "curl")
    assert os.access(resolved, os.X_OK)
