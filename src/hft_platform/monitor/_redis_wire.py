"""Minimal Redis RESP client for monitor live cache I/O."""

from __future__ import annotations

import os
import socket
from io import IOBase
from typing import Any

_DEFAULT_TIMEOUT_S = float(os.getenv("HFT_MONITOR_REDIS_TIMEOUT_S", "5.0"))


def encode_resp(*parts: str) -> bytes:
    payload = [f"*{len(parts)}\r\n".encode("utf-8")]
    for part in parts:
        raw = str(part).encode("utf-8")
        payload.append(f"${len(raw)}\r\n".encode("utf-8"))
        payload.append(raw + b"\r\n")
    return b"".join(payload)


def read_resp(stream) -> Any:
    prefix = stream.read(1)
    if not prefix:
        raise RuntimeError("empty redis response")
    if prefix == b"+":
        return stream.readline().rstrip(b"\r\n").decode("utf-8", errors="replace")
    if prefix == b":":
        return int(stream.readline().rstrip(b"\r\n").decode("utf-8", errors="replace"))
    if prefix == b"$":
        size = int(stream.readline().rstrip(b"\r\n").decode("utf-8", errors="replace"))
        if size < 0:
            return None
        payload = stream.read(size)
        stream.read(2)
        return payload.decode("utf-8", errors="replace")
    if prefix == b"*":
        count = int(stream.readline().rstrip(b"\r\n").decode("utf-8", errors="replace"))
        if count < 0:
            return None
        return [read_resp(stream) for _ in range(count)]
    if prefix == b"-":
        err = stream.readline().rstrip(b"\r\n").decode("utf-8", errors="replace")
        raise RuntimeError(f"redis error: {err}")
    raise RuntimeError(f"unsupported redis response prefix: {prefix!r}")


class RedisClient:
    """Tiny blocking Redis client used by monitor/TUI live cache."""

    __slots__ = ("host", "port", "password", "timeout_s", "_sock", "_stream")

    def __init__(self, host: str, port: int, password: str = "", timeout_s: float = _DEFAULT_TIMEOUT_S) -> None:
        self.host = host
        self.port = int(port)
        self.password = password
        self.timeout_s = float(timeout_s)
        self._sock: socket.socket | None = None
        self._stream: IOBase | None = None

    @property
    def connected(self) -> bool:
        return self._sock is not None

    def connect(self) -> None:
        if self._sock is not None:
            return
        sock = socket.create_connection((self.host, self.port), timeout=self.timeout_s)
        sock.settimeout(self.timeout_s)
        stream = sock.makefile("rwb", buffering=4096)
        self._sock = sock
        self._stream = stream
        if self.password:
            self.request("AUTH", self.password)

    def close(self) -> None:
        try:
            if self._stream is not None:
                self._stream.close()
        finally:
            self._stream = None
            if self._sock is not None:
                try:
                    self._sock.close()
                finally:
                    self._sock = None

    def request(self, *parts: str) -> Any:
        self.connect()
        if self._stream is None:
            raise RuntimeError("redis stream not available")
        try:
            self._stream.write(encode_resp(*parts))
            self._stream.flush()
            return read_resp(self._stream)
        except (socket.error, ConnectionResetError, BrokenPipeError, OSError) as exc:
            self.close()
            raise RuntimeError(f"redis connection lost: {exc}") from exc

    def pipeline(self, *commands: tuple[str, ...]) -> list[Any]:
        """Send multiple commands in a single write, read all responses."""
        self.connect()
        if self._stream is None:
            raise RuntimeError("redis stream not available")
        try:
            self._stream.write(b"".join(encode_resp(*cmd) for cmd in commands))
            self._stream.flush()
            return [read_resp(self._stream) for _ in commands]
        except (socket.error, ConnectionResetError, BrokenPipeError, OSError) as exc:
            self.close()
            raise RuntimeError(f"redis connection lost: {exc}") from exc
