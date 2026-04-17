import os
import threading

from prometheus_client import REGISTRY, Counter, Gauge, Histogram

_METRICS_PREFIX = os.getenv("HFT_METRICS_PREFIX", "")

_KNOWN_EXCEPTION_TYPES: frozenset[str] = frozenset(
    {
        "ConnectionError",
        "TimeoutError",
        "OSError",
        "RuntimeError",
        "asyncio.TimeoutError",
        "ConnectionResetError",
        "ConnectionRefusedError",
        "BrokenPipeError",
    }
)


def cap_exception_type(exc: BaseException) -> str:
    """Return the exception class name if it is in the known allowlist, else ``'_other'``."""
    name = type(exc).__name__
    if name in _KNOWN_EXCEPTION_TYPES:
        return name
    return "_other"


def _pn(name: str) -> str:
    """Prefix a metric name if HFT_METRICS_PREFIX is set."""
    if _METRICS_PREFIX and not name.startswith(_METRICS_PREFIX):
        return f"{_METRICS_PREFIX}{name}"
    return name


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


def _unregister_all_custom_metrics() -> None:
    """Unregister all non-default collectors to allow safe re-instantiation."""
    from prometheus_client import gc_collector, platform_collector, process_collector

    default_types = (
        type(platform_collector.PLATFORM_COLLECTOR),
        type(gc_collector.GC_COLLECTOR),
        type(process_collector.PROCESS_COLLECTOR),
    )
    to_remove = set()
    for _name, collector in list(REGISTRY._names_to_collectors.items()):
        if not isinstance(collector, default_types):
            to_remove.add(collector)
    for collector in to_remove:
        try:
            REGISTRY.unregister(collector)
        except (KeyError, ValueError):
            pass


class MetricsRegistry:
    _instance = None
    _instance_lock = threading.Lock()
    _MAX_LABEL_SYMBOLS: int = int(os.getenv("HFT_METRICS_MAX_LABEL_SYMBOLS", "200"))

    def __init__(self):
        self._seen_symbols: set[str] = set()
        _unregister_all_custom_metrics()
        _unregister_metric_prefixes(
            [
                _pn("feed_events_total"),
                _pn("feed_latency_ns"),
                _pn("feed_interarrival_ns"),
                _pn("bus_overflow_total"),
                _pn("bus_gap_events_total"),
                _pn("normalization_errors_total"),
                _pn("normalization_skip_total"),
                _pn("lob_updates_total"),
                _pn("lob_snapshots_total"),
                _pn("feed_last_event_ts"),
                _pn("feed_time_skew_ns"),
                _pn("strategy_latency_ns"),
                _pn("strategy_intents_total"),
                _pn("risk_reject_total"),
                _pn("stormguard_mode"),
                _pn("stormguard_transitions_total"),
                _pn("stormguard_halt_exempt_bypass_total"),
                _pn("halt_drain_safety_intent_lost_total"),
                _pn("order_actions_total"),
                _pn("order_reject_total"),
                _pn("order_halt_skip_total"),
                _pn("order_deadline_expired_total"),
                _pn("phantom_order_candidates_total"),
                _pn("api_guard_timeout_total"),
                _pn("shadow_orders_total"),
                _pn("shadow_mode_active"),
                _pn("execution_events_total"),
                _pn("execution_router_errors_total"),
                _pn("execution_gateway_errors_total"),
                _pn("execution_router_lag_ns"),
                _pn("execution_router_alive"),
                _pn("execution_gateway_alive"),
                _pn("execution_router_heartbeat_ts"),
                _pn("execution_gateway_heartbeat_ts"),
                _pn("position_pnl_realized"),
                _pn("shioaji_api_latency_ms"),
                _pn("shioaji_api_errors_total"),
                _pn("shioaji_api_jitter_ms"),
                _pn("shioaji_api_jitter_ms_hist"),
                _pn("pipeline_latency_ns"),
                _pn("recorder_schema_init_failed"),
                _pn("recorder_failures_total"),
                _pn("recorder_batches_flushed_total"),
                _pn("recorder_rows_flushed_total"),
                _pn("recorder_wal_writes_total"),
                _pn("recorder_wal_skipped_rows_total"),
                _pn("recorder_wal_write_latency_ms"),
                _pn("recorder_wal_fsync_latency_ms"),
                _pn("recorder_ch_insert_latency_ms"),
                _pn("recorder_insert_batches_total"),
                _pn("wal_disk_available_mb"),
                _pn("wal_disk_circuit_breaker_active"),
                _pn("queue_depth"),
                _pn("feed_resubscribe_total"),
                _pn("feed_reconnect_total"),
                _pn("feed_reconnect_timeout_total"),
                _pn("feed_reconnect_exception_total"),
                _pn("system_cpu_usage"),
                _pn("system_memory_usage"),
                _pn("event_loop_lag_ms"),
                # Phase 5 metrics
                _pn("circuit_breaker_state"),
                _pn("dlq_size_total"),
                _pn("orphaned_fill"),
                _pn("fills_total"),
                _pn("duplicate_fill"),
                _pn("synthetic_fill_id"),
                _pn("portfolio_total_pnl"),
                _pn("reconciliation_discrepancy_count"),
                _pn("recorder_insert_retry_total"),
                _pn("feed_gap_by_symbol_seconds"),
                # Phase 12 metrics
                _pn("shioaji_keepalive_failures_total"),
                _pn("quote_version_switch_total"),
                _pn("quote_schema_mismatch_total"),
                _pn("shioaji_contract_lookup_errors_total"),
                _pn("latency_spans_dropped_total"),
                _pn("clickhouse_connection_health"),
                _pn("redis_connection_health"),
                _pn("wal_corrupt_files_total"),
                # Phase 12 P2 metrics
                _pn("wal_batch_flush_total"),
                _pn("wal_batch_flush_retry_total"),
                _pn("session_refresh_total"),
                _pn("market_open_grace_active"),
                _pn("wal_directory_size_bytes"),
                _pn("wal_file_count"),
                _pn("wal_oldest_file_age_seconds"),
                # Phase 12 P2.2 metrics
                _pn("raw_queue_dropped_total"),
                _pn("raw_queue_depth"),
                _pn("clickhouse_pool_active"),
                _pn("clickhouse_pool_timeout_total"),
                _pn("clickhouse_pool_checkout_latency_ms"),
                # CE-M2 Gateway SLO metrics
                _pn("gateway_dedup_hits_total"),
                _pn("gateway_reject_total"),
                _pn("gateway_dispatch_latency_ns"),
                _pn("gateway_intent_channel_depth"),
                _pn("gateway_policy_mode"),
                _pn("gateway_exposure_notional_scaled"),
                # CE-M3 WAL SLO metrics
                _pn("wal_mode"),
                _pn("wal_replay_lag_seconds"),
                _pn("wal_replay_throughput_rows_total"),
                _pn("wal_replay_errors_total"),
                _pn("wal_backlog_files"),
                _pn("wal_drain_eta_seconds"),
                _pn("disk_pressure_level"),
                # Alpha liveness metrics
                _pn("alpha_signal_events_total"),
                _pn("alpha_last_signal_ts"),
                # Strategy exception metrics
                _pn("strategy_exceptions_total"),
                # Quote watchdog recovery metrics
                _pn("quote_watchdog_recovery_attempts_total"),
                _pn("shioaji_quote_route_total"),
                _pn("shioaji_quote_callback_ingress_latency_ns"),
                _pn("shioaji_quote_callback_queue_depth"),
                _pn("shioaji_quote_callback_queue_dropped_total"),
                _pn("shioaji_thread_alive"),
                _pn("shioaji_quote_pending_age_seconds"),
                _pn("shioaji_quote_pending_stall_total"),
                _pn("shioaji_session_lock_conflicts_total"),
                _pn("feed_session_conflict_total"),
                _pn("feed_session_lease_ops_total"),
                _pn("feed_first_quote_total"),
                _pn("shioaji_login_fail_total"),
                _pn("shioaji_crash_signature_total"),
                _pn("market_data_callback_parse_total"),
                _pn("feature_plane_updates_total"),
                _pn("feature_plane_latency_ns"),
                _pn("feature_quality_flags_total"),
                _pn("feature_shadow_parity_checks_total"),
                _pn("feature_shadow_parity_mismatch_total"),
                _pn("feature_profile_activations_total"),
                _pn("feature_profile_rollout_state"),
                _pn("feature_profile_compat_failures_total"),
                _pn("contract_refresh_total"),
                _pn("contract_refresh_symbols_changed_total"),
                _pn("autonomy_mode"),
                _pn("autonomy_transitions_total"),
                _pn("strategy_quarantine_active"),
                _pn("platform_reduce_only_active"),
                _pn("manual_rearm_required"),
                # WU-04/WU-18 Reconciliation resilience metrics
                _pn("reconciliation_sync_total"),
                _pn("reconciliation_sync_duration_seconds"),
                _pn("reconciliation_discrepancy_total"),
                _pn("reconciliation_consecutive_failures"),
                _pn("reconciliation_last_success_ts"),
                _pn("reconciliation_auto_corrected_total"),
                _pn("position_drift_qty"),
                _pn("portfolio_drawdown_pct"),
                _pn("portfolio_trade_count"),
                # ClickHouse backup metrics
                _pn("hft_backup_last_success_ts"),
                _pn("hft_backup_size_bytes"),
                _pn("hft_backup_duration_seconds"),
                _pn("hft_backup_retained_count"),
                # Pipeline Determinism & Async Defense (D1-D8)
                _pn("exec_queue_overflow_total"),
                _pn("exec_overflow_drained_total"),
                _pn("exec_overflow_evicted_total"),
                _pn("terminal_before_registration_total"),
                _pn("deferred_terminal_expired_total"),
                _pn("risk_halt_blocked_total"),
                _pn("order_queue_full_total"),
                _pn("risk_dlq_drained_total"),
                _pn("risk_dlq_expired_total"),
                _pn("risk_dlq_revalidation_rejected_total"),
                _pn("risk_dlq_overflow_total"),
                _pn("fill_dlq_overflow_total"),
                _pn("deferred_terminal_overflow_total"),
                _pn("audit_dropped_total"),
                _pn("intent_queue_full_total"),
                _pn("risk_engine_error_total"),
                # rejection_sink overflow
                _pn("rejection_sink_overflow_total"),
                # SLO-2: E2E order-to-fill latency
                _pn("e2e_order_latency_ns"),
                # Recorder exec drop counter (P-01)
                _pn("recorder_exec_drops_total"),
                # Recorder exec WAL fallback counter
                _pn("recorder_exec_wal_fallback_total"),
                _pn("recorder_exec_wal_fallback_failure_total"),
                # Recorder reinject circuit breaker drops (P-21)
                _pn("recorder_reinject_circuit_breaker_drops_total"),
                # Recorder bridge queue-full drops
                _pn("recorder_bridge_drops_total"),
                # Recorder direct-path queue drops
                _pn("recorder_direct_drops_total"),
                # Rust-to-Python normalizer fallbacks
                _pn("rust_fallback_total"),
                # Post-normalization processing errors (LOB/feature/publish)
                _pn("process_raw_error_total"),
                # Normalization failures (pre-event) in MarketDataService
                _pn("normalize_error_total"),
                # FeatureEngine → StormGuard escalation
                _pn("feature_engine_escalation_total"),
                # Normalizer → StormGuard escalation
                _pn("norm_engine_escalation_total"),
                # Feature staleness detection
                _pn("feature_staleness_detected_total"),
                # Stale event skip counter
                _pn("stale_event_skip_total"),
                # Strategy timeout circuit breaker
                _pn("strategy_timeout_total"),
                _pn("strategy_circuit_break_total"),
                # LOB-only split latency (P3b)
                _pn("lob_only_latency_ns"),
                # Execution fill data loss
                _pn("exec_fill_data_loss"),
                # Pipeline health FSM (PipelineHealthTracker)
                _pn("pipeline_health_state"),
                _pn("pipeline_degradation_events_total"),
                # Per-consumer ring buffer lag gauge
                _pn("bus_consumer_lag"),
                # Broker-thread callback data loss
                _pn("md_callback_drop_total"),
                # Recorder degraded mode
                _pn("recorder_degraded_mode"),
                _pn("recorder_degraded_total"),
                # Observability gap closures
                _pn("strategy_events_received_total"),
                _pn("alias_resolution_coverage_ratio"),
                _pn("reconciliation_drift_streak"),
            ]
        )
        # Market Data
        self.feed_events_total = Counter(_pn("feed_events_total"), "Total feed events", ["type"])
        self.feed_latency_ns = Histogram(
            _pn("feed_latency_ns"), "Feed ingest latency", buckets=[1000, 5000, 10000, 50000, 100000]
        )  # 1us to 100us
        self.feed_interarrival_ns = Histogram(
            _pn("feed_interarrival_ns"),
            "Feed inter-arrival time (ns)",
            buckets=[1_000_000, 2_000_000, 5_000_000, 10_000_000, 50_000_000, 100_000_000, 500_000_000],
        )  # 1ms to 500ms
        self.bus_overflow_total = Counter(_pn("bus_overflow_total"), "Event bus overflows")
        self.bus_gap_events_total = Counter(_pn("bus_gap_events_total"), "GapEvents injected on consumer overflow")
        self.bus_consumer_lag = Gauge(_pn("bus_consumer_lag"), "Consumer lag behind writer cursor", ["consumer"])
        self.normalization_errors_total = Counter(_pn("normalization_errors_total"), "Normalization failures", ["type"])
        self.normalization_skip_total = Counter(
            _pn("normalization_skip_total"), "Normalization silent skips", ["type", "reason"]
        )
        self.rust_fallback_total = Counter(
            _pn("rust_fallback_total"), "Rust-to-Python normalizer fallback count", ["type"]
        )
        self.lob_updates_total = Counter(_pn("lob_updates_total"), "LOB updates applied", ["symbol", "type"])
        self.lob_snapshots_total = Counter(_pn("lob_snapshots_total"), "LOB snapshots applied", ["symbol"])
        self.feed_reconnect_total = Counter(_pn("feed_reconnect_total"), "Feed reconnect attempts", ["result"])
        self.feed_reconnect_timeout_total = Counter(
            _pn("feed_reconnect_timeout_total"),
            "Feed reconnect attempts that timed out",
            ["reason"],
        )
        self.feed_reconnect_exception_total = Counter(
            _pn("feed_reconnect_exception_total"),
            "Feed reconnect attempts that raised exceptions",
            ["reason", "exception_type"],
        )
        self.feed_resubscribe_total = Counter(_pn("feed_resubscribe_total"), "Feed resubscribe attempts", ["result"])
        self.feed_last_event_ts = Gauge(
            _pn("feed_last_event_ts"), "Last feed event timestamp (unix seconds)", ["source"]
        )
        self.feed_time_skew_ns = Gauge(
            _pn("feed_time_skew_ns"),
            "Feed time skew (local_ts - exch_ts) in ns",
            ["topic"],
        )
        self.shioaji_api_latency_ms = Histogram(
            _pn("shioaji_api_latency_ms"),
            "Shioaji API latency (ms)",
            ["op", "result"],
            buckets=[0.1, 0.5, 1, 2, 5, 10, 20, 50, 100, 200, 500, 1000, 2000],
        )
        self.shioaji_api_errors_total = Counter(_pn("shioaji_api_errors_total"), "Shioaji API errors", ["op"])
        self.shioaji_api_jitter_ms = Gauge(_pn("shioaji_api_jitter_ms"), "Shioaji API jitter (ms)", ["op"])
        self.shioaji_api_jitter_ms_hist = Histogram(
            _pn("shioaji_api_jitter_ms_hist"),
            "Shioaji API jitter distribution (ms)",
            ["op"],
            buckets=[0.1, 0.5, 1, 2, 5, 10, 20, 50, 100, 200, 500, 1000],
        )

        self.pipeline_latency_ns = Histogram(
            _pn("pipeline_latency_ns"),
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
            _pn("strategy_latency_ns"),
            "Strategy execution time",
            ["strategy"],
            buckets=[5000, 20000, 50000, 100000, 200000],
        )  # 5us to 200us
        self.strategy_intents_total = Counter(_pn("strategy_intents_total"), "Intents generated", ["strategy"])
        self.risk_reject_total = Counter(_pn("risk_reject_total"), "Risk rejections", ["reason", "strategy"])
        self.stormguard_mode = Gauge(
            _pn("stormguard_mode"), "StormGuard State (0=NORMAL, 1=WARM, 2=STORM, 3=HALT)", ["strategy"]
        )
        self.stormguard_transitions_total = Counter(
            _pn("stormguard_transitions_total"),
            "StormGuard state transitions",
            ["direction"],  # "escalation" or "de_escalation"
        )
        self.stormguard_halt_exempt_bypass_total = Counter(
            _pn("stormguard_halt_exempt_bypass_total"),
            "StormGuard halt-exempt bypass events (strategy allowed through HALT)",
        )
        self.halt_drain_safety_intent_lost_total = Counter(
            _pn("halt_drain_safety_intent_lost_total"),
            "Safety intents (CANCEL/FORCE_FLAT) lost during HALT drain re-queue",
        )
        self.autonomy_mode = Gauge(
            _pn("autonomy_mode"),
            "Autonomy control-plane mode (0=NORMAL, 1=STRATEGY_QUARANTINED, 2=PLATFORM_REDUCE_ONLY, 3=HALT)",
            ["scope"],
        )
        self.autonomy_transitions_total = Counter(
            _pn("autonomy_transitions_total"),
            "Autonomy control-plane transitions",
            ["scope", "from_mode", "to_mode", "reason"],
        )
        self.strategy_quarantine_active = Gauge(
            _pn("strategy_quarantine_active"),
            "Whether a strategy is currently quarantined (1=active, 0=inactive)",
            ["strategy"],
        )
        self.platform_reduce_only_active = Gauge(
            _pn("platform_reduce_only_active"),
            "Whether the platform is currently in reduce-only mode (1=active, 0=inactive)",
        )
        self.manual_rearm_required = Gauge(
            _pn("manual_rearm_required"),
            "Whether manual re-arm is required before autonomy rights return (1=required, 0=not required)",
            ["scope"],
        )

        # Order
        self.order_actions_total = Counter(_pn("order_actions_total"), "Order actions sent", ["type"])
        self.order_reject_total = Counter(_pn("order_reject_total"), "Broker rejects")
        self.order_halt_skip_total = Counter(
            _pn("order_halt_skip_total"),
            "Orders skipped in _api_worker because StormGuard transitioned to HALT",
        )
        self.order_deadline_expired_total = Counter(
            _pn("order_deadline_expired_total"),
            "Orders dropped pre-dispatch because deadline_ns was exceeded",
        )
        self.phantom_order_candidates_total = Counter(
            _pn("phantom_order_candidates_total"),
            "Timed-out mutating API calls that may have succeeded at broker",
        )
        self.api_guard_timeout_total = Counter(
            _pn("api_guard_timeout_total"),
            "API semaphore guard timeouts (not counted as circuit breaker failures)",
        )
        # Shadow mode metrics
        self.shadow_orders_total = Counter(
            _pn("shadow_orders_total"),
            "Shadow orders intercepted (not sent to broker)",
            ["strategy", "symbol", "side"],
        )
        self.shadow_mode_active = Gauge(
            _pn("shadow_mode_active"),
            "Shadow order mode status (1=enabled, 0=disabled)",
        )

        # Execution
        self.execution_events_total = Counter(_pn("execution_events_total"), "Execution callbacks", ["type"])
        self.execution_router_errors_total = Counter(_pn("execution_router_errors_total"), "Execution router errors")
        self.execution_gateway_errors_total = Counter(_pn("execution_gateway_errors_total"), "Execution gateway errors")
        self.orphaned_fill_total = Counter(_pn("orphaned_fill_total"), "Orphaned fills routed to DLQ")
        self.phantom_fill_reconciled_total = Counter(
            _pn("phantom_fill_reconciled_total"),
            "Orphaned fills auto-reconciled via phantom order matching",
        )
        self.fills_total = Counter(_pn("fills_total"), "Total successful fills processed")
        self.duplicate_fill_total = Counter(_pn("duplicate_fill_total"), "Duplicate fills skipped by dedup check")
        self.fill_normalization_failed_total = Counter(
            _pn("fill_normalization_failed_total"),
            "Fill events that failed normalization (missing account, parse error)",
        )
        self.synthetic_fill_id_total = Counter(
            _pn("synthetic_fill_id_total"),
            "Fills with synthesized fill_id (broker omitted seqno)",
        )
        self.execution_router_lag_ns = Histogram(
            _pn("execution_router_lag_ns"),
            "Execution report lag (ns)",
            buckets=[100_000, 500_000, 1_000_000, 5_000_000, 10_000_000, 50_000_000, 100_000_000, 500_000_000],
        )
        self.execution_router_alive = Gauge(_pn("execution_router_alive"), "Execution router task alive (1/0)")
        self.execution_gateway_alive = Gauge(_pn("execution_gateway_alive"), "Execution gateway task alive (1/0)")
        self.execution_router_heartbeat_ts = Gauge(
            _pn("execution_router_heartbeat_ts"), "Execution router heartbeat (unix seconds)"
        )
        self.execution_gateway_heartbeat_ts = Gauge(
            _pn("execution_gateway_heartbeat_ts"), "Execution gateway heartbeat (unix seconds)"
        )
        # E2E order-to-fill latency (SLO-2)
        self.e2e_order_latency_ns = Histogram(
            _pn("e2e_order_latency_ns"),
            "End-to-end order-to-fill latency in nanoseconds",
            buckets=[1e6, 5e6, 10e6, 20e6, 50e6, 100e6, 200e6, 500e6, 1e9],
        )
        self.recorder_exec_drops_total = Counter(
            _pn("recorder_exec_drops_total"),
            "Execution events dropped due to full recorder queue",
            ["topic"],
        )
        self.recorder_exec_wal_fallback_total = Counter(
            _pn("recorder_exec_wal_fallback_total"),
            "Execution events written to WAL fallback after recorder queue full",
            ["topic"],
        )
        self.recorder_exec_wal_fallback_failure_total = Counter(
            _pn("recorder_exec_wal_fallback_failure_total"),
            "WAL fallback write failures for execution events",
            ["topic"],
        )
        self.position_pnl_realized = Gauge(_pn("position_pnl_realized"), "Realized PnL", ["strategy", "symbol"])
        self.portfolio_total_pnl = Gauge(
            _pn("portfolio_total_pnl"), "Total realized PnL across all positions (scaled int)"
        )
        self.portfolio_drawdown_pct = Gauge(
            _pn("portfolio_drawdown_pct"), "Portfolio drawdown from peak equity (0.0 to 1.0)"
        )
        self.portfolio_trade_count = Counter(_pn("portfolio_trade_count"), "Total trade count", ["strategy", "side"])

        # Infra
        self.recorder_bridge_drops_total = Counter(
            _pn("recorder_bridge_drops_total"),
            "Events dropped by recorder bridge due to full recorder queue",
            ["topic"],
        )
        self.recorder_direct_drops_total = Counter(
            _pn("recorder_direct_drops_total"),
            "Direct-path recorder queue drops in MarketDataService",
        )
        self.recorder_degraded_mode = Gauge(
            _pn("recorder_degraded_mode"),
            "Recorder degraded mode active (1=degraded, 0=normal)",
        )
        self.recorder_degraded_total = Counter(
            _pn("recorder_degraded_total"),
            "Times recorder entered degraded mode",
        )
        # Schema initialization
        self.recorder_schema_init_failed = Gauge(
            _pn("recorder_schema_init_failed"),
            "1 if ClickHouse schema initialization failed at startup (WAL-only mode)",
        )
        self.recorder_failures_total = Counter(_pn("recorder_failures_total"), "Recorder write failures")
        self.recorder_batches_flushed_total = Counter(
            _pn("recorder_batches_flushed_total"), "Flushed batches", ["table"]
        )
        self.recorder_rows_flushed_total = Counter(_pn("recorder_rows_flushed_total"), "Flushed rows", ["table"])
        self.recorder_wal_writes_total = Counter(_pn("recorder_wal_writes_total"), "WAL writes", ["table"])
        self.recorder_wal_skipped_rows_total = Counter(
            _pn("recorder_wal_skipped_rows_total"),
            "Recorder WAL rows skipped due to disk pressure policy",
            ["writer", "table", "reason"],
        )
        self.recorder_wal_write_latency_ms = Histogram(
            _pn("recorder_wal_write_latency_ms"),
            "Recorder WAL write latency in milliseconds",
            ["writer", "mode"],
            buckets=[0.1, 0.5, 1, 2, 5, 10, 20, 50, 100, 250, 500, 1000],
        )
        self.recorder_ch_insert_latency_ms = Histogram(
            _pn("recorder_ch_insert_latency_ms"),
            "ClickHouse insert latency in milliseconds",
            ["table"],
            buckets=[1, 5, 10, 50, 100, 500, 1000, 5000, 10000, 30000],
        )
        self.recorder_wal_fsync_latency_ms = Histogram(
            _pn("recorder_wal_fsync_latency_ms"),
            "Recorder WAL fsync latency in milliseconds",
            ["writer", "target"],
            buckets=[0.05, 0.1, 0.2, 0.5, 1, 2, 5, 10, 20, 50, 100],
        )
        self.wal_disk_available_mb = Gauge(
            _pn("wal_disk_available_mb"),
            "Available disk space for WAL directory in MB",
        )
        self.wal_disk_circuit_breaker_active = Gauge(
            _pn("wal_disk_circuit_breaker_active"),
            "WAL disk space circuit breaker state (1=active, 0=inactive)",
            ["writer"],
        )
        self.recorder_process_errors_total = Counter(
            _pn("recorder_process_errors_total"),
            "Recorder main loop processing errors",
        )
        # Pipeline health FSM (PipelineHealthTracker)
        self.pipeline_health_state = Gauge(
            _pn("pipeline_health_state"),
            "Recorder pipeline health state (0=HEALTHY, 1=DEGRADED, 2=CRITICAL, 3=DATA_LOSS)",
        )
        self.pipeline_degradation_events_total = Counter(
            _pn("pipeline_degradation_events_total"),
            "Pipeline health state transition count",
        )
        self.queue_depth = Gauge(_pn("queue_depth"), "Queue depth by type", ["queue"])
        self.event_loop_lag_ms = Gauge(_pn("event_loop_lag_ms"), "Event loop lag (ms)")
        self.startup_warnings_total = Counter(
            _pn("startup_warnings_total"),
            "Startup warnings by component",
            ["component"],
        )

        # Phase 5: Advanced Robustness Metrics
        # Circuit breaker state (0=closed/healthy, 1=open/tripped)
        self.circuit_breaker_state = Gauge(
            _pn("circuit_breaker_state"),
            "Circuit breaker state (0=closed, 1=open)",
            ["component"],
        )
        # Dead Letter Queue cumulative count
        self.dlq_size_total = Counter(
            _pn("dlq_size_total"),
            "Dead Letter Queue cumulative entry count",
            ["source"],  # e.g., "order", "recorder"
        )
        # Reconciliation discrepancy count
        self.reconciliation_discrepancy_count = Gauge(
            _pn("reconciliation_discrepancy_count"),
            "Number of position discrepancies detected",
        )
        # WU-04/WU-18: Reconciliation resilience & Prometheus metrics
        self.reconciliation_sync_total = Counter(
            _pn("reconciliation_sync_total"),
            "Reconciliation sync outcomes",
            ["result"],  # success|failure|skip
        )
        self.reconciliation_sync_duration_seconds = Histogram(
            _pn("reconciliation_sync_duration_seconds"),
            "Reconciliation sync duration in seconds",
            buckets=[0.01, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0],
        )
        self.reconciliation_discrepancy_total = Counter(
            _pn("reconciliation_discrepancy_total"),
            "Reconciliation discrepancies by severity",
            ["severity"],  # info|warning|critical
        )
        self.reconciliation_consecutive_failures = Gauge(
            _pn("reconciliation_consecutive_failures"),
            "Current number of consecutive reconciliation failures",
        )
        self.position_drift_qty = Gauge(
            _pn("position_drift_qty"),
            "Absolute qty drift between local and broker positions",
            ["strategy", "symbol"],
        )
        self.reconciliation_last_success_ts = Gauge(
            _pn("reconciliation_last_success_ts"),
            "Unix epoch seconds of last successful reconciliation",
        )
        self.reconciliation_auto_corrected_total = Counter(
            _pn("reconciliation_auto_corrected_total"),
            "Positions auto-corrected by adopting broker state",
            ["symbol"],
        )
        # Recorder batch insert retry count
        self.recorder_insert_retry_total = Counter(
            _pn("recorder_insert_retry_total"),
            "Recorder batch insert retry count",
            ["table", "result"],  # result: retry|success|failed
        )
        self.recorder_insert_batches_total = Counter(
            _pn("recorder_insert_batches_total"),
            "Recorder insert batch final outcomes",
            ["table", "result"],  # success_no_retry|success_after_retry|failed_after_retry|failed_no_client
        )
        # Per-symbol feed gap in seconds
        self.feed_gap_by_symbol_seconds = Gauge(
            _pn("feed_gap_by_symbol_seconds"),
            "Feed gap per symbol (seconds since last tick)",
            ["symbol"],
        )

        # Phase 12: Market Data Robustness & Database Writing Upgrades
        # Shioaji keep-alive failure counter (A3)
        self.shioaji_keepalive_failures_total = Counter(
            _pn("shioaji_keepalive_failures_total"),
            "Shioaji keep-alive check failures",
        )
        # Quote version switch counter (A4)
        self.quote_version_switch_total = Counter(
            _pn("quote_version_switch_total"),
            "Quote version switches (upgrade/downgrade)",
            ["direction"],  # "upgrade" or "downgrade"
        )
        self.quote_schema_mismatch_total = Counter(
            _pn("quote_schema_mismatch_total"),
            "Quote callback payload schema mismatches rejected by schema guard",
            ["expected", "reason"],
        )
        # Contract lookup errors by symbol (A5)
        self.shioaji_contract_lookup_errors_total = Counter(
            _pn("shioaji_contract_lookup_errors_total"),
            "Contract lookup failures by symbol",
            ["code"],
        )
        # Latency spans dropped due to overflow (B2)
        self.latency_spans_dropped_total = Counter(
            _pn("latency_spans_dropped_total"),
            "Latency spans dropped due to queue/buffer overflow",
        )
        # ClickHouse connection health gauge (B4)
        self.clickhouse_connection_health = Gauge(
            _pn("clickhouse_connection_health"),
            "ClickHouse connection health (1=healthy, 0=unhealthy)",
        )
        # Redis connection health gauge
        self.redis_connection_health = Gauge(
            _pn("redis_connection_health"),
            "Redis connection health (1=healthy, 0=unhealthy)",
        )
        # Corrupt WAL files counter (B5)
        self.wal_corrupt_files_total = Counter(
            _pn("wal_corrupt_files_total"),
            "Corrupt WAL files quarantined",
        )

        # Phase 12 P2: Holiday Resilience & Scheduled WAL Import
        # WAL batch flush at market close (C2)
        self.wal_batch_flush_total = Counter(
            _pn("wal_batch_flush_total"),
            "WAL batch flush operations at market close",
            ["result"],  # "ok" or "error"
        )
        # WAL batch flush retry counter (O2)
        self.wal_batch_flush_retry_total = Counter(
            _pn("wal_batch_flush_retry_total"),
            "WAL batch flush retry attempts",
        )
        # Session refresh counter (C3)
        self.session_refresh_total = Counter(
            _pn("session_refresh_total"),
            "Preventive session refresh operations",
            ["result"],  # "ok" or "error"
        )
        # Market open grace period active indicator (C4)
        self.market_open_grace_active = Gauge(
            _pn("market_open_grace_active"),
            "Whether market open grace period is active (1=active, 0=inactive)",
        )
        # WAL directory monitoring (C5)
        self.wal_directory_size_bytes = Gauge(
            _pn("wal_directory_size_bytes"),
            "Total size of WAL directory in bytes",
        )
        self.wal_file_count = Gauge(
            _pn("wal_file_count"),
            "Number of pending WAL files",
        )
        self.wal_oldest_file_age_seconds = Gauge(
            _pn("wal_oldest_file_age_seconds"),
            "Age of oldest WAL file in seconds",
        )

        # Phase 12 P2.2: Database & Market Data Optimizations
        # raw_queue backpressure metrics (P0-1)
        self.raw_queue_dropped_total = Counter(
            _pn("raw_queue_dropped_total"),
            "Raw queue messages dropped due to backpressure",
        )
        self.process_raw_error_total = Counter(
            _pn("process_raw_error_total"),
            "Post-normalization processing errors (LOB/feature/publish)",
        )
        self.normalize_error_total = Counter(
            _pn("normalize_error_total"),
            "Normalization failures in MarketDataService",
        )
        self.raw_queue_depth = Gauge(
            _pn("raw_queue_depth"),
            "Current raw queue depth",
        )
        # ClickHouse connection pool metrics (P0-3)
        self.clickhouse_pool_active = Gauge(
            _pn("clickhouse_pool_active"),
            "Number of active connections in ClickHouse pool",
        )
        self.clickhouse_pool_timeout_total = Counter(
            _pn("clickhouse_pool_timeout_total"),
            "ClickHouse connection pool checkout timeouts",
        )
        self.clickhouse_pool_checkout_latency_ms = Histogram(
            _pn("clickhouse_pool_checkout_latency_ms"),
            "ClickHouse connection pool checkout latency (ms)",
            buckets=[0.1, 0.5, 1, 2, 5, 10, 20, 50, 100, 200, 500, 1000],
        )

        # CE-M2 Gateway SLO (CE2-07)
        # SLO: P99 < 1ms, alert > 2ms
        self.gateway_dedup_hits_total = Counter(
            _pn("gateway_dedup_hits_total"),
            "Idempotency cache hits (duplicate intents suppressed)",
        )
        # reason label: HALT, DEGRADE, EXPOSURE, VALIDATOR, DEDUP
        self.gateway_reject_total = Counter(
            _pn("gateway_reject_total"),
            "Gateway rejected intents by reason",
            ["reason"],
        )
        # SLO: P99 < 1_000_000 ns (1ms); alert > 2_000_000 ns (2ms)
        self.gateway_dispatch_latency_ns = Histogram(
            _pn("gateway_dispatch_latency_ns"),
            "End-to-end gateway dispatch latency (ns): dedup→policy→exposure→risk→dispatch",
            buckets=[1_000, 5_000, 10_000, 50_000, 100_000, 500_000, 1_000_000, 5_000_000],
        )
        # SLO: depth < 100; alert > 500
        self.gateway_intent_channel_depth = Gauge(
            _pn("gateway_intent_channel_depth"),
            "Current depth of LocalIntentChannel queue",
        )
        self.gateway_dlq_size = Gauge(
            _pn("gateway_dlq_size"),
            "Gateway DLQ expired-intent count",
        )
        # 0=NORMAL, 1=DEGRADE, 2=HALT
        self.gateway_policy_mode = Gauge(
            _pn("gateway_policy_mode"),
            "Current GatewayPolicy mode (0=NORMAL, 1=DEGRADE, 2=HALT)",
        )
        self.gateway_exposure_notional_scaled = Gauge(
            _pn("gateway_exposure_notional_scaled"),
            "Current per-strategy/symbol exposure (scaled integer)",
            ["strategy", "symbol"],
        )
        self.gateway_exposure_global_notional_scaled = Gauge(
            _pn("gateway_exposure_global_notional_scaled"),
            "Current global exposure notional (scaled integer, sum across all strategies/symbols)",
        )

        # CE-M3 WAL SLO (CE3-06)
        # 0=direct, 1=wal_first
        self.wal_mode = Gauge(
            _pn("wal_mode"),
            "Current recorder WAL mode (0=direct, 1=wal_first)",
        )
        # SLO: lag < 300s; alert > 600s
        self.wal_replay_lag_seconds = Gauge(
            _pn("wal_replay_lag_seconds"),
            "Oldest unprocessed WAL file age in seconds (replay lag)",
        )
        self.wal_replay_throughput_rows_total = Counter(
            _pn("wal_replay_throughput_rows_total"),
            "Total rows successfully replayed from WAL to ClickHouse",
        )
        self.wal_replay_errors_total = Counter(
            _pn("wal_replay_errors_total"),
            "WAL replay errors by type",
            ["type"],
        )
        # SLO: backlog < 50; alert > 200
        self.wal_backlog_files = Gauge(
            _pn("wal_backlog_files"),
            "Number of WAL files pending replay",
        )
        self.wal_drain_eta_seconds = Gauge(
            _pn("wal_drain_eta_seconds"),
            "Estimated seconds to drain WAL backlog at current throughput",
        )
        # SLO: level = 0; alert >= 2
        # 0=ok, 1=warn, 2=critical, 3=halt
        self.disk_pressure_level = Gauge(
            _pn("disk_pressure_level"),
            "Current disk pressure level (0=ok, 1=warn, 2=critical, 3=halt)",
        )

        # Alpha signal liveness (P0-2)
        # Tracks whether strategies are producing non-flat signals.
        # Grafana alert: time() - alpha_last_signal_ts > 300 → "alpha silent"
        self.alpha_signal_events_total = Counter(
            _pn("alpha_signal_events_total"),
            "Alpha signal decisions by outcome",
            ["strategy", "outcome"],  # outcome: "intent" | "flat"
        )
        self.alpha_last_signal_ts = Gauge(
            _pn("alpha_last_signal_ts"),
            "Unix timestamp of last non-flat alpha signal",
            ["strategy"],
        )
        # Alpha governance pipeline metrics
        self.alpha_gate_results_total = Counter(
            _pn("alpha_gate_results_total"),
            "Alpha gate evaluation results",
            ["alpha_id", "gate", "result"],  # result: "pass" | "fail"
        )
        self.alpha_promotion_results_total = Counter(
            _pn("alpha_promotion_results_total"),
            "Alpha promotion decisions",
            ["alpha_id", "result"],  # result: "approved" | "rejected" | "forced"
        )
        self.alpha_canary_actions_total = Counter(
            _pn("alpha_canary_actions_total"),
            "Alpha canary state transitions",
            ["alpha_id", "action"],  # action: "hold" | "escalated" | "rolled_back" | "graduated"
        )
        # Strategy exception counter — strategy, exception_type, method
        self.strategy_exceptions_total = Counter(
            _pn("strategy_exceptions_total"),
            "Strategy exceptions by type and handler method",
            ["strategy", "exception_type", "method"],
        )
        # Strategy timeout circuit breaker
        self.strategy_timeout_total = Counter(
            _pn("strategy_timeout_total"),
            "Strategy handle_event calls exceeding wall-clock timeout",
            ["strategy_name"],
        )
        self.strategy_circuit_break_total = Counter(
            _pn("strategy_circuit_break_total"),
            "Strategy circuit breaks triggered by consecutive timeouts",
            ["strategy_name"],
        )
        # Quote watchdog recovery attempts (re-register callbacks / version downgrade)
        self.quote_watchdog_recovery_attempts_total = Counter(
            _pn("quote_watchdog_recovery_attempts_total"),
            "Quote watchdog recovery attempts by action",
            ["action"],  # action: "version_downgrade" | "callback_reregister"
        )
        self.shioaji_quote_route_total = Counter(
            _pn("shioaji_quote_route_total"),
            "Shioaji quote callback route outcomes",
            ["result"],  # "miss" | "fallback" | "drop"
        )
        self.shioaji_quote_callback_ingress_latency_ns = Histogram(
            _pn("shioaji_quote_callback_ingress_latency_ns"),
            "Shioaji callback ingress handler latency (ns), from callback entry to queue handoff/drop decision",
            buckets=[500, 1_000, 5_000, 10_000, 20_000, 50_000, 100_000, 200_000, 500_000, 1_000_000, 5_000_000],
        )
        self.shioaji_quote_callback_queue_depth = Gauge(
            _pn("shioaji_quote_callback_queue_depth"),
            "Current Shioaji callback ingress queue depth",
        )
        self.shioaji_quote_callback_queue_dropped_total = Counter(
            _pn("shioaji_quote_callback_queue_dropped_total"),
            "Dropped Shioaji callback payloads due to callback ingress queue overflow",
        )
        self.shioaji_thread_alive = Gauge(
            _pn("shioaji_thread_alive"),
            "Shioaji runtime thread liveness (1=alive, 0=down)",
            ["thread"],
        )
        self.shioaji_quote_pending_age_seconds = Gauge(
            _pn("shioaji_quote_pending_age_seconds"),
            "Age in seconds of pending quote-resubscribe state",
        )
        self.shioaji_quote_pending_stall_total = Counter(
            _pn("shioaji_quote_pending_stall_total"),
            "Pending quote-resubscribe entered stall state",
            ["reason"],
        )
        self.shioaji_session_lock_conflicts_total = Counter(
            _pn("shioaji_session_lock_conflicts_total"),
            "Detected potential multi-runtime broker session lock conflicts",
        )
        self.feed_session_conflict_total = Counter(
            _pn("feed_session_conflict_total"),
            "Detected another runtime holding the feed session at startup (Redis preflight)",
            ["role"],
        )
        self.feed_session_lease_ops_total = Counter(
            _pn("feed_session_lease_ops_total"),
            "Redis feed session lease operation outcomes",
            ["op", "result"],  # op: preflight|refresh|stale_cleanup|teardown
        )
        self.feed_first_quote_total = Counter(
            _pn("feed_first_quote_total"),
            "First live quote received since engine start",
        )
        self.shioaji_login_fail_total = Counter(
            _pn("shioaji_login_fail_total"),
            "Shioaji login attempts exhausted retries",
            ["reason"],
        )
        self.shioaji_crash_signature_total = Counter(
            _pn("shioaji_crash_signature_total"),
            "Detected Shioaji crash precursor signatures",
            ["signature", "context"],
        )
        self.market_data_callback_parse_total = Counter(
            _pn("market_data_callback_parse_total"),
            "MarketDataService Shioaji callback parser outcomes",
            ["result"],  # "fast" | "fallback" | "miss"
        )
        self.md_callback_drop_total = Counter(
            _pn("md_callback_drop_total"),
            "Market data callback drops (broker thread)",
            ["reason"],  # "parse_miss" | "loop_missing" | "callback_error"
        )
        self.feature_plane_updates_total = Counter(
            _pn("feature_plane_updates_total"),
            "FeatureEngine runtime update outcomes",
            ["result", "feature_set"],  # result: "emitted" | "updated" | "error"
        )
        self.feature_plane_latency_ns = Histogram(
            _pn("feature_plane_latency_ns"),
            "FeatureEngine processing latency (ns)",
            buckets=[
                1_000,
                5_000,
                10_000,
                20_000,
                50_000,
                100_000,
                200_000,
                500_000,
                1_000_000,
                5_000_000,
            ],
        )
        self.lob_only_latency_ns = Histogram(
            _pn("lob_only_latency_ns"),
            "LOB-only processing latency (ns), excluding FeatureEngine",
            buckets=[
                1_000,
                5_000,
                10_000,
                20_000,
                50_000,
                100_000,
                200_000,
                500_000,
                1_000_000,
                5_000_000,
            ],
        )
        self.feature_quality_flags_total = Counter(
            _pn("feature_quality_flags_total"),
            "FeatureEngine quality flags emitted",
            ["flag"],  # gap/reset/stale/out_of_order/partial
        )
        # FE-07 skeleton metrics: used by parity harness/shadow rollout in later phases.
        self.feature_shadow_parity_checks_total = Counter(
            _pn("feature_shadow_parity_checks_total"),
            "Feature shadow parity checks",
            ["feature_set", "result"],  # result: checked|skipped
        )
        self.feature_shadow_parity_mismatch_total = Counter(
            _pn("feature_shadow_parity_mismatch_total"),
            "Feature shadow parity mismatches",
            ["feature_set", "feature_id"],
        )
        self.feature_profile_activations_total = Counter(
            _pn("feature_profile_activations_total"),
            "Feature profile activations / rollbacks",
            ["feature_set", "profile_id", "action"],  # action: activate|rollback|shadow
        )
        self.feature_profile_rollout_state = Gauge(
            _pn("feature_profile_rollout_state"),
            "Feature profile rollout state (0=disabled,1=shadow,2=active)",
            ["feature_set", "profile_id"],
        )
        self.feature_profile_compat_failures_total = Counter(
            _pn("feature_profile_compat_failures_total"),
            "Strategy/Feature compatibility failures",
            ["strategy", "code"],
        )
        self.feature_engine_escalation_total = Counter(
            _pn("feature_engine_escalation_total"),
            "FeatureEngine consecutive failure escalations to StormGuard STORM",
        )
        self.norm_engine_escalation_total = Counter(
            _pn("norm_engine_escalation_total"),
            "Normalizer consecutive failure escalations to StormGuard STORM",
        )
        self.feature_staleness_detected_total = Counter(
            _pn("feature_staleness_detected_total"),
            "Times is_feature_stale() returned True (stale or never-updated features)",
        )
        self.stale_event_skip_total = Counter(
            _pn("stale_event_skip_total"),
            "Events skipped due to staleness in StrategyRunner",
        )
        self.contract_refresh_total = Counter(
            _pn("contract_refresh_total"),
            "Contract refresh operations",
            ["result"],  # ok|error|skipped_locked
        )
        self.contract_refresh_symbols_changed_total = Counter(
            _pn("contract_refresh_symbols_changed_total"),
            "Symbol changes detected after contract refresh",
            ["change"],  # added|removed|same
        )

        # ── Backup Metrics ──────────────────────────────────────────
        self.backup_last_success_ts = Gauge(
            _pn("hft_backup_last_success_ts"),
            "Unix timestamp of last successful ClickHouse backup",
        )
        self.backup_size_bytes = Gauge(
            _pn("hft_backup_size_bytes"),
            "Size of most recent ClickHouse backup in bytes",
        )
        self.backup_duration_seconds = Gauge(
            _pn("hft_backup_duration_seconds"),
            "Duration of most recent ClickHouse backup in seconds",
        )
        self.backup_retained_count = Gauge(
            _pn("hft_backup_retained_count"),
            "Number of ClickHouse backups currently retained on disk",
        )

        # ── Fill Data Loss (I-09) ─────────────────────────────────────
        self.exec_fill_data_loss_total = Counter(
            _pn("exec_fill_data_loss_total"),
            "Fills LOST because WAL writer unavailable and recorder queue full",
        )

        # ── Pipeline Determinism & Async Defense (D1-D8) ─────────────
        self.exec_queue_overflow_total = Counter(
            _pn("exec_queue_overflow_total"),
            "Fills routed to overflow buffer when raw_exec_queue is full",
        )
        self.exec_overflow_drained_total = Counter(
            _pn("exec_overflow_drained_total"),
            "Fills successfully drained from overflow buffer",
        )
        self.exec_overflow_evicted_total = Counter(
            _pn("exec_overflow_evicted_total"),
            "Fills LOST when overflow buffer is also full",
        )
        self.terminal_before_registration_total = Counter(
            _pn("terminal_before_registration_total"),
            "Terminal callbacks deferred because order not yet registered",
        )
        self.deferred_terminal_expired_total = Counter(
            _pn("deferred_terminal_expired_total"),
            "Deferred terminal callbacks that expired without resolution",
        )
        self.risk_halt_blocked_total = Counter(
            _pn("risk_halt_blocked_total"),
            "Commands blocked by RiskEngine HALT guard before dispatch",
        )
        self.order_queue_full_total = Counter(
            _pn("order_queue_full_total"),
            "Approved commands dropped due to order_queue full in RiskEngine",
        )
        self.risk_dlq_drained_total = Counter(
            _pn("risk_dlq_drained_total"),
            "DLQ entries successfully drained back to order_queue",
        )
        self.risk_dlq_expired_total = Counter(
            _pn("risk_dlq_expired_total"),
            "DLQ entries expired due to TTL staleness",
        )
        self.risk_dlq_revalidation_rejected_total = Counter(
            _pn("risk_dlq_revalidation_rejected_total"),
            "DLQ entries rejected during replay due to position-limit re-check",
        )
        self.risk_dlq_overflow_total = Counter(
            _pn("risk_dlq_overflow_total"),
            "Risk DLQ overflow evictions (oldest entry dropped)",
        )
        self.fill_dlq_overflow_total = Counter(
            _pn("fill_dlq_overflow_total"),
            "Orphaned fill DLQ overflow evictions (oldest fill silently dropped)",
        )
        self.deferred_terminal_overflow_total = Counter(
            _pn("deferred_terminal_overflow_total"),
            "Deferred terminal deque overflow (oldest terminal silently dropped)",
        )
        self.audit_dropped_total = Counter(
            _pn("audit_dropped_total"),
            "Audit events dropped due to queue full",
            ["table"],
        )
        self.intent_queue_full_total = Counter(
            _pn("intent_queue_full_total"),
            "Intents dropped due to QueueFull in StrategyRunner submit loop",
        )
        self.risk_engine_error_total = Counter(
            _pn("risk_engine_error_total"),
            "RiskEngine internal errors caught in main run() loop",
            ["error_type"],
        )
        self.rejection_sink_overflow_total = Counter(
            _pn("rejection_sink_overflow_total"),
            "RiskFeedback drops due to rejection_sink QueueFull (feedback lost)",
        )

        # Recorder reinject circuit breaker drops (P-21)
        self.recorder_reinject_circuit_breaker_drops_total = Counter(
            _pn("recorder_reinject_circuit_breaker_drops_total"),
            "Rows dropped by reinject circuit breaker after consecutive double-faults",
            ["table"],
        )

        # ── Observability gap closures ──────────────────────────────
        # Bug 12 (13hr R47 silent): per-strategy event dispatch rate.
        # A strategy that stops receiving events for hours should be alertable.
        self.strategy_events_received_total = Counter(
            _pn("strategy_events_received_total"),
            "Events dispatched to a strategy's handle_event (pre-call)",
            ["strategy_id"],
        )
        # Bug 12: alias resolution coverage — if configured aliases (e.g. TXFR1/C0)
        # fail to land in SymbolMetadata, strategies silently see 0 events.
        self.alias_resolution_coverage_ratio = Gauge(
            _pn("alias_resolution_coverage_ratio"),
            "Fraction of configured broker aliases propagated into SymbolMetadata (0.0-1.0)",
        )
        # MANUAL drift persistence: mirror consecutive_observations counter
        # per-symbol so operators can alert on streaks that never clear.
        self.reconciliation_drift_streak = Gauge(
            _pn("reconciliation_drift_streak"),
            "Consecutive reconciliation observations of the same drift (resets to 0 on resolve)",
            ["symbol"],
        )

        # System (v2)
        try:
            import psutil

            self.system_cpu_usage = Gauge(_pn("system_cpu_usage"), "CPU Usage Percent")
            self.system_memory_usage = Gauge(_pn("system_memory_usage"), "Memory Usage Percent")

            # Simple hook to update system metrics on scrape (or periodically)
            # For simplicity, we can rely on a background task or just update regularly.
            # Here we just define them.
        except ImportError:
            pass

    def cap_symbol(self, symbol: str) -> str:
        """Return *symbol* for labelling, capping unique values at ``_MAX_LABEL_SYMBOLS``.

        Once the cap is reached, unseen symbols are mapped to ``"_other"``
        to prevent Prometheus cardinality explosion.  Already-seen symbols
        always pass through unchanged.
        """
        if symbol in self._seen_symbols:
            return symbol
        if len(self._seen_symbols) < self._MAX_LABEL_SYMBOLS:
            self._seen_symbols.add(symbol)
            return symbol
        return "_other"

    @classmethod
    def get(cls):
        if cls._instance is None:
            with cls._instance_lock:
                if cls._instance is None:
                    cls._instance = cls()
        return cls._instance

    def update_system_metrics(self):
        try:
            import psutil

            self.system_cpu_usage.set(psutil.cpu_percent())
            self.system_memory_usage.set(psutil.virtual_memory().percent)
        except Exception as _exc:  # noqa: BLE001
            pass


# Helper to expose via simple HTTP handler if needed, or just use Registry


def get_metrics() -> "MetricsRegistry | None":
    """Return the MetricsRegistry singleton, or None if not yet initialised.

    Safe to call from any module — returns None rather than raising if the
    singleton has not been constructed yet (e.g. during unit tests that do not
    call MetricsRegistry.get() first).
    """
    return MetricsRegistry._instance
