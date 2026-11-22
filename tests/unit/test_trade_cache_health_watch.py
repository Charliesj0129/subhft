"""The trade-cache health watch, the metrics it feeds, and the CA-expiry alert rule.

The watch (``ops/trade_cache_health_watch.py``) only schedules the broker client's read-only probe in a worker
thread. What the probe reads and exports is covered in ``test_shioaji_ops_checks.py``.
"""

from __future__ import annotations

import asyncio
import math
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

from hft_platform.observability.metrics import (
    CA_EXPIRY_STATES,
    TRADE_CACHE_HEALTH_CHECK_RESULTS,
    TRADE_CACHE_HEALTH_STATES,
    MetricsRegistry,
)
from hft_platform.ops import trade_cache_health_watch
from hft_platform.ops.trade_cache_health_watch import run_trade_cache_health_watch

ROOT = Path(__file__).resolve().parents[2]


class _Client:
    """Records which thread each probe ran on and raises when told to."""

    def __init__(self, *, fail_first: int = 0, target_calls: int = 1) -> None:
        self.threads: list[int] = []
        self.fail_first = fail_first
        self.target_calls = target_calls
        self.reached = threading.Event()

    def check_trade_cache_health(self) -> str:
        self.threads.append(threading.get_ident())
        if len(self.threads) >= self.target_calls:
            self.reached.set()
        if len(self.threads) <= self.fail_first:
            raise RuntimeError("probe blew up")
        return "healthy"


async def _cancel(task: asyncio.Task) -> None:
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


@pytest.mark.asyncio
async def test_watch_runs_the_probe_in_a_worker_thread_not_on_the_event_loop():
    client = _Client()
    loop_thread = threading.get_ident()

    task = asyncio.create_task(run_trade_cache_health_watch(client, interval_s=60))
    # The first probe runs before the first sleep; wait for it rather than sleeping a fixed time.
    await asyncio.wait_for(asyncio.to_thread(client.reached.wait, 10), timeout=15)
    await _cancel(task)

    assert len(client.threads) == 1
    assert client.threads[0] != loop_thread


@pytest.mark.asyncio
async def test_watch_keeps_probing_after_a_probe_raises(monkeypatch):
    # The clamp exists so a caller cannot spin the SDK; lift it so the loop can be exercised without waiting.
    monkeypatch.setattr(trade_cache_health_watch, "MIN_INTERVAL_S", 0.0)
    client = _Client(fail_first=1, target_calls=3)

    # 1 ms between probes: unavoidable to drive a real loop, and the wait below is on the probe count, not on time.
    task = asyncio.create_task(run_trade_cache_health_watch(client, interval_s=0.001))
    await asyncio.wait_for(asyncio.to_thread(client.reached.wait, 10), timeout=15)
    await _cancel(task)

    assert len(client.threads) >= 3


@pytest.mark.asyncio
async def test_watch_raises_a_too_small_interval_to_the_minimum(monkeypatch):
    slept: list[float] = []
    real_sleep = asyncio.sleep

    async def fake_sleep(delay: float) -> None:
        slept.append(delay)
        await real_sleep(0)

    monkeypatch.setattr(trade_cache_health_watch.asyncio, "sleep", fake_sleep)
    client = _Client(target_calls=2)

    task = asyncio.create_task(run_trade_cache_health_watch(client, interval_s=0.001))
    await asyncio.wait_for(asyncio.to_thread(client.reached.wait, 10), timeout=15)
    await _cancel(task)

    assert slept
    assert set(slept) == {trade_cache_health_watch.MIN_INTERVAL_S}


@pytest.mark.asyncio
@pytest.mark.parametrize("interval_s", [0, -1.0])
async def test_watch_with_a_non_positive_interval_returns_at_once_without_probing(interval_s):
    client = _Client()

    result = await asyncio.wait_for(run_trade_cache_health_watch(client, interval_s=interval_s), timeout=1.0)

    assert result is None
    assert client.threads == []


@pytest.mark.asyncio
async def test_watch_for_a_client_without_the_probe_returns_at_once():
    result = await asyncio.wait_for(run_trade_cache_health_watch(SimpleNamespace()), timeout=1.0)

    assert result is None


def test_bootstrap_schedules_the_watch_only_for_a_client_that_has_the_probe():
    source = (ROOT / "src" / "hft_platform" / "services" / "bootstrap.py").read_text(encoding="utf-8")

    guard = 'if hasattr(order_client, "check_trade_cache_health"):'
    schedule = "deferred_tasks.append(run_trade_cache_health_watch(order_client))"
    assert guard in source
    assert schedule in source
    assert source.index(guard) < source.index(schedule)


# ---------------------------------------------------------------------------
# Registry: what the gauges read before anything has been checked
# ---------------------------------------------------------------------------


@pytest.fixture
def registry():
    MetricsRegistry._instance = None
    created = MetricsRegistry.get()
    yield created
    MetricsRegistry._instance = None


def _samples(metric) -> dict[tuple[tuple[str, str], ...], float]:
    return {
        tuple(sorted(sample.labels.items())): sample.value
        for family in metric.collect()
        for sample in family.samples
        if not sample.name.endswith("_created")
    }


def test_registry_starts_with_no_ca_reading_and_every_state_series_at_zero(registry):
    days = _samples(registry.shioaji_ca_days_to_expiry)
    states = _samples(registry.shioaji_ca_expiry_state)

    assert math.isnan(days[()])
    assert states == {(("state", state),): 0.0 for state in CA_EXPIRY_STATES}


def test_registry_starts_with_every_trade_cache_series_at_zero(registry):
    states = _samples(registry.shioaji_trade_cache_health_state)
    checks = _samples(registry.shioaji_trade_cache_health_checks_total)

    assert states == {(("state", state),): 0.0 for state in TRADE_CACHE_HEALTH_STATES}
    assert checks == {(("result", result),): 0.0 for result in TRADE_CACHE_HEALTH_CHECK_RESULTS}
    assert _samples(registry.shioaji_trade_cache_health_last_success_ts) == {(): 0.0}


# ---------------------------------------------------------------------------
# Alert rule: loaded in production through config/monitoring/prometheus.yml
# ---------------------------------------------------------------------------


def _alert(name: str) -> dict:
    data = yaml.safe_load((ROOT / "config" / "monitoring" / "alerts" / "rules.yaml").read_text(encoding="utf-8"))
    for group in data["groups"]:
        for rule in group.get("rules", []):
            if rule.get("alert") == name:
                return rule
    raise AssertionError(f"{name} is not in rules.yaml")


def test_ca_expiry_alert_fires_below_14_days_on_the_exported_gauge(registry):
    rule = _alert("ShioajiCaExpiringSoon")

    assert rule["expr"] == "shioaji_ca_days_to_expiry < 14"
    assert rule["labels"]["severity"] == "warning"
    assert hasattr(registry, "shioaji_ca_days_to_expiry")


def test_ca_expiry_alert_waits_out_a_blip_and_points_at_an_existing_runbook():
    rule = _alert("ShioajiCaExpiringSoon")

    assert rule["for"] == "15m"
    runbook = rule["annotations"]["runbook_url"].split("#", 1)[0]
    assert (ROOT / runbook).is_file()


def test_the_alert_file_that_holds_the_ca_rule_is_listed_in_prometheus_rule_files():
    config = yaml.safe_load((ROOT / "config" / "monitoring" / "prometheus.yml").read_text(encoding="utf-8"))

    assert "/etc/prometheus/alerts/rules.yaml" in config["rule_files"]
