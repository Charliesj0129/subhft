"""The feed reconnect backstop must outlast the broker client's own budgets.

``MarketDataService._trigger_reconnect`` wraps ``client.reconnect`` in
``asyncio.wait_for(asyncio.to_thread(...))``. That combination cannot cancel the
worker thread -- it abandons the future and lets the reconnect run on. So an
outer bound smaller than the work it wraps does not shorten anything; it only
makes the platform believe a reconnect that is still in progress has failed,
drop the feed to DISCONNECTED, and schedule a competing reconnect against the
one still running.

At the shipped 30s default it did that on every trading day. Measured on the
live engine 2026-08-31..2026-09-07, of 68 Shioaji logins the median was 0.26s
and p90 7.73s, but the five pre-open reconnects at 08:30:0x CST took 44-60s --
all five over the bound. Each logged "Reconnect timed out", went DISCONNECTED,
and was then overtaken by its own login succeeding about 30s later. The
duplicate reconnects that followed are what left the order session
unestablished at the 08:45 open.
"""

from __future__ import annotations

import inspect
import os
from unittest import mock

import pytest


def _client_defaults() -> dict[str, float]:
    """Read the shioaji client's stage budgets from its own source defaults.

    Deliberately does not import a configured client: the point is to pin the
    *defaults that ship*, which is what production runs -- none of these are set
    in the deployed engine's environment.
    """
    from hft_platform.feed_adapter.shioaji import client as sj_client

    src = inspect.getsource(sj_client.ShioajiClient.__init__)
    wanted = {
        "logout": "HFT_SHIOAJI_RECONNECT_TIMEOUT_S",
        "login": "HFT_SHIOAJI_LOGIN_TIMEOUT_S",
        "login_contract": "HFT_SHIOAJI_LOGIN_CONTRACT_TIMEOUT_S",
        "subscribe": "HFT_SHIOAJI_RECONNECT_SUBSCRIBE_TIMEOUT_S",
        "login_retry_max": "HFT_SHIOAJI_LOGIN_RETRY_MAX",
    }
    out: dict[str, float] = {}
    for key, env in wanted.items():
        marker = f'os.getenv("{env}", "'
        assert marker in src, f"{env} default no longer parseable from ShioajiClient.__init__"
        rest = src.split(marker, 1)[1]
        out[key] = float(rest.split('"', 1)[0])
    return out


def _outer_default() -> float:
    """The reconnect backstop ``MarketDataService`` applies with a clean env."""
    from hft_platform.services import market_data

    src = inspect.getsource(market_data.MarketDataService.__init__)
    marker = 'os.getenv("HFT_MD_RECONNECT_TIMEOUT_S", "'
    assert marker in src, "HFT_MD_RECONNECT_TIMEOUT_S default no longer parseable"
    return float(src.split(marker, 1)[1].split('"', 1)[0])


class TestReconnectBackstopOutlastsInnerBudgets:
    """Regression for the daily 08:30 CST false "Reconnect timed out"."""

    def test_backstop_exceeds_the_summed_inner_stage_budgets(self):
        """The wrapper must not expire while a stage inside it may still run."""
        inner = _client_defaults()
        worst_case = (
            inner["logout"]
            + max(inner["login"], inner["login_contract"]) * (1 + inner["login_retry_max"])
            + inner["subscribe"]
        )
        outer = _outer_default()

        assert outer > worst_case, (
            f"reconnect backstop {outer}s must outlast the broker client's own "
            f"stage budgets ({worst_case}s: logout {inner['logout']} + login "
            f"{max(inner['login'], inner['login_contract'])}x"
            f"{1 + int(inner['login_retry_max'])} + subscribe {inner['subscribe']}). "
            "A wrapper that expires first cannot stop the thread -- it only "
            "reports a false failure and starts a competing reconnect."
        )

    def test_backstop_exceeds_every_individual_inner_stage(self):
        """Not even the single longest stage may outlive the wrapper."""
        inner = _client_defaults()
        outer = _outer_default()
        for stage in ("logout", "login", "login_contract", "subscribe"):
            assert outer > inner[stage], f"backstop {outer}s does not outlast stage {stage}={inner[stage]}s"

    def test_backstop_covers_the_measured_pre_open_reconnect(self):
        """60s is the slowest reconnect login observed on the live engine."""
        measured_worst_login_s = 59.98  # 2026-09-07T00:30:04Z, the slowest of 68
        assert _outer_default() > measured_worst_login_s

    def test_the_mixin_fallback_agrees_with_the_service_default(self):
        """A divergent fallback is a second, silently different, budget."""
        from hft_platform.services import _md_reconnect

        src = inspect.getsource(_md_reconnect.MarketDataReconnectMixin._trigger_reconnect)
        marker = 'getattr(self, "reconnect_timeout_s", '
        assert marker in src
        fallback = float(src.split(marker, 1)[1].split(")", 1)[0])
        assert fallback == _outer_default(), "mixin fallback and service default must not diverge"


class TestReconnectBackstopBehaviour:
    """The wrapper must let a slow-but-successful reconnect finish."""

    @staticmethod
    def _service():
        from hft_platform.services.market_data import MarketDataService

        svc = MarketDataService.__new__(MarketDataService)
        svc._last_reconnect_ts = 0.0
        svc.reconnect_cooldown_s = 0.0
        svc.reconnect_timeout_s = _outer_default()
        svc.metrics_registry = None
        svc.lob = None
        svc.feature_engine = None
        svc._on_reconnect_callbacks = []
        svc._resubscribe_attempts = 3
        return svc

    @pytest.mark.asyncio
    async def test_a_reconnect_slower_than_the_old_bound_is_not_reported_failed(self, monkeypatch):
        """A 45s reconnect -- inside the inner budget, past the old 30s -- succeeds."""
        import asyncio

        from hft_platform.services.market_data import FeedState, MarketDataService

        svc = self._service()
        states: list[FeedState] = []
        monkeypatch.setattr(MarketDataService, "_set_state", lambda _s, st: states.append(st))
        monkeypatch.setattr(MarketDataService, "_within_reconnect_window", lambda _s: True)

        client = mock.MagicMock()
        client._apply_pending_resets = mock.MagicMock()
        svc.client = client
        svc.raw_queue = asyncio.Queue()

        elapsed = {"s": 0.0}

        async def _fake_wait_for(aw, timeout):
            # The reconnect takes 45s: past the retired 30s bound, inside both
            # the inner budget and the new backstop.
            elapsed["s"] = 45.0
            aw.close()
            if elapsed["s"] > timeout:
                raise TimeoutError
            return True

        monkeypatch.setattr(asyncio, "wait_for", _fake_wait_for)

        ok = await svc._trigger_reconnect(gap=185399.4, reason="heartbeat_gap")

        assert ok is True
        assert FeedState.DISCONNECTED not in states, "a 45s reconnect must not be reported as failed"
        assert states[-1] is FeedState.CONNECTED

    @pytest.mark.asyncio
    async def test_a_genuinely_hung_reconnect_still_times_out(self, monkeypatch):
        """The backstop is longer, not absent."""
        import asyncio

        from hft_platform.services.market_data import FeedState, MarketDataService

        svc = self._service()
        states: list[FeedState] = []
        monkeypatch.setattr(MarketDataService, "_set_state", lambda _s, st: states.append(st))
        monkeypatch.setattr(MarketDataService, "_within_reconnect_window", lambda _s: True)
        svc.client = mock.MagicMock()

        async def _fake_wait_for(aw, timeout):
            aw.close()
            raise TimeoutError

        monkeypatch.setattr(asyncio, "wait_for", _fake_wait_for)

        ok = await svc._trigger_reconnect(gap=1.0, reason="heartbeat_gap")

        assert ok is False
        assert states[-1] is FeedState.DISCONNECTED


def test_production_does_not_override_the_backstop_env():
    """Documents that the code default is what the deployed engine runs.

    HFT_MD_RECONNECT_TIMEOUT_S is unset on the production engine, so changing
    the default here is what actually changes behaviour there. If a future
    deployment starts setting it, this test is where the assumption is written
    down.
    """
    assert "HFT_MD_RECONNECT_TIMEOUT_S" not in os.environ or float(os.environ["HFT_MD_RECONNECT_TIMEOUT_S"]) > 195.0
