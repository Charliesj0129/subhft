"""Tests for thread-safe MetricsRegistry singleton (double-checked locking)."""

import threading

import pytest


class TestMetricsRegistryLock:
    def setup_method(self):
        """Reset singleton before each test to ensure a clean state."""
        from hft_platform.observability import metrics as metrics_module

        metrics_module.MetricsRegistry._instance = None

    def teardown_method(self):
        """Reset singleton after each test."""
        from hft_platform.observability import metrics as metrics_module

        metrics_module.MetricsRegistry._instance = None

    def test_instance_lock_class_attribute_exists(self):
        """_instance_lock must be present as a threading.Lock class attribute."""
        from hft_platform.observability.metrics import MetricsRegistry

        assert hasattr(MetricsRegistry, "_instance_lock")
        assert isinstance(MetricsRegistry._instance_lock, type(threading.Lock()))

    def test_get_returns_same_instance_sequentially(self):
        """Sequential calls to get() must return the identical instance."""
        from hft_platform.observability.metrics import MetricsRegistry

        first = MetricsRegistry.get()
        second = MetricsRegistry.get()
        assert first is second

    def test_concurrent_get_returns_same_instance(self):
        """Multiple threads calling get() concurrently must all receive the same instance."""
        from hft_platform.observability.metrics import MetricsRegistry

        num_threads = 20
        instances: list = [None] * num_threads
        barrier = threading.Barrier(num_threads)
        errors: list = []

        def worker(idx: int) -> None:
            try:
                barrier.wait(timeout=5)
                instances[idx] = MetricsRegistry.get()
            except Exception as exc:  # noqa: BLE001
                errors.append(exc)

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(num_threads)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=10)

        assert not errors, f"Worker threads raised exceptions: {errors}"
        assert all(inst is not None for inst in instances), "Some threads got None"
        first = instances[0]
        assert all(inst is first for inst in instances), (
            "Threads received different MetricsRegistry instances — race condition detected"
        )
