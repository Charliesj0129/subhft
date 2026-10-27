"""``contract_fetcher._login_shioaji`` against the shioaji 1.7 contract load.

From 1.7 ``login()`` loads contracts itself and an explicit
``fetch_contracts`` issued meanwhile fails at once with "exclusive access
lost" (live probe 2026-10-27). The old helper called it straight after login
and let the error escape, so ``hft symbols`` could not log in on 1.7.9.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from hft_platform.feed_adapter import contract_fetcher
from tests.unit.shioaji_contracts_helper import FakeClock, Sdk179ContractsApi


def _login(monkeypatch: pytest.MonkeyPatch, load_at_s: float) -> Sdk179ContractsApi:
    clock = FakeClock()
    api = Sdk179ContractsApi(clock, load_at_s=load_at_s)
    monkeypatch.setenv("SHIOAJI_API_KEY", "k")
    monkeypatch.setenv("SHIOAJI_SECRET_KEY", "s")
    monkeypatch.setattr(contract_fetcher, "sj", SimpleNamespace(Shioaji=lambda simulation: api))
    real_wait = contract_fetcher.wait_contracts_ready
    monkeypatch.setattr(
        contract_fetcher,
        "wait_contracts_ready",
        lambda *a, **kw: real_wait(*a, clock=clock, sleep=clock.sleep, **kw),
    )
    assert contract_fetcher._login_shioaji() is api
    return api


def test_contract_fetcher_waits_instead_of_racing_login(monkeypatch: pytest.MonkeyPatch) -> None:
    api = _login(monkeypatch, load_at_s=0.4)

    assert api.calls == ["login"]


def test_contract_fetcher_survives_exclusive_access_lost_while_loading(monkeypatch: pytest.MonkeyPatch) -> None:
    api = _login(monkeypatch, load_at_s=45.0)

    assert api.calls == ["login", "fetch_contracts"]


def test_contract_fetcher_fails_loudly_when_contracts_never_load(monkeypatch: pytest.MonkeyPatch) -> None:
    with pytest.raises(RuntimeError, match="contracts not loaded"):
        _login(monkeypatch, load_at_s=float("inf"))
