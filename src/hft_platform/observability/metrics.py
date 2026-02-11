from prometheus_client import REGISTRY, Counter, Gauge, Histogram


def _unregister_metric_prefixes(prefixes: list[str]) -> None:
    collectors = set()
    for name, collector in list(REGISTRY._names_to_collectors.items()):
        if any(name.startswith(prefix) for prefix in prefixes):
            collectors.add(collector)
    for collector in collectors:
        try:
            REGISTRY.unregister(collector)
        except KeyError:
            pass


class MetricsRegistry:
    _instance = None

    def __init__(self):
        _unregister_metric_prefixes(
            [
                "feed_events_total",
                "feed_latency_ns",
                "feed_interarrival_ns",
                "bus_overflow_total",
                "normalization_errors_total",
                "lob_updates_total",
                "lob_snapshots_total",
                "feed_last_event_ts",
                "feed_time_skew_ns",
                "strategy_latency_ns",
                "strategy_intents_total",
                "risk_reject_total",
                "stormguard_mode",
                "strategy_position",
                "strategy_skew",
                "strategy_micro_price",
                "order_actions_total",
                "order_reject_total",
                "execution_events_total",
                "execution_router_errors_total",
                "execution_gateway_errors_total",
                "execution_router_lag_ns",
                "execution_router_alive",
                "execution_gateway_alive",
                "execution_router_heartbeat_ts",
                "execution_gateway_heartbeat_ts",
                "position_pnl_realized",
                "shioaji_api_latency_ms",
                "shioaji_api_errors_total",
                "shioaji_api_jitter_ms",
                "shioaji_api_jitter_ms_hist",
                "pipeline_latency_ns",
                "recorder_failures_total",
                "recorder_batches_flushed_total",
                "recorder_rows_flushed_total",
                "recorder_wal_writes_total",
                "queue_depth",
                "feed_resubscribe_total",
                "feed_reconnect_total",
                "system_cpu_usage",
                "system_memory_usage",
                "event_loop_lag_ms",
                # Phase 5 metrics
                "circuit_breaker_state",
                "dlq_size_total",
                "reconciliation_discrepancy_count",
                "recorder_insert_retry_total",
                "feed_gap_by_symbol_seconds",
                # Phase 12 metrics
                "shioaji_keepalive_failures_total",
                "quote_version_switch_total",
                "shioaji_contract_lookup_errors_total",
                "latency_spans_dropped_total",
                "clickhouse_connection_health",
                "wal_corrupt_files_total",
                # Phase 12 P2 metrics
                "wal_batch_flush_total",
                "wal_batch_flush_retry_total",
                "session_refresh_total",
                "market_open_grace_active",
                "wal_directory_size_bytes",
                "wal_file_count",
                "wal_oldest_file_age_seconds",
            ]
        )
        # Market Data
        self.feed_events_total = Counter("feed_events_total", "Total feed events", ["type"])
        self.feed_latency_ns = Histogram(
            "feed_latency_ns", "Feed ingest latency", buckets=[1000, 5000, 10000, 50000, 100000]
        )  # 1us to 100us
        self.feed_interarrival_ns = Histogram(
            "feed_interarrival_ns",
            "Feed inter-arrival time (ns)",
            buckets=[1_000_000, 2_000_000, 5_000_000, 10_000_000, 50_000_000, 100_000_000, 500_000_000],
        )  # 1ms to 500ms
        self.bus_overflow_total = Counter("bus_overflow_total", "Event bus overflows")
        self.normalization_errors_total = Counter("normalization_errors_total", "Normalization failures", ["type"])
        self.lob_updates_total = Counter("lob_updates_total", "LOB updates applied", ["symbol", "type"])
        self.lob_snapshots_total = Counter("lob_snapshots_total", "LOB snapshots applied", ["symbol"])
        self.feed_reconnect_total = Counter("feed_reconnect_total", "Feed reconnect attempts", ["result"])
        self.feed_resubscribe_total = Counter("feed_resubscribe_total", "Feed resubscribe attempts", ["result"])
        self.feed_last_event_ts = Gauge("feed_last_event_ts", "Last feed event timestamp (unix seconds)", ["source"])
        self.feed_time_skew_ns = Gauge(
            "feed_time_skew_ns",
            "Feed time skew (local_ts - exch_ts) in ns",
            ["topic"],
        )
        self.shioaji_api_latency_ms = Histogram(
            "shioaji_api_latency_ms",
            "Shioaji API latency (ms)",
            ["op", "result"],
            buckets=[0.1, 0.5, 1, 2, 5, 10, 20, 50, 100, 200, 500, 1000, 2000],
        )
        self.shioaji_api_errors_total = Counter("shioaji_api_errors_total", "Shioaji API errors", ["op"])
        self.shioaji_api_jitter_ms = Gauge("shioaji_api_jitter_ms", "Shioaji API jitter (ms)", ["op"])
        self.shioaji_api_jitter_ms_hist = Histogram(
            "shioaji_api_jitter_ms_hist",
            "Shioaji API jitter distribution (ms)",
            ["op"],
            buckets=[0.1, 0.5, 1, 2, 5, 10, 20, 50, 100, 200, 500, 1000],
        )

        self.pipeline_latency_ns = Histogram(
            "pipeline_latency_ns",
            "Pipeline stage latency (ns)",
            ["stage"],
            buckets=[
                1_000,
                5_000,
                10_000,
                50_000,
                100_000,
                500_000,
                1_000_000,
                5_000_000,
                10_000_000,
                50_000_000,
                100_000_000,
                500_000_000,
                1_000_000_000,
            ],
        )

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
        self.execution_router_errors_total = Counter("execution_router_errors_total", "Execution router errors")
        self.execution_gateway_errors_total = Counter("execution_gateway_errors_total", "Execution gateway errors")
        self.execution_router_lag_ns = Histogram(
            "execution_router_lag_ns",
            "Execution report lag (ns)",
            buckets=[100_000, 500_000, 1_000_000, 5_000_000, 10_000_000, 50_000_000, 100_000_000, 500_000_000],
        )
        self.execution_router_alive = Gauge("execution_router_alive", "Execution router task alive (1/0)")
        self.execution_gateway_alive = Gauge("execution_gateway_alive", "Execution gateway task alive (1/0)")
        self.execution_router_heartbeat_ts = Gauge(
            "execution_router_heartbeat_ts", "Execution router heartbeat (unix seconds)"
        )
        self.execution_gateway_heartbeat_ts = Gauge(
            "execution_gateway_heartbeat_ts", "Execution gateway heartbeat (unix seconds)"
        )
        self.position_pnl_realized = Gauge("position_pnl_realized", "Realized PnL", ["strategy", "symbol"])

        # Infra
        self.recorder_failures_total = Counter("recorder_failures_total", "Recorder write failures")
        self.recorder_batches_flushed_total = Counter("recorder_batches_flushed_total", "Flushed batches", ["table"])
        self.recorder_rows_flushed_total = Counter("recorder_rows_flushed_total", "Flushed rows", ["table"])
        self.recorder_wal_writes_total = Counter("recorder_wal_writes_total", "WAL writes", ["table"])
        self.queue_depth = Gauge("queue_depth", "Queue depth by type", ["queue"])
        self.event_loop_lag_ms = Gauge("event_loop_lag_ms", "Event loop lag (ms)")

        # Phase 5: Advanced Robustness Metrics
        # Circuit breaker state (0=closed/healthy, 1=open/tripped)
        self.circuit_breaker_state = Gauge(
            "circuit_breaker_state",
            "Circuit breaker state (0=closed, 1=open)",
            ["component"],
        )
        # Dead Letter Queue cumulative count
        self.dlq_size_total = Counter(
            "dlq_size_total",
            "Dead Letter Queue cumulative entry count",
            ["source"],  # e.g., "order", "recorder"
        )
        # Reconciliation discrepancy count
        self.reconciliation_discrepancy_count = Gauge(
            "reconciliation_discrepancy_count",
            "Number of position discrepancies detected",
        )
        # Recorder batch insert retry count
        self.recorder_insert_retry_total = Counter(
            "recorder_insert_retry_total",
            "Recorder batch insert retry count",
            ["table", "result"],  # result: "success", "failed"
        )
        # Per-symbol feed gap in seconds
        self.feed_gap_by_symbol_seconds = Gauge(
            "feed_gap_by_symbol_seconds",
            "Feed gap per symbol (seconds since last tick)",
            ["symbol"],
        )

        # Phase 12: Market Data Robustness & Database Writing Upgrades
        # Shioaji keep-alive failure counter (A3)
        self.shioaji_keepalive_failures_total = Counter(
            "shioaji_keepalive_failures_total",
            "Shioaji keep-alive check failures",
        )
        # Quote version switch counter (A4)
        self.quote_version_switch_total = Counter(
            "quote_version_switch_total",
            "Quote version switches (upgrade/downgrade)",
            ["direction"],  # "upgrade" or "downgrade"
        )
        # Contract lookup errors by symbol (A5)
        self.shioaji_contract_lookup_errors_total = Counter(
            "shioaji_contract_lookup_errors_total",
            "Contract lookup failures by symbol",
            ["code"],
        )
        # Latency spans dropped due to overflow (B2)
        self.latency_spans_dropped_total = Counter(
            "latency_spans_dropped_total",
            "Latency spans dropped due to queue/buffer overflow",
        )
        # ClickHouse connection health gauge (B4)
        self.clickhouse_connection_health = Gauge(
            "clickhouse_connection_health",
            "ClickHouse connection health (1=healthy, 0=unhealthy)",
        )
        # Corrupt WAL files counter (B5)
        self.wal_corrupt_files_total = Counter(
            "wal_corrupt_files_total",
            "Corrupt WAL files quarantined",
        )

        # Phase 12 P2: Holiday Resilience & Scheduled WAL Import
        # WAL batch flush at market close (C2)
        self.wal_batch_flush_total = Counter(
            "wal_batch_flush_total",
            "WAL batch flush operations at market close",
            ["result"],  # "ok" or "error"
        )
        # WAL batch flush retry counter (O2)
        self.wal_batch_flush_retry_total = Counter(
            "wal_batch_flush_retry_total",
            "WAL batch flush retry attempts",
        )
        # Session refresh counter (C3)
        self.session_refresh_total = Counter(
            "session_refresh_total",
            "Preventive session refresh operations",
            ["result"],  # "ok" or "error"
        )
        # Market open grace period active indicator (C4)
        self.market_open_grace_active = Gauge(
            "market_open_grace_active",
            "Whether market open grace period is active (1=active, 0=inactive)",
        )
        # WAL directory monitoring (C5)
        self.wal_directory_size_bytes = Gauge(
            "wal_directory_size_bytes",
            "Total size of WAL directory in bytes",
        )
        self.wal_file_count = Gauge(
            "wal_file_count",
            "Number of pending WAL files",
        )
        self.wal_oldest_file_age_seconds = Gauge(
            "wal_oldest_file_age_seconds",
            "Age of oldest WAL file in seconds",
        )

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
