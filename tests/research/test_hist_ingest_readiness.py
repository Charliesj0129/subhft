from __future__ import annotations

import importlib
import sys
from types import SimpleNamespace

import pytest

from research.data_pipeline.hist_ingest import store


def readiness_module():
    return importlib.import_module("research.data_pipeline.hist_ingest.readiness")


class FakeClock:
    def __init__(self):
        self.now = 0.0
        self.sleeps = []

    def clock(self):
        return self.now

    def sleep(self, delay):
        self.sleeps.append(delay)
        self.now += delay


def test_readiness_waits_for_http_after_startup_lag():
    readiness = readiness_module()
    clock, attempts = FakeClock(), []

    def ping(host, port, timeout):
        attempts.append((host, port, timeout, clock.now))
        return clock.now >= 35

    assert readiness.wait_for_clickhouse("hist-db", 18123, ping=ping, clock=clock.clock, sleep=clock.sleep)
    assert attempts[-1][3] == 35
    assert all(a[:2] == ("hist-db", 18123) for a in attempts)
    assert clock.sleeps == [1.0] * 35


def test_readiness_timeout_is_bounded_without_real_sleep():
    readiness = readiness_module()
    clock, attempts = FakeClock(), []

    def ping(host, port, timeout):
        attempts.append((clock.now, timeout))
        clock.now += timeout
        return False

    assert not readiness.wait_for_clickhouse("localhost", 8123, ping=ping, clock=clock.clock, sleep=clock.sleep)
    assert clock.now == 120
    assert attempts[-1][0] + attempts[-1][1] <= 120


def test_readiness_loads_env_file_before_resolving_store_endpoint(tmp_path, monkeypatch):
    readiness = readiness_module()
    env_file = tmp_path / "fake.env"
    env_file.write_text("HFT_CLICKHOUSE_HOST=fake-db\nHFT_CLICKHOUSE_PORT=18123\n")
    monkeypatch.delenv("HFT_CLICKHOUSE_HOST", raising=False)
    monkeypatch.delenv("HFT_CLICKHOUSE_PORT", raising=False)
    calls = []
    monkeypatch.setattr(readiness, "wait_for_clickhouse", lambda host, port: calls.append((host, port)) or True)
    assert readiness.main(["--env-file", str(env_file)]) == 0
    assert calls == [("fake-db", 18123)]
    clients = []
    monkeypatch.setenv("HFT_CLICKHOUSE_USER", "fake-user")
    monkeypatch.setenv("HFT_CLICKHOUSE_PASSWORD", "fake-password")
    monkeypatch.setitem(sys.modules, "clickhouse_connect", SimpleNamespace(get_client=lambda **kw: clients.append(kw)))
    store.HistStore.from_env()
    assert (clients[0]["host"], clients[0]["port"]) == calls[0]


def test_readiness_failure_logs_one_line_without_ingestion(monkeypatch, capsys):
    readiness = readiness_module()
    monkeypatch.setattr(readiness, "wait_for_clickhouse", lambda host, port: False)
    assert readiness.main(["--env-file", "/nonexistent"]) == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == "clickhouse_not_ready: HTTP /ping unavailable after 120s\n"


@pytest.mark.parametrize("ready", [True, False])
def test_readiness_ping_checks_http_body_without_network(monkeypatch, ready):
    readiness = readiness_module()
    calls = []

    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return None

        def read(self, size):
            return b"Ok.\n" if ready else b"not ready"

    def urlopen(request, timeout):
        calls.append((request.full_url, timeout))
        return Response()

    monkeypatch.setattr(readiness, "urlopen", urlopen)
    assert readiness.http_ping("fake-db", 18123, 0.25) is ready
    assert calls == [("http://fake-db:18123/ping", 0.25)]


def test_readiness_ping_returns_false_for_connection_refused(monkeypatch, capsys):
    readiness = readiness_module()

    def urlopen(request, timeout):
        raise ConnectionRefusedError("offline refusal")

    monkeypatch.setattr(readiness, "urlopen", urlopen)
    assert readiness.http_ping("fake-db", 18123, 0.25) is False
    captured = capsys.readouterr()
    assert captured.out == captured.err == ""
