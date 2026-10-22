"""The session-lock file name must not carry the account's credentials.

The name used to fall back SHIOAJI_ACCOUNT -> SHIOAJI_PERSON_ID -> SHIOAJI_API_KEY verbatim,
so with SHIOAJI_ACCOUNT unset (the compose default) a national ID, or the first 64
characters of the API key, became a file name under ``.wal/.locks/``.

A digest of the national ID is no better: about 5e8 valid values, so it is recovered by
enumeration. Only the API key (long and random) may feed the name.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from pathlib import Path
from unittest import mock

import pytest

from hft_platform.feed_adapter.shioaji._config import load_shioaji_config

_FAKE_PERSON_ID = "A" + "1" + "23456789"
_OTHER_FAKE_PERSON_ID = "B" + "2" + "98765432"
_FAKE_API_KEY = "FAKEAPIKEY" + "0123456789abcdef"
_OTHER_FAKE_API_KEY = "OTHERFAKEKEY" + "fedcba9876543210"
_NATIONAL_ID_SHAPE = re.compile(r"[A-Z][12]\d{8}")


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in ("SHIOAJI_ACCOUNT", "SHIOAJI_PERSON_ID", "SHIOAJI_API_KEY"):
        monkeypatch.delenv(name, raising=False)


@pytest.fixture
def lock_id() -> Callable[[], str]:
    # Imported here, not at module level, so that on a tree without the helper the
    # client-level test at the bottom still runs and fails on its assertion.
    from hft_platform.feed_adapter.shioaji._config import session_lock_id

    return session_lock_id


def test_person_id_is_not_written_into_the_name(monkeypatch: pytest.MonkeyPatch, lock_id: Callable[[], str]) -> None:
    monkeypatch.setenv("SHIOAJI_PERSON_ID", _FAKE_PERSON_ID)
    monkeypatch.setenv("SHIOAJI_API_KEY", _FAKE_API_KEY)
    name = lock_id()
    assert _FAKE_PERSON_ID not in name
    assert not _NATIONAL_ID_SHAPE.search(name)


def test_api_key_is_not_written_into_the_name(monkeypatch: pytest.MonkeyPatch, lock_id: Callable[[], str]) -> None:
    monkeypatch.setenv("SHIOAJI_API_KEY", _FAKE_API_KEY)
    assert _FAKE_API_KEY[:8] not in lock_id()


def test_person_id_is_not_an_input_to_the_name(monkeypatch: pytest.MonkeyPatch, lock_id: Callable[[], str]) -> None:
    """A digest of a national ID can be reversed by enumeration, so it must not feed the name."""
    monkeypatch.setenv("SHIOAJI_API_KEY", _FAKE_API_KEY)
    monkeypatch.setenv("SHIOAJI_PERSON_ID", _FAKE_PERSON_ID)
    first = lock_id()
    monkeypatch.setenv("SHIOAJI_PERSON_ID", _OTHER_FAKE_PERSON_ID)
    assert lock_id() == first

    monkeypatch.delenv("SHIOAJI_API_KEY")
    assert lock_id() == "default", "a person id alone must not name the lock"


def test_hashed_name_is_stable_for_one_api_key(monkeypatch: pytest.MonkeyPatch, lock_id: Callable[[], str]) -> None:
    monkeypatch.setenv("SHIOAJI_API_KEY", _FAKE_API_KEY)
    first = lock_id()
    assert first.startswith("id-")
    assert lock_id() == first


def test_hashed_name_differs_between_api_keys(monkeypatch: pytest.MonkeyPatch, lock_id: Callable[[], str]) -> None:
    monkeypatch.setenv("SHIOAJI_API_KEY", _FAKE_API_KEY)
    first = lock_id()
    monkeypatch.setenv("SHIOAJI_API_KEY", _OTHER_FAKE_API_KEY)
    assert lock_id() != first, "two credentials must not share a lock"


def test_blank_api_key_keeps_the_default_name(monkeypatch: pytest.MonkeyPatch, lock_id: Callable[[], str]) -> None:
    monkeypatch.setenv("SHIOAJI_API_KEY", "   ")
    assert lock_id() == "default"


def test_account_label_still_wins_and_is_used_as_written(
    monkeypatch: pytest.MonkeyPatch, lock_id: Callable[[], str]
) -> None:
    monkeypatch.setenv("SHIOAJI_ACCOUNT", "ACC123")
    monkeypatch.setenv("SHIOAJI_API_KEY", _FAKE_API_KEY)
    assert lock_id() == "ACC123"


def test_nothing_set_keeps_the_default_name(lock_id: Callable[[], str]) -> None:
    assert lock_id() == "default"


def test_config_and_client_agree_on_the_lock_path(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """The client's lock path carries no credential, and both derivations agree on it.

    Uses only names that exist on main too, so it fails there on the assertion.
    """
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
    assert _FAKE_API_KEY[:8] not in client._session_lock_path
