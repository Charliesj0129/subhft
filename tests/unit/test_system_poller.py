import sys
import time

from hft_platform.observability.metrics import MetricsRegistry


def test_system_poller_starts_and_stops(monkeypatch):
    class DummyVM:
        percent = 42.0

    class DummyPsutil:
        @staticmethod
        def cpu_percent():
            return 12.5

        @staticmethod
        def virtual_memory():
            return DummyVM()

    monkeypatch.setitem(sys.modules, "psutil", DummyPsutil)
    MetricsRegistry._instance = None
    metrics = MetricsRegistry.get()

    from hft_platform.observability._system_poller import SystemPoller

    poller = SystemPoller(metrics, interval_s=1.0)
    poller.start()
    assert poller._thread is not None
    assert poller._thread.is_alive()
    poller.stop()
    poller._thread.join(timeout=3.0)
    assert not poller._running


def test_system_poller_updates_gauges(monkeypatch):
    class DummyVM:
        percent = 55.0

    class DummyPsutil:
        @staticmethod
        def cpu_percent():
            return 33.3

        @staticmethod
        def virtual_memory():
            return DummyVM()

    monkeypatch.setitem(sys.modules, "psutil", DummyPsutil)
    MetricsRegistry._instance = None
    metrics = MetricsRegistry.get()

    from hft_platform.observability._system_poller import SystemPoller

    poller = SystemPoller(metrics, interval_s=0.1)
    poller.start()
    time.sleep(0.3)
    poller.stop()
    poller._thread.join(timeout=3.0)

    # Gauges should have been updated
    assert metrics.system_cpu_usage._value.get() > 0
    assert metrics.system_memory_usage._value.get() > 0


def test_system_poller_handles_no_psutil(monkeypatch):
    """When psutil is not available, the poller loop exits gracefully."""
    import builtins

    original_import = builtins.__import__

    def fake_import(name, *args, **kwargs):
        if name == "psutil":
            raise ImportError("no psutil")
        return original_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", fake_import)
    monkeypatch.delitem(sys.modules, "psutil", raising=False)
    MetricsRegistry._instance = None
    metrics = MetricsRegistry.get()

    from hft_platform.observability._system_poller import SystemPoller

    poller = SystemPoller(metrics, interval_s=0.1)
    poller.start()
    time.sleep(0.3)
    # Thread should have exited due to ImportError
    assert not poller._thread.is_alive()


def test_system_poller_no_double_start(monkeypatch):
    class DummyVM:
        percent = 10.0

    class DummyPsutil:
        @staticmethod
        def cpu_percent():
            return 5.0

        @staticmethod
        def virtual_memory():
            return DummyVM()

    monkeypatch.setitem(sys.modules, "psutil", DummyPsutil)
    MetricsRegistry._instance = None
    metrics = MetricsRegistry.get()

    from hft_platform.observability._system_poller import SystemPoller

    poller = SystemPoller(metrics, interval_s=1.0)
    poller.start()
    first_thread = poller._thread
    poller.start()  # Should not create a new thread
    assert poller._thread is first_thread
    poller.stop()
    poller._thread.join(timeout=3.0)
