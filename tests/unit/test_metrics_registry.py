import builtins
import sys

from prometheus_client import Counter, REGISTRY

from hft_platform.observability.metrics import MetricsRegistry, _unregister_metric_prefixes


def test_unregister_metric_prefixes_removes():
    counter = Counter("test_prefix_metric_total", "doc")
    assert "test_prefix_metric_total" in REGISTRY._names_to_collectors  # type: ignore[attr-defined]
    _unregister_metric_prefixes(["test_prefix_metric"])
    assert "test_prefix_metric_total" not in REGISTRY._names_to_collectors  # type: ignore[attr-defined]
    assert "test_prefix_metric_created" not in REGISTRY._names_to_collectors  # type: ignore[attr-defined]


def test_metrics_registry_system_metrics(monkeypatch):
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
    registry = MetricsRegistry.get()
    assert hasattr(registry, "system_cpu_usage")
    registry.update_system_metrics()


def test_metrics_registry_import_error(monkeypatch):
    original_import = builtins.__import__

    def fake_import(name, *args, **kwargs):
        if name == "psutil":
            raise ImportError("no psutil")
        return original_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", fake_import)
    MetricsRegistry._instance = None
    registry = MetricsRegistry.get()
    assert not hasattr(registry, "system_cpu_usage")


def test_metrics_registry_update_system_metrics_handles_error(monkeypatch):
    class BadPsutil:
        @staticmethod
        def cpu_percent():
            raise RuntimeError("boom")

    monkeypatch.setitem(sys.modules, "psutil", BadPsutil)
    MetricsRegistry._instance = None
    registry = MetricsRegistry.get()
    registry.update_system_metrics()


def test_unregister_metric_prefixes_handles_keyerror(monkeypatch):
    Counter("test_prefix_keyerror_total", "doc")

    def raise_keyerror(_collector):
        raise KeyError("missing")

    monkeypatch.setattr(REGISTRY, "unregister", raise_keyerror)
    _unregister_metric_prefixes(["test_prefix_keyerror"])
