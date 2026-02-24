import asyncio
from unittest.mock import MagicMock

from hft_platform.observability.latency import LatencyRecorder, _bool_env


def test_latency_recorder_enqueues(monkeypatch):
    monkeypatch.setenv("HFT_LATENCY_TRACE", "1")
    monkeypatch.setenv("HFT_LATENCY_SAMPLE_EVERY", "1")
    LatencyRecorder.reset_for_tests()
    rec = LatencyRecorder.get()
    queue: asyncio.Queue = asyncio.Queue()
    rec.configure(queue)

    rec.record("stage", 1000, trace_id="trace", symbol="SYM", strategy_id="alpha", ts_ns=123)

    item = queue.get_nowait()
    assert item["topic"] == "latency_spans"
    assert item["data"]["stage"] == "stage"
    assert item["data"]["trace_id"] == "trace"


def test_bool_env_variants():
    assert _bool_env(None, default=True) is True
    assert _bool_env(None, default=False) is False
    assert _bool_env(True) is True
    assert _bool_env("1") is True
    assert _bool_env("yes") is True
    assert _bool_env("false") is False


def test_latency_recorder_sampling_and_guards(monkeypatch):
    monkeypatch.setenv("HFT_LATENCY_TRACE", "1")
    monkeypatch.setenv("HFT_LATENCY_SAMPLE_EVERY", "2")
    LatencyRecorder.reset_for_tests()
    rec = LatencyRecorder.get()
    queue: asyncio.Queue = asyncio.Queue()
    rec.configure(queue)

    rec.record("stage", -1)
    assert queue.empty()

    rec.record("stage", 1000)
    assert queue.empty()

    rec.record("stage", 2000, ts_ns=456)
    item = queue.get_nowait()
    assert item["data"]["latency_us"] == 2


def test_latency_recorder_no_queue_and_put_fail(monkeypatch):
    monkeypatch.setenv("HFT_LATENCY_TRACE", "1")
    monkeypatch.setenv("HFT_LATENCY_SAMPLE_EVERY", "1")
    LatencyRecorder.reset_for_tests()
    rec = LatencyRecorder.get()
    rec.configure(None)
    rec.record("stage", 1000)

    class BadQueue:
        def put_nowait(self, _item):
            raise RuntimeError("boom")

    rec.configure(BadQueue())
    rec.record("stage", 1000, ts_ns=789)


def test_latency_recorder_invalid_sample_env(monkeypatch):
    monkeypatch.setenv("HFT_LATENCY_TRACE", "0")
    monkeypatch.setenv("HFT_LATENCY_SAMPLE_EVERY", "bad")
    LatencyRecorder.reset_for_tests()
    rec = LatencyRecorder.get()
    assert rec._sample_every == 100
    assert rec._should_sample() is False


def test_latency_recorder_retry_buffer_and_drop(monkeypatch):
    monkeypatch.setenv("HFT_LATENCY_TRACE", "1")
    monkeypatch.setenv("HFT_LATENCY_SAMPLE_EVERY", "1")
    monkeypatch.setenv("HFT_LATENCY_RETRY_BUFFER_SIZE", "1")
    LatencyRecorder.reset_for_tests()
    rec = LatencyRecorder.get()

    class AlwaysFullQueue:
        def put_nowait(self, _item):
            raise RuntimeError("full")

    class MetricsStub:
        def __init__(self):
            self.latency_spans_dropped_total = MagicMock()

        def pipeline_latency_ns(self):  # pragma: no cover - helper
            raise AssertionError("not used")

    rec.metrics = MetricsStub()
    rec.configure(AlwaysFullQueue())
    rec._dropped_total = 99

    rec.record("stage", 1000)
    rec.record("stage", 2000)

    assert rec._dropped_total >= 100
    rec.metrics.latency_spans_dropped_total.inc.assert_called()


def test_latency_recorder_drain_retry_buffer(monkeypatch):
    monkeypatch.setenv("HFT_LATENCY_TRACE", "1")
    monkeypatch.setenv("HFT_LATENCY_SAMPLE_EVERY", "1")
    LatencyRecorder.reset_for_tests()
    rec = LatencyRecorder.get()

    class FlakyQueue:
        def __init__(self):
            self.calls = 0

        def put_nowait(self, _item):
            self.calls += 1
            if self.calls > 1:
                raise RuntimeError("full")

    queue = FlakyQueue()
    rec.configure(queue)
    rec._retry_buffer.extend(
        [
            {"topic": "latency_spans", "data": {"a": 1}},
            {"topic": "latency_spans", "data": {"a": 2}},
        ]
    )

    rec._drain_retry_buffer()

    assert len(rec._retry_buffer) == 1


def test_latency_recorder_invalid_retry_buffer_env(monkeypatch):
    monkeypatch.setenv("HFT_LATENCY_RETRY_BUFFER_SIZE", "bad")
    LatencyRecorder.reset_for_tests()
    rec = LatencyRecorder.get()
    assert rec._retry_buffer_size == 1000


def test_latency_recorder_obs_policy_minimal_defaults(monkeypatch):
    monkeypatch.delenv("HFT_LATENCY_SAMPLE_EVERY", raising=False)
    monkeypatch.delenv("HFT_LATENCY_METRICS_SAMPLE_EVERY", raising=False)
    monkeypatch.setenv("HFT_OBS_POLICY", "minimal")
    LatencyRecorder.reset_for_tests()
    rec = LatencyRecorder.get()
    assert rec._sample_every >= 1000
    assert rec._metrics_sample_every >= 16
