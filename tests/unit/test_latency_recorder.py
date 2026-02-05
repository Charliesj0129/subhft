import asyncio

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
