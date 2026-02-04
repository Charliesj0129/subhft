from prometheus_client import Counter, Gauge, Histogram


class MetricsRegistry:
    _instance = None

    def __init__(self):
        # Market Data
        self.feed_events_total = Counter("feed_events_total", "Total feed events", ["type"])
        self.feed_latency_ns = Histogram(
            "feed_latency_ns", "Feed ingest latency", buckets=[1000, 5000, 10000, 50000, 100000]
        )  # 1us to 100us
        self.bus_overflow_total = Counter("bus_overflow_total", "Event bus overflows")
        self.normalization_errors_total = Counter("normalization_errors_total", "Normalization failures", ["type"])
        self.lob_updates_total = Counter("lob_updates_total", "LOB updates applied", ["symbol", "type"])
        self.lob_snapshots_total = Counter("lob_snapshots_total", "LOB snapshots applied", ["symbol"])

        # Strategy/Risk
        self.strategy_latency_ns = Histogram(
            "strategy_latency_ns", "Strategy execution time", ["strategy"], buckets=[5000, 20000, 50000, 100000, 200000]
        )  # 5us to 200us
        self.strategy_intents_total = Counter("strategy_intents_total", "Intents generated", ["strategy"])
        self.risk_reject_total = Counter("risk_reject_total", "Risk rejections", ["reason", "strategy"])
        self.stormguard_mode = Gauge(
            "stormguard_mode", "StormGuard State (0=NORMAL, 1=WARM, 2=STORM, 3=HALT)", ["strategy"]
        )

        # Strategy Alpha (Whitebox)
        self.strategy_position = Gauge("strategy_position", "Current Net Position", ["strategy", "symbol"])
        self.strategy_skew = Gauge("strategy_skew", "Price Skew adjustment", ["strategy", "symbol"])
        self.strategy_micro_price = Gauge("strategy_micro_price", "Computed MicroPrice", ["strategy", "symbol"])

        # Order
        self.order_actions_total = Counter("order_actions_total", "Order actions sent", ["type"])
        self.order_reject_total = Counter("order_reject_total", "Broker rejects")

        # Execution
        self.execution_events_total = Counter("execution_events_total", "Execution callbacks", ["type"])
        self.position_pnl_realized = Gauge("position_pnl_realized", "Realized PnL", ["strategy", "symbol"])
        self.execution_router_alive = Gauge("execution_router_alive", "Execution router liveness")
        self.execution_gateway_alive = Gauge("execution_gateway_alive", "Execution gateway liveness")

        # Broker/API
        self.shioaji_api_latency_ms = Histogram(
            "shioaji_api_latency_ms",
            "Shioaji API latency (ms)",
            ["op", "result"],
            buckets=[0.1, 0.5, 1, 2, 5, 10, 20, 50, 100, 200, 500, 1000, 2000],
        )
        self.shioaji_api_errors_total = Counter("shioaji_api_errors_total", "Shioaji API errors", ["op"])

        # Infra
        self.recorder_failures_total = Counter("recorder_failures_total", "Recorder write failures")
        self.recorder_batches_flushed_total = Counter("recorder_batches_flushed_total", "Flushed batches", ["table"])
        self.recorder_rows_flushed_total = Counter("recorder_rows_flushed_total", "Flushed rows", ["table"])
        self.recorder_wal_writes_total = Counter("recorder_wal_writes_total", "WAL writes", ["table"])
        self.queue_depth = Gauge("queue_depth", "Queue depth", ["queue"])
        self.feed_resubscribe_total = Counter("feed_resubscribe_total", "Feed resubscribe attempts", ["result"])
        self.feed_reconnect_total = Counter("feed_reconnect_total", "Feed reconnect attempts", ["result"])

        # System (v2)
        try:
            import psutil

            self.system_cpu_usage = Gauge("system_cpu_usage", "CPU Usage Percent")
            self.system_memory_usage = Gauge("system_memory_usage", "Memory Usage Percent")

            # Simple hook to update system metrics on scrape (or periodically)
            # For simplicity, we can rely on a background task or just update regularly.
            # Here we just define them.
        except ImportError:
            pass

    @classmethod
    def get(cls):
        if cls._instance is None:
            cls._instance = cls()
        return cls._instance

    def update_system_metrics(self):
        try:
            import psutil

            self.system_cpu_usage.set(psutil.cpu_percent())
            self.system_memory_usage.set(psutil.virtual_memory().percent)
        except Exception:
            pass


# Helper to expose via simple HTTP handler if needed, or just use Registry
