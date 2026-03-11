from __future__ import annotations

import io
from unittest.mock import MagicMock, patch

from hft_platform.services.bootstrap import SystemBootstrapper, _encode_resp, _read_resp


def test_build_broker_clients_engine_uses_facade(tmp_path):
    cfg = tmp_path / "symbols.yaml"
    cfg.write_text("symbols:\n  - code: '2330'\n    exchange: 'TSE'\n", encoding="utf-8")
    bootstrapper = SystemBootstrapper({})

    with patch("hft_platform.services.bootstrap.ShioajiClientFacade") as facade_cls:
        md_client, order_client = bootstrapper._build_broker_clients("engine", str(cfg), {}, broker_id="shioaji")

    assert facade_cls.call_count == 2
    assert md_client is facade_cls.return_value
    assert order_client is facade_cls.return_value


def test_build_broker_clients_maintenance_uses_noop(tmp_path):
    cfg = tmp_path / "symbols.yaml"
    cfg.write_text("symbols:\n  - code: '2330'\n    exchange: 'TSE'\n", encoding="utf-8")
    bootstrapper = SystemBootstrapper({})

    with patch("hft_platform.services.bootstrap.ShioajiClientFacade") as facade_cls:
        md_client, order_client = bootstrapper._build_broker_clients("maintenance", str(cfg), {}, broker_id="shioaji")

    assert facade_cls.call_count == 0
    assert md_client.login() is False
    assert order_client.get_exchange("2330") == ""
    assert md_client.place_order("2330", "TSE", "Buy", 100.0, 1)["status"] == "blocked"


def test_runtime_role_normalization():
    bootstrapper = SystemBootstrapper({})
    with patch.dict("os.environ", {"HFT_RUNTIME_ROLE": "wal-loader"}, clear=False):
        assert bootstrapper._get_runtime_role() == "wal_loader"


def test_check_session_ownership_conflict_does_not_override():
    bootstrapper = SystemBootstrapper({})

    class _DummySock:
        def __init__(self):
            # GET -> "other-owner", TTL -> 120
            self._stream = io.BytesIO(b"$11\r\nother-owner\r\n:120\r\n")
            self.sent: list[bytes] = []

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def settimeout(self, timeout):
            return None

        def makefile(self, mode):
            return self._stream

        def sendall(self, payload: bytes):
            self.sent.append(payload)

    dummy_sock = _DummySock()
    conflict_counter = MagicMock()
    metrics = MagicMock(feed_session_conflict_total=conflict_counter)
    conflict_counter.labels.return_value = MagicMock()

    with (
        patch.dict("os.environ", {"HFT_RUNTIME_INSTANCE_ID": "self-owner"}, clear=False),
        patch("hft_platform.services.bootstrap.socket.create_connection", return_value=dummy_sock),
        patch("hft_platform.observability.metrics.MetricsRegistry.get", return_value=metrics),
    ):
        owned = bootstrapper._check_session_ownership("engine")

    assert owned is False
    assert any(b"GET" in cmd for cmd in dummy_sock.sent)
    assert any(b"TTL" in cmd for cmd in dummy_sock.sent)
    assert not any(b"SETEX" in cmd for cmd in dummy_sock.sent)
    conflict_counter.labels.assert_called_once_with(role="engine")
    conflict_counter.labels.return_value.inc.assert_called_once()


def test_check_session_ownership_cleans_stale_owner_and_acquires():
    bootstrapper = SystemBootstrapper({})

    class _DummySock:
        def __init__(self):
            # GET -> owner, TTL -> -1(stale), GET verify owner, DEL -> :1, GET -> nil, SETEX -> +OK
            self._stream = io.BytesIO(b"$11\r\nother-owner\r\n:-1\r\n$11\r\nother-owner\r\n:1\r\n$-1\r\n+OK\r\n")
            self.sent: list[bytes] = []

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def settimeout(self, timeout):
            return None

        def makefile(self, mode):
            return self._stream

        def sendall(self, payload: bytes):
            self.sent.append(payload)

    dummy_sock = _DummySock()
    with (
        patch.dict("os.environ", {"HFT_RUNTIME_INSTANCE_ID": "self-owner"}, clear=False),
        patch("hft_platform.services.bootstrap.socket.create_connection", return_value=dummy_sock),
    ):
        owned = bootstrapper._check_session_ownership("engine")

    assert owned is True
    assert any(b"DEL" in cmd for cmd in dummy_sock.sent)
    assert any(b"SETEX" in cmd for cmd in dummy_sock.sent)


def test_resp_module_level_helpers():
    """OPS-03: Module-level _encode_resp / _read_resp encode/decode correctly."""
    encoded = _encode_resp("SETEX", "mykey", "60", "myval")
    assert encoded == b"*4\r\n$5\r\nSETEX\r\n$5\r\nmykey\r\n$2\r\n60\r\n$5\r\nmyval\r\n"

    # Simple status reply
    ok_stream = io.BytesIO(b"+OK\r\n")
    assert _read_resp(ok_stream) == "OK"

    # Integer reply
    int_stream = io.BytesIO(b":42\r\n")
    assert _read_resp(int_stream) == 42

    # Bulk string reply
    bulk_stream = io.BytesIO(b"$5\r\nhello\r\n")
    assert _read_resp(bulk_stream) == "hello"

    # Null bulk string
    null_stream = io.BytesIO(b"$-1\r\n")
    assert _read_resp(null_stream) is None


def test_lease_refresh_thread_starts_and_stops():
    """OPS-03: _start_lease_refresh_thread sets running=True and spawns alive thread;
    _stop_lease_refresh_thread sets running=False."""
    bootstrapper = SystemBootstrapper({})

    sent_commands: list[bytes] = []

    class _MockSock:
        def __init__(self):
            self._stream = io.BytesIO(b"+OK\r\n" * 20)

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def settimeout(self, t):
            pass

        def makefile(self, mode):
            return self._stream

        def sendall(self, data):
            sent_commands.append(data)

    with patch("hft_platform.services.bootstrap.socket.create_connection", return_value=_MockSock()):
        bootstrapper._start_lease_refresh_thread(
            host="redis",
            port=6379,
            password="",
            key="feed:session:owner",
            owner_id="test-id",
            ttl_s=30,
            timeout_s=0.5,
        )

    assert bootstrapper._lease_refresh_running is True
    assert bootstrapper._lease_refresh_thread is not None
    assert bootstrapper._lease_refresh_thread.is_alive()

    bootstrapper._stop_lease_refresh_thread()
    assert bootstrapper._lease_refresh_running is False
    assert bootstrapper._lease_refresh_thread is None


def test_teardown_sends_del():
    """OPS-03: teardown() sends DEL command to Redis for engine role."""
    bootstrapper = SystemBootstrapper({})
    bootstrapper._last_role = "engine"

    sent_commands: list[bytes] = []

    class _MockSock:
        def __init__(self):
            self._stream = io.BytesIO(b"$10\r\ntest-owner\r\n:1\r\n")

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def settimeout(self, t):
            pass

        def makefile(self, mode):
            return self._stream

        def sendall(self, data):
            sent_commands.append(data)

    with (
        patch.dict("os.environ", {"HFT_RUNTIME_INSTANCE_ID": "test-owner"}, clear=False),
        patch("hft_platform.services.bootstrap.socket.create_connection", return_value=_MockSock()),
    ):
        bootstrapper.teardown()

    assert any(b"DEL" in cmd for cmd in sent_commands)


def test_teardown_skips_del_when_not_owner():
    bootstrapper = SystemBootstrapper({})
    bootstrapper._last_role = "engine"

    sent_commands: list[bytes] = []

    class _MockSock:
        def __init__(self):
            self._stream = io.BytesIO(b"$11\r\nother-owner\r\n")

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def settimeout(self, t):
            pass

        def makefile(self, mode):
            return self._stream

        def sendall(self, data):
            sent_commands.append(data)

    with (
        patch.dict("os.environ", {"HFT_RUNTIME_INSTANCE_ID": "test-owner"}, clear=False),
        patch("hft_platform.services.bootstrap.socket.create_connection", return_value=_MockSock()),
    ):
        bootstrapper.teardown()

    assert any(b"GET" in cmd for cmd in sent_commands)
    assert not any(b"DEL" in cmd for cmd in sent_commands)
