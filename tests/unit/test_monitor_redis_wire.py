"""Tests for monitor._redis_wire: RESP protocol encoding and decoding."""

from __future__ import annotations

import io

import pytest

from hft_platform.monitor._redis_wire import RedisClient, encode_resp, read_resp


def test_encode_resp_single_command() -> None:
    result = encode_resp("PING")
    assert result == b"*1\r\n$4\r\nPING\r\n"


def test_encode_resp_set_command() -> None:
    result = encode_resp("SET", "key", "value")
    assert result == b"*3\r\n$3\r\nSET\r\n$3\r\nkey\r\n$5\r\nvalue\r\n"


def test_read_resp_simple_string() -> None:
    stream = io.BufferedReader(io.BytesIO(b"+OK\r\n"))
    assert read_resp(stream) == "OK"


def test_read_resp_integer() -> None:
    stream = io.BufferedReader(io.BytesIO(b":42\r\n"))
    assert read_resp(stream) == 42


def test_read_resp_bulk_string() -> None:
    stream = io.BufferedReader(io.BytesIO(b"$5\r\nhello\r\n"))
    assert read_resp(stream) == "hello"


def test_read_resp_null_bulk_string() -> None:
    stream = io.BufferedReader(io.BytesIO(b"$-1\r\n"))
    assert read_resp(stream) is None


def test_read_resp_array() -> None:
    stream = io.BufferedReader(io.BytesIO(b"*2\r\n$3\r\nfoo\r\n$3\r\nbar\r\n"))
    assert read_resp(stream) == ["foo", "bar"]


def test_read_resp_null_array() -> None:
    stream = io.BufferedReader(io.BytesIO(b"*-1\r\n"))
    assert read_resp(stream) is None


def test_read_resp_error_raises() -> None:
    stream = io.BufferedReader(io.BytesIO(b"-ERR unknown command\r\n"))
    with pytest.raises(RuntimeError, match="ERR unknown command"):
        read_resp(stream)


def test_read_resp_empty_raises() -> None:
    stream = io.BufferedReader(io.BytesIO(b""))
    with pytest.raises(RuntimeError, match="empty"):
        read_resp(stream)


def test_read_resp_nested_array() -> None:
    stream = io.BufferedReader(io.BytesIO(b"*2\r\n:1\r\n$3\r\nabc\r\n"))
    result = read_resp(stream)
    assert result == [1, "abc"]


def test_client_connected_property() -> None:
    client = RedisClient(host="127.0.0.1", port=6379)
    assert client.connected is False


def test_client_request_raises_without_connection() -> None:
    client = RedisClient(host="127.0.0.1", port=1)  # invalid port
    with pytest.raises((RuntimeError, OSError)):
        client.request("PING")
