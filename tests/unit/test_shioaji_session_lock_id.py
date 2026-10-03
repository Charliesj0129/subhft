"""The session-lock file name must not carry the account's credentials.

The name used to fall back SHIOAJI_ACCOUNT -> SHIOAJI_PERSON_ID -> SHIOAJI_API_KEY verbatim,
so with SHIOAJI_ACCOUNT unset (the compose default) a national ID, or the first 64
characters of the API key, became a file name under ``.wal/.locks/``.
"""

from __future__ import annotations

import re
from pathlib import Path
from unittest import mock

import pytest

from hft_platform.feed_adapter.shioaji._config import load_shioaji_config, session_lock_id

_FAKE_PERSON_ID = "A" + "1" + "23456789"
_FAKE_API_KEY = "FAKEAPIKEY" + "0123456789abcdef"
_NATIONAL_ID_SHAPE = re.compile(r"[A-Z][12]\d{8}")


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in ("SHIOAJI_ACCOUNT", "SHIOAJI_PERSON_ID", "SHIOAJI_API_KEY"):
        monkeypatch.delenv(name, raising=False)


def test_person_id_fallback_is_not_written_into_the_name(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SHIOAJI_PERSON_ID", _FAKE_PERSON_ID)
    lock_id = session_lock_id()
    assert _FAKE_PERSON_ID not in lock_id
    assert not _NATIONAL_ID_SHAPE.search(lock_id)


def test_api_key_fallback_is_not_written_into_the_name(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SHIOAJI_API_KEY", _FAKE_API_KEY)
    lock_id = session_lock_id()
    assert _FAKE_API_KEY[:8] not in lock_id


def test_hashed_name_is_stable_for_one_credential(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SHIOAJI_PERSON_ID", _FAKE_PERSON_ID)
    assert session_lock_id() == session_lock_id()


def test_hashed_name_differs_between_credentials(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SHIOAJI_PERSON_ID", _FAKE_PERSON_ID)
    first = session_lock_id()
    monkeypatch.setenv("SHIOAJI_PERSON_ID", "B" + "2" + "98765432")
    assert session_lock_id() != first, "two accounts must not share a lock"


def test_account_label_still_wins_and_is_used_as_written(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SHIOAJI_ACCOUNT", "ACC123")
    monkeypatch.setenv("SHIOAJI_PERSON_ID", _FAKE_PERSON_ID)
    assert session_lock_id() == "ACC123"


def test_nothing_set_keeps_the_default_name() -> None:
    assert session_lock_id() == "default"


def test_config_and_client_agree_on_the_lock_path(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """The mutual exclusion only works if every code path derives the same name."""
    monkeypatch.setenv("SHIOAJI_PERSON_ID", _FAKE_PERSON_ID)
    monkeypatch.setenv("SHIOAJI_API_KEY", _FAKE_API_KEY)
    monkeypatch.setenv("SHIOAJI_SECRET_KEY", "FAKE")
    monkeypatch.setenv("HFT_MODE", "sim")
    monkeypatch.setenv("HFT_SHIOAJI_SESSION_LOCK_DIR", str(tmp_path))
    sym = tmp_path / "symbols.yaml"
    sym.write_text("symbols: []")

    cfg = load_shioaji_config()
    with mock.patch("hft_platform.feed_adapter.shioaji.client._sdk", return_value=None):
        from hft_platform.feed_adapter.shioaji.client import ShioajiClient

        client = ShioajiClient(config_path=str(sym))

    assert Path(cfg.session_lock_path).name == Path(client._session_lock_path).name
    assert _FAKE_PERSON_ID not in client._session_lock_path
