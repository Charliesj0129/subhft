"""Shioaji SDK test doubles shared across login tests."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any


def mark_contracts_loaded(api: Any) -> Any:
    """Give a mock ``api`` the contracts a finished login load leaves behind.

    Login waits until Futures, Options and Stocks each yield a contract (see
    ``_compat.contracts_loaded``); a bare ``MagicMock`` category yields nothing,
    so without this a mocked login waits out the whole contract budget.
    """
    api.Contracts.Futures = [SimpleNamespace(code="TMFL6")]
    api.Contracts.Options = [SimpleNamespace(code="TX118000L6")]
    api.Contracts.Stocks = [SimpleNamespace(code="2330")]
    return api


class FakeClock:
    """Monotonic clock whose ``sleep`` advances time instead of blocking."""

    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.now += seconds


class Sdk179ContractsApi:
    """shioaji 1.7.x login/contracts behaviour as seen live on 2026-10-27.

    ``login()`` starts the contract load itself; the contracts become visible at
    ``load_at_s`` on ``clock``. Until then an explicit ``fetch_contracts`` fails
    at once with "exclusive access lost", as the probe recorded. The signatures
    match the 1.7 stub strictly: ``login()`` takes no contract arguments.
    """

    def __init__(self, clock: FakeClock, load_at_s: float) -> None:
        self._clock = clock
        self._load_at_s = load_at_s
        self._loaded = SimpleNamespace()
        mark_contracts_loaded(SimpleNamespace(Contracts=self._loaded))
        self._empty = SimpleNamespace(Futures=[], Options=[], Stocks=[])
        self.calls: list[str] = []

    def login(self, api_key, secret_key, subscribe_trade=True, receive_window=30000, force_refresh=False):
        self.calls.append("login")
        return []

    def fetch_contracts(self, contract_download=False, contracts_timeout=30000, contracts_cb=None):
        self.calls.append("fetch_contracts")
        if self._clock() < self._load_at_s:
            raise RuntimeError("fetch_contracts: exclusive access lost (concurrent API call started)")
        return self._loaded

    @property
    def Contracts(self) -> Any:  # noqa: N802 - SDK attribute name
        return self._loaded if self._clock() >= self._load_at_s else self._empty
