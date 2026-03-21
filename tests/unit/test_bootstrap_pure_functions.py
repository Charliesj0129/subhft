"""Tests for pure functions and simple classes in services/bootstrap.py —
covers _encode_resp, _read_resp, _RoleGuardedNoopClient, _get_runtime_role,
_resolve_broker_id, _read_int_resp, _lease_is_stale."""

from __future__ import annotations

import io

import pytest

import hft_platform.services.bootstrap as bootstrap_mod
import hft_platform.observability.health as health_mod
from hft_platform.services.bootstrap import (
    SystemBootstrapper,
    _encode_resp,
    _read_resp,
    _RoleGuardedNoopClient,
    wait_for_readiness,
)

# ---------------------------------------------------------------------------
# _encode_resp()
# ---------------------------------------------------------------------------


class TestEncodeResp:
    def test_single_part(self) -> None:
        result = _encode_resp("PING")
        assert result == b"*1\r\n$4\r\nPING\r\n"

    def test_multiple_parts(self) -> None:
        result = _encode_resp("SET", "key", "val")
        assert result == b"*3\r\n$3\r\nSET\r\n$3\r\nkey\r\n$3\r\nval\r\n"

    def test_empty_string_part(self) -> None:
        result = _encode_resp("")
        assert result == b"*1\r\n$0\r\n\r\n"

    def test_numeric_part(self) -> None:
        result = _encode_resp("SETEX", "k", "300", "v")
        assert b"$3\r\n300\r\n" in result


# ---------------------------------------------------------------------------
# _read_resp()
# ---------------------------------------------------------------------------


class TestReadResp:
    @staticmethod
    def _stream(data: bytes) -> io.BytesIO:
        return io.BytesIO(data)

    def test_simple_string(self) -> None:
        result = _read_resp(self._stream(b"+OK\r\n"))
        assert result == "OK"

    def test_integer(self) -> None:
        result = _read_resp(self._stream(b":42\r\n"))
        assert result == 42

    def test_bulk_string(self) -> None:
        result = _read_resp(self._stream(b"$5\r\nhello\r\n"))
        assert result == "hello"

    def test_null_bulk_string(self) -> None:
        result = _read_resp(self._stream(b"$-1\r\n"))
        assert result is None

    def test_error(self) -> None:
        with pytest.raises(RuntimeError, match="redis error: ERR unknown"):
            _read_resp(self._stream(b"-ERR unknown\r\n"))

    def test_empty_stream(self) -> None:
        with pytest.raises(RuntimeError, match="empty redis response"):
            _read_resp(self._stream(b""))

    def test_unsupported_prefix(self) -> None:
        with pytest.raises(RuntimeError, match="unsupported redis response prefix"):
            _read_resp(self._stream(b"~unexpected\r\n"))


# ---------------------------------------------------------------------------
# _RoleGuardedNoopClient
# ---------------------------------------------------------------------------


class TestRoleGuardedNoopClient:
    def _make(self, role: str = "monitor") -> _RoleGuardedNoopClient:
        return _RoleGuardedNoopClient(role)

    def test_login_returns_false(self) -> None:
        c = self._make()
        assert c.login() is False

    def test_logged_in_false(self) -> None:
        c = self._make()
        assert c.logged_in is False

    def test_place_order_blocked(self) -> None:
        c = self._make("maintenance")
        result = c.place_order(symbol="2330", price=100, qty=1)
        assert result["status"] == "blocked"
        assert "maintenance" in result["reason"]

    def test_cancel_order_blocked(self) -> None:
        c = self._make()
        result = c.cancel_order(trade=None)
        assert result["status"] == "blocked"

    def test_update_order_blocked(self) -> None:
        c = self._make()
        result = c.update_order(trade=None, price=100.0, qty=1)
        assert result["status"] == "blocked"

    def test_subscribe_basket_stores_callback(self) -> None:
        c = self._make()
        sentinel = object()
        c.subscribe_basket(sentinel)
        assert c.tick_callback is sentinel

    def test_fetch_snapshots_empty(self) -> None:
        c = self._make()
        assert c.fetch_snapshots() == []

    def test_get_positions_empty(self) -> None:
        c = self._make()
        assert c.get_positions() == []

    def test_get_account_balance_blocked(self) -> None:
        c = self._make()
        result = c.get_account_balance()
        assert result["status"] == "blocked"

    def test_close_sets_logged_in_false(self) -> None:
        c = self._make()
        c.logged_in = True
        c.close()
        assert c.logged_in is False

    def test_shutdown_delegates_to_close(self) -> None:
        c = self._make()
        c.logged_in = True
        c.shutdown(logout=True)
        assert c.logged_in is False

    def test_validate_symbols_empty(self) -> None:
        c = self._make()
        assert c.validate_symbols() == []

    def test_get_exchange_empty(self) -> None:
        c = self._make()
        assert c.get_exchange("2330") == ""

    def test_resubscribe_false(self) -> None:
        c = self._make()
        assert c.resubscribe() is False

    def test_reconnect_returns_false(self) -> None:
        c = self._make()
        assert c.reconnect() is False

    def test_reload_symbols_returns_none(self) -> None:
        c = self._make()
        assert c.reload_symbols() is None

    def test_set_execution_callbacks_returns_none(self) -> None:
        c = self._make()
        assert c.set_execution_callbacks(lambda: None, lambda: None) is None

    def test_get_margin_blocked(self) -> None:
        c = self._make()
        result = c.get_margin()
        assert result["status"] == "blocked"

    def test_list_position_detail_empty(self) -> None:
        c = self._make()
        assert c.list_position_detail() == []

    def test_list_profit_loss_empty(self) -> None:
        c = self._make()
        assert c.list_profit_loss() == []

    def test_get_contract_refresh_status_blocked(self) -> None:
        c = self._make()
        result = c.get_contract_refresh_status()
        assert result["status"] == "blocked"


# ---------------------------------------------------------------------------
# SystemBootstrapper._get_runtime_role()
# ---------------------------------------------------------------------------


class TestGetRuntimeRole:
    def _make(self) -> SystemBootstrapper:
        return SystemBootstrapper(settings={})

    def test_default_engine(self, monkeypatch) -> None:
        monkeypatch.delenv("HFT_RUNTIME_ROLE", raising=False)
        assert self._make()._get_runtime_role() == "engine"

    def test_valid_roles(self, monkeypatch) -> None:
        for role in ("engine", "maintenance", "monitor", "wal_loader"):
            monkeypatch.setenv("HFT_RUNTIME_ROLE", role)
            assert self._make()._get_runtime_role() == role

    def test_invalid_role_falls_back_to_engine(self, monkeypatch) -> None:
        monkeypatch.setenv("HFT_RUNTIME_ROLE", "bogus")
        assert self._make()._get_runtime_role() == "engine"

    def test_role_with_whitespace_and_case(self, monkeypatch) -> None:
        monkeypatch.setenv("HFT_RUNTIME_ROLE", "  Monitor  ")
        assert self._make()._get_runtime_role() == "monitor"

    def test_role_with_dashes_converted(self, monkeypatch) -> None:
        monkeypatch.setenv("HFT_RUNTIME_ROLE", "wal-loader")
        assert self._make()._get_runtime_role() == "wal_loader"


# ---------------------------------------------------------------------------
# SystemBootstrapper._resolve_broker_id()
# ---------------------------------------------------------------------------


class TestResolveBrokerId:
    def test_default_shioaji(self, monkeypatch) -> None:
        monkeypatch.delenv("HFT_BROKER", raising=False)
        assert SystemBootstrapper._resolve_broker_id() == "shioaji"

    def test_fubon(self, monkeypatch) -> None:
        monkeypatch.setenv("HFT_BROKER", "fubon")
        assert SystemBootstrapper._resolve_broker_id() == "fubon"

    def test_invalid_raises(self, monkeypatch) -> None:
        monkeypatch.setenv("HFT_BROKER", "unknown_broker")
        with pytest.raises(ValueError, match="Unknown HFT_BROKER"):
            SystemBootstrapper._resolve_broker_id()


# ---------------------------------------------------------------------------
# SystemBootstrapper._read_int_resp()
# ---------------------------------------------------------------------------


class TestReadIntResp:
    def test_int_value(self) -> None:
        assert SystemBootstrapper._read_int_resp(42) == 42

    def test_string_int(self) -> None:
        assert SystemBootstrapper._read_int_resp("100") == 100

    def test_none_returns_default(self) -> None:
        assert SystemBootstrapper._read_int_resp(None) == -2

    def test_garbage_returns_default(self) -> None:
        assert SystemBootstrapper._read_int_resp("abc", default=-99) == -99


# ---------------------------------------------------------------------------
# SystemBootstrapper._lease_is_stale()
# ---------------------------------------------------------------------------


class TestLeaseIsStale:
    def test_key_missing(self) -> None:
        assert SystemBootstrapper._lease_is_stale(ttl_s=-2, takeover_ttl_s=0) is True

    def test_key_no_expire(self) -> None:
        assert SystemBootstrapper._lease_is_stale(ttl_s=-1, takeover_ttl_s=0) is True

    def test_ttl_below_takeover(self) -> None:
        assert SystemBootstrapper._lease_is_stale(ttl_s=10, takeover_ttl_s=30) is True

    def test_ttl_above_takeover(self) -> None:
        assert SystemBootstrapper._lease_is_stale(ttl_s=100, takeover_ttl_s=30) is False

    def test_takeover_zero_not_stale(self) -> None:
        # takeover_ttl_s=0 means stale takeover is disabled
        assert SystemBootstrapper._lease_is_stale(ttl_s=50, takeover_ttl_s=0) is False


@pytest.mark.asyncio
async def test_wait_for_readiness_returns_when_health_server_reports_ready(monkeypatch: pytest.MonkeyPatch) -> None:
    class _FakeHealthServer:
        def __init__(self, system):
            self.system = system

        def _check_readiness(self) -> tuple[bool, dict[str, object]]:
            return True, {"system_running": True, "feed_connected": True}

    async def _no_sleep(_delay: float) -> None:
        return None

    monkeypatch.setattr(health_mod, "HealthServer", _FakeHealthServer)
    monkeypatch.setattr(bootstrap_mod.asyncio, "sleep", _no_sleep)

    await wait_for_readiness(object(), timeout_s=0.01)
