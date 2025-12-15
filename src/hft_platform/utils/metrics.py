class Metrics:
    """Stub for Prometheus/StatsD metrics."""
    @staticmethod
    def counter(name: str, value: int = 1, tags: dict = None):
        # In real impl, send to statsd
        pass

    @staticmethod
    def gauge(name: str, value: float, tags: dict = None):
        pass

    @staticmethod
    def histogram(name: str, value: float, tags: dict = None):
        pass
