import os
import threading

from prometheus_client import REGISTRY, Counter, Gauge, Histogram

from hft_platform.observability.code_identity import running_code_sha

_METRICS_PREFIX = os.getenv("HFT_METRICS_PREFIX", "")

registry_rw_lock: threading.RLock = threading.RLock()

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


_REJECT_REASON_MAX_LEN = 64

UNRESOLVED_PHANTOM_OUTCOMES: tuple[str, ...] = ("cancel_timeout", "cancel_failed", "fill_claimed_after_expiry")
PAGED_PHANTOM_OUTCOMES: tuple[str, ...] = ("fill_claimed_after_expiry",)
BROKER_LOOKUP_PHANTOM_OUTCOMES: tuple[str, ...] = ("found_open", "lookup_failed", "lookup_ambiguous")


def cap_reject_reason(reason: str) -> str:
    """Return the stable code at the head of a reject reason, else ``'OTHER'``.

    The formatted detail is not lost -- it stays on the rejection log line and
    on ``RiskDecision`` / ``RiskFeedback``, which is where an operator reading a
    single rejection looks. A counter is the wrong place for it.
    """
    head = reason.split(":", 1)[0].split("(", 1)[0].strip()
    if not head or len(head) > _REJECT_REASON_MAX_LEN:
        return "OTHER"
    if not head.isascii() or not head[0].isalpha():
        return "OTHER"
    if not all(ch.isalnum() or ch == "_" for ch in head):
        return "OTHER"
    return head


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
        self._registry_rw_lock = registry_rw_lock
        self._registry_rw_lock.__enter__()
        try:
            self._init_collectors()
        finally:
            self._registry_rw_lock.__exit__(None, None, None)

    def _init_collectors(self) -> None:
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
                _pn("feed_time_skew_over_threshold_total"),
                _pn("strategy_latency_ns"),
                _pn("strategy_intents_total"),
                _pn("track_gate_intents_filtered_total"),
                _pn("risk_reject_total"),
                _pn("stormguard_mode"),
                _pn("stormguard_halt_reason"),
                _pn("kill_switch_active"),
                _pn("manual_latch_active"),
                _pn("stormguard_transitions_total"),
                _pn("stormguard_toxicity_score"),
                _pn("stormguard_escalations_total"),
                _pn("stormguard_latency_input_armed"),
                _pn("stormguard_latency_input_max_us"),
                _pn("drift_burst_detected_total"),
                _pn("stormguard_halt_exempt_bypass_total"),
                _pn("halt_drain_safety_intent_lost_total"),
                _pn("loss_exit_orders_total"),
                _pn("loss_exit_unpriced_total"),
                _pn("kill_switch_flatten_blocked"),
                _pn("halt_callback_no_loop_total"),
                _pn("halt_callback_schedule_failed_total"),
                _pn("halt_callback_dispatched_total"),
                _pn("background_task_failed_total"),
                _pn("order_actions_total"),
                _pn("order_reject_total"),
                _pn("order_contract_code_resolution_total"),
                _pn("order_halt_skip_total"),
                _pn("order_halt_post_dispatch_cancel_total"),
                _pn("api_queue_priority_eviction_total"),
                _pn("order_deadline_expired_total"),
                _pn("order_deadline_overridden_total"),
                _pn("order_session_warmup_total"),
                _pn("order_local_reject_total"),
                _pn("order_broker_silence_seconds"),
                _pn("order_broker_inflight"),
                _pn("order_broker_silence_episode_seconds"),
                _pn("place_order_late_result_total"),
                _pn("phantom_bound_total"),
                _pn("phantom_order_candidates_total"),
                _pn("phantom_recovery_releases_total"),
                _pn("live_order_ttl_releases_total"),
                _pn("live_order_swept_after_fill_total"),
                _pn("live_order_completed_by_fill_total"),
                _pn("order_repeat_terminal_total"),
                _pn("cancel_unconfirmed_total"),
                _pn("order_outcome_unknown_total"),
                _pn("order_dlq_adds_total"),
                _pn("cancel_retry_total"),
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
                _pn("order_session_reconnect_total"),
                _pn("order_session_down"),
                _pn("system_cpu_usage"),
                _pn("system_memory_usage"),
                _pn("event_loop_lag_ms"),
                _pn("event_loop_probe_lag_ms"),
                _pn("supervisor_tick_duration_ms"),
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
                _pn("feed_gap_latched_silent_symbols_total"),
                _pn("shioaji_keepalive_failures_total"),
                _pn("shioaji_session_down_total"),
                _pn("quote_version_switch_total"),
                _pn("quote_schema_mismatch_total"),
                _pn("shioaji_contract_lookup_errors_total"),
                _pn("latency_spans_dropped_total"),
                _pn("clickhouse_connection_health"),
                _pn("wal_corrupt_files_total"),
                _pn("wal_orphan_tmp_cleaned_total"),
                _pn("wal_batch_flush_total"),
                _pn("wal_batch_flush_retry_total"),
                _pn("session_refresh_total"),
                _pn("market_open_grace_active"),
                _pn("market_trading_hours_active"),
                _pn("wal_directory_size_bytes"),
                _pn("wal_file_count"),
                _pn("wal_oldest_file_age_seconds"),
                _pn("raw_queue_dropped_total"),
                _pn("raw_queue_depth"),
                _pn("clickhouse_pool_active"),
                _pn("clickhouse_pool_timeout_total"),
                _pn("clickhouse_pool_checkout_latency_ms"),
                _pn("gateway_dedup_hits_total"),
                _pn("gateway_reject_total"),
                _pn("gateway_dispatch_latency_ns"),
                _pn("gateway_intent_channel_depth"),
                _pn("gateway_policy_mode"),
                _pn("gateway_exposure_notional_scaled"),
                _pn("wal_mode"),
                _pn("wal_replay_lag_seconds"),
                _pn("wal_replay_throughput_rows_total"),
                _pn("wal_replay_errors_total"),
                _pn("wal_backlog_files"),
                _pn("wal_drain_eta_seconds"),
                _pn("disk_pressure_level"),
                _pn("alpha_signal_events_total"),
                _pn("alpha_last_signal_ts"),
                _pn("strategy_exceptions_total"),
                _pn("quote_watchdog_recovery_attempts_total"),
                _pn("shioaji_quote_route_total"),
                _pn("shioaji_quote_callback_ingress_latency_ns"),
                _pn("shioaji_quote_callback_queue_depth"),
                _pn("shioaji_quote_callback_queue_dropped_total"),
                _pn("shioaji_thread_alive"),
                _pn("shioaji_quote_pending_age_seconds"),
                _pn("shioaji_quote_pending_stall_total"),
                _pn("shioaji_session_lock_conflicts_total"),
                _pn("shioaji_login_slot_timeouts_total"),
                _pn("feed_session_conflict_total"),
                _pn("feed_session_lease_ops_total"),
                _pn("shioaji_logout_failed_total"),
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
                _pn("contract_cache_last_success_ts"),
                _pn("contract_update_events_total"),
                _pn("contract_update_last_event_ts"),
                _pn("contract_event_callback_registered"),
                _pn("autonomy_mode"),
                _pn("autonomy_transitions_total"),
                _pn("strategy_quarantine_active"),
                _pn("strategy_quarantine_undurable"),
                _pn("strategy_quarantine_persist_failed_total"),
                _pn("platform_reduce_only_active"),
                _pn("platform_inputs_calendar_unavailable_total"),
                _pn("manual_rearm_required"),
                _pn("reconciliation_sync_total"),
                _pn("reconciliation_sync_duration_seconds"),
                _pn("reconciliation_discrepancy_total"),
                _pn("reconciliation_consecutive_failures"),
                _pn("reconciliation_last_success_ts"),
                _pn("reconciliation_auto_corrected_total"),
                _pn("reconciliation_not_comparable"),
                _pn("position_drift_qty"),
                _pn("portfolio_drawdown_pct"),
                _pn("portfolio_trade_count"),
                _pn("hft_backup_last_success_ts"),
                _pn("hft_backup_size_bytes"),
                _pn("hft_backup_duration_seconds"),
                _pn("hft_backup_retained_count"),
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
                _pn("fill_dlq_persist_failed_total"),
                _pn("deferred_terminal_overflow_total"),
                _pn("audit_dropped_total"),
                _pn("intent_queue_full_total"),
                _pn("risk_engine_error_total"),
                _pn("rejection_sink_overflow_total"),
                _pn("e2e_order_latency_ns"),
                _pn("recorder_exec_drops_total"),
                _pn("recorder_exec_wal_fallback_total"),
                _pn("recorder_exec_wal_fallback_failure_total"),
                _pn("recorder_reinject_circuit_breaker_drops_total"),
                _pn("recorder_bridge_drops_total"),
                _pn("recorder_direct_drops_total"),
                _pn("recorder_intent_drop_total"),
                _pn("rust_fallback_total"),
                _pn("process_raw_error_total"),
                _pn("normalize_error_total"),
                _pn("feature_engine_escalation_total"),
                _pn("norm_engine_escalation_total"),
                _pn("feature_staleness_detected_total"),
                _pn("stale_event_skip_total"),
                _pn("strategy_timeout_total"),
                _pn("strategy_circuit_break_total"),
                _pn("lob_only_latency_ns"),
                _pn("exec_fill_data_loss"),
                _pn("pipeline_health_state"),
                _pn("pipeline_degradation_events_total"),
                _pn("bus_consumer_lag"),
                _pn("md_callback_drop_total"),
                _pn("recorder_degraded_mode"),
                _pn("recorder_degraded_total"),
                _pn("strategy_events_received_total"),
                _pn("alias_resolution_coverage_ratio"),
                _pn("alias_map_size"),
                _pn("strategy_bound_live_symbols"),
                _pn("front_month_subscribed"),
                _pn("front_month_strategy_on_front"),
                _pn("front_month_check_timestamp_seconds"),
                _pn("feed_connect_gate_blocked_total"),
                _pn("reconciliation_drift_streak"),
                _pn("reconciliation_untrusted_sample_streak"),
                _pn("feed_symbol_config_reload_total"),
                _pn("feed_subscription_truncate_total"),
            ]
        )
        self.feed_events_total = Counter(_pn("feed_events_total"), "Total feed events", ["type"])
        self.feed_latency_ns = Histogram(
            _pn("feed_latency_ns"), "Feed ingest latency", buckets=[1000, 5000, 10000, 50000, 100000]
        )
        self.feed_interarrival_ns = Histogram(
            _pn("feed_interarrival_ns"),
            "Feed inter-arrival time (ns)",
            buckets=[1_000_000, 2_000_000, 5_000_000, 10_000_000, 50_000_000, 100_000_000, 500_000_000],
        )
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
        self.unknown_symbol_ingress_total = Counter(
            _pn("unknown_symbol_ingress_total"),
            "Events with symbols not declared in SymbolMetadata that reached an ingress boundary",
            ["plane"],
        )
        self.feed_reconnect_total = Counter(_pn("feed_reconnect_total"), "Feed reconnect attempts", ["result"])
        self.order_session_reconnect_total = Counter(
            _pn("order_session_reconnect_total"),
            "Order-session reconnect attempts made by the order-session watchdog",
            ["result"],
        )
        self.order_session_down = Gauge(
            _pn("order_session_down"),
            "1 when consecutive order attempts are failing on an unestablished broker session",
        )
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
        self.feed_resubscribe_skipped_concurrent_total = Counter(
            _pn("feed_resubscribe_skipped_concurrent_total"),
            "D2: Feed resubscribe attempts skipped because another caller held _resubscribe_lock",
        )
        self.feed_subscription_retry_total = Counter(
            _pn("feed_subscription_retry_total"),
            "D1: subscribe retry decisions per symbol (ok|skip_backoff|skip_permanent)",
            ["symbol", "result"],
        )
        self.feed_subscription_permanent_failures_total = Counter(
            _pn("feed_subscription_permanent_failures_total"),
            "D1: symbols that crossed HFT_SUB_RETRY_MAX_ATTEMPTS and stopped retrying",
            ["symbol"],
        )
        self.feed_subscription_retry_attempts = Gauge(
            _pn("feed_subscription_retry_attempts"),
            "D1: current attempt counter per symbol (resets to 0 on success)",
            ["symbol"],
        )
        self.feed_symbol_config_reload_total = Counter(
            _pn("feed_symbol_config_reload_total"),
            "Q3: symbol config reload result (ok|exceeds_limit|exceeds_pool_capacity|parse_error|other)",
            ["result"],
        )
        self.feed_subscription_truncate_total = Counter(
            _pn("feed_subscription_truncate_total"),
            "P2 #8: subscription truncated below configured universe (reason=conn_limit)",
            ["reason"],
        )
        self.feed_last_event_ts = Gauge(
            _pn("feed_last_event_ts"), "Last feed event timestamp (unix seconds)", ["source"]
        )
        self.feed_time_skew_ns = Gauge(
            _pn("feed_time_skew_ns"),
            "Feed time skew (local_ts - exch_ts) in ns — current observed delta after clamp",
            ["topic"],
        )
        self.feed_time_skew_over_threshold_total = Counter(
            _pn("feed_time_skew_over_threshold_total"),
            "Feed events with local_ts - exch_ts above a severity threshold",
            ["topic", "severity"],
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
                2_500_000_000,
                5_000_000_000,
                10_000_000_000,
            ],
        )

        self.strategy_latency_ns = Histogram(
            _pn("strategy_latency_ns"),
            "Strategy execution time",
            ["strategy"],
            buckets=[5000, 20000, 50000, 100000, 200000],
        )
        self.strategy_intents_total = Counter(_pn("strategy_intents_total"), "Intents generated", ["strategy"])
        self.strategy_stale_cancels_total = Counter(
            _pn("strategy_stale_cancels_total"),
            "Cancels fired by distance-from-mid reconciler (D1)",
            ["strategy", "side"],
        )
        self.strategy_inflight_oids = Gauge(
            _pn("strategy_inflight_oids"),
            "In-flight order count per strategy/side (D3 tracking)",
            ["strategy", "side"],
        )
        self.strategy_pending_qty = Gauge(
            _pn("strategy_pending_qty"),
            "Quantity a strategy believes is working at the broker, per symbol/side. "
            "This is what gates further quoting; if it stays elevated the strategy "
            "is latched off. Had no metric until 2026-08-12, which is why a "
            "two-day quoting freeze was invisible.",
            ["strategy", "symbol", "side"],
        )
        self.strategy_gate_blocked_total = Counter(
            _pn("strategy_gate_blocked_total"),
            "Ticks suppressed by a strategy placement gate, by gate name",
            ["strategy", "gate"],
        )
        self.risk_reject_total = Counter(_pn("risk_reject_total"), "Risk rejections", ["reason", "strategy"])
        self.stormguard_mode = Gauge(
            _pn("stormguard_mode"), "StormGuard State (0=NORMAL, 1=WARM, 2=STORM, 3=HALT)", ["strategy"]
        )
        self.stormguard_halt_reason = Gauge(
            _pn("stormguard_halt_reason"),
            "1 while StormGuard is in a HALT entered for this reason, else 0",
            ["reason"],
        )
        self.kill_switch_active = Gauge(
            _pn("kill_switch_active"),
            "1 while the kill-switch hold is set (latch file present, or a startup-recovery halt pinned it), else 0",
        )
        self.manual_latch_active = Gauge(
            _pn("manual_latch_active"),
            "1 while the durable manual latch file is present (risk/manual_latches.py), else 0",
            ["latch"],
        )
        self.stormguard_transitions_total = Counter(
            _pn("stormguard_transitions_total"),
            "StormGuard state transitions",
            ["direction"],
        )
        self.stormguard_halt_exempt_bypass_total = Counter(
            _pn("stormguard_halt_exempt_bypass_total"),
            "StormGuard halt-exempt bypass events (strategy allowed through HALT)",
        )
        self.halt_drain_safety_intent_lost_total = Counter(
            _pn("halt_drain_safety_intent_lost_total"),
            "Safety intents (CANCEL/FORCE_FLAT) lost during HALT drain re-queue",
        )
        self.loss_exit_orders_total = Counter(
            _pn("loss_exit_orders_total"),
            "FORCE_FLAT intents emitted by the platform loss/expiry exit, by trigger",
            ["trigger"],
        )
        self.loss_exit_unpriced_total = Counter(
            _pn("loss_exit_unpriced_total"),
            "Exit triggers that could not be priced (no two-sided book), by trigger",
            ["trigger"],
        )
        self.kill_switch_flatten_blocked = Gauge(
            _pn("kill_switch_flatten_blocked"),
            "1 while the kill switch holds open positions it will not close because no recent "
            "reconciliation confirmed them against the broker, else 0",
        )
        self.halt_callback_no_loop_total = Counter(
            _pn("halt_callback_no_loop_total"),
            "HALT notification callbacks dropped because no event loop was bound",
        )
        self.halt_callback_schedule_failed_total = Counter(
            _pn("halt_callback_schedule_failed_total"),
            "HALT notification callbacks that failed to schedule on the engine loop",
        )
        self.halt_callback_dispatched_total = Counter(
            _pn("halt_callback_dispatched_total"),
            "HALT notification callbacks successfully dispatched to the engine loop",
            ["path"],
        )
        self.background_task_failed_total = Counter(
            _pn("background_task_failed_total"),
            "Fire-and-forget background tasks (core.background_tasks.spawn_retained) that ended with an exception",
            ["name"],
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
        self.strategy_quarantine_undurable = Gauge(
            _pn("strategy_quarantine_undurable"),
            (
                "Whether a strategy's quarantine is held in memory only because its "
                "durable write failed or has not been confirmed (1=undurable, 0=durable). "
                "While this is 1 a restart will silently release that quarantine."
            ),
            ["strategy"],
        )
        self.strategy_quarantine_persist_failed_total = Counter(
            _pn("strategy_quarantine_persist_failed_total"),
            "Strategy quarantine durable writes that failed outright",
            ["strategy"],
        )
        self.platform_inputs_calendar_unavailable_total = Counter(
            _pn("platform_inputs_calendar_unavailable_total"),
            "Supervisor reads of the session calendar that raised (the feed-gap reduce-only reasons then read 'shut')",
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

        self.order_actions_total = Counter(_pn("order_actions_total"), "Order actions sent", ["type"])
        self.order_reject_total = Counter(_pn("order_reject_total"), "Broker rejects")
        self.order_cancel_already_terminal_total = Counter(
            _pn("order_cancel_already_terminal_total"),
            "CANCEL intents whose target was already terminal (success, not failure)",
            ["reason"],
        )
        self.order_contract_code_resolution_total = Counter(
            _pn("order_contract_code_resolution_total"),
            "OrderAdapter contract_code resolution path (Gate 3 migration telemetry)",
            ["source"],
        )
        self.order_halt_skip_total = Counter(
            _pn("order_halt_skip_total"),
            "Orders skipped in _api_worker because StormGuard transitioned to HALT",
        )
        self.order_halt_post_dispatch_cancel_total = Counter(
            _pn("order_halt_post_dispatch_cancel_total"),
            "H1: defensive cancels issued after StormGuard transitioned to HALT "
            "during a broker dispatch await window (TOCTOU recovery)",
        )
        self.api_queue_priority_eviction_total = Counter(
            _pn("api_queue_priority_eviction_total"),
            "OrderAdapter._api_queue priority-based evictions when full",
            ["evicted_intent_type", "evictor_intent_type"],
        )
        self.order_deadline_expired_total = Counter(
            _pn("order_deadline_expired_total"),
            "Orders dropped pre-dispatch because deadline_ns was exceeded",
        )
        self.order_deadline_overridden_total = Counter(
            _pn("order_deadline_overridden_total"),
            "CANCEL / FORCE_FLAT commands dispatched although deadline_ns had passed "
            "(risk-reducing commands are never dropped for lateness)",
            ["intent_type"],
        )
        self.order_local_reject_total = Counter(
            _pn("order_local_reject_total"),
            "Intents refused by a local guard before any broker call, by reason. "
            "These are NOT dead letters: nothing left the platform, so there is "
            "nothing to replay. They used to be written to the order DLQ, where "
            "1,733 circuit_breaker refusals over 2026-08-28..09-16 drove "
            "OrderDeadLetterQueueGrowing (critical) on THESHOW.",
            ["reason"],
        )
        self.order_broker_silence_seconds = Gauge(
            _pn("order_broker_silence_seconds"),
            "Seconds the oldest broker order call sent after the last broker answer has been waiting (0 = none)",
        )
        self.order_broker_inflight = Gauge(
            _pn("order_broker_inflight"),
            "Broker order calls (place/cancel/update) handed to the SDK and not yet ended, timed-out ones included",
        )
        self.order_broker_silence_episode_seconds = Histogram(
            _pn("order_broker_silence_episode_seconds"),
            "Broker order-path silence ended by an answer, seconds",
            buckets=(0.05, 0.1, 0.25, 0.5, 1.0, 2.0, 3.0, 5.0, 10.0, 20.0, 30.0, 60.0),
        )
        self.place_order_late_result_total = Counter(
            _pn("place_order_late_result_total"),
            "place_order answers that arrived after the wrapper timeout "
            "(kind=trade: broker result; error: SDK raised; cancelled_before_send: guard stopped the call)",
            ["kind"],
        )
        self.phantom_bound_total = Counter(
            _pn("phantom_bound_total"),
            "What was done with a timed-out place_order once its answer arrived "
            "(cancel_sent / cancel_timeout / cancel_failed / cancel_inflight / skipped_filled / skipped_gone / "
            "released_not_sent / kept_for_ttl; at the TTL: found_open / lookup_failed / lookup_ambiguous)",
            ["outcome"],
        )
        for _outcome in UNRESOLVED_PHANTOM_OUTCOMES + BROKER_LOOKUP_PHANTOM_OUTCOMES:
            self.phantom_bound_total.labels(outcome=_outcome)
        self.phantom_order_candidates_total = Counter(
            _pn("phantom_order_candidates_total"),
            "Timed-out mutating API calls that may have succeeded at broker",
        )
        self.phantom_recovery_releases_total = Counter(
            _pn("phantom_recovery_releases_total"),
            "Phantom orders released after TTL expiry to unfreeze strategy pending counters (Bug D, 2026-04-20)",
        )
        self.live_order_ttl_releases_total = Counter(
            _pn("live_order_ttl_releases_total"),
            "Dispatched orders swept after TTL with no terminal callback, releasing the strategy pending slot "
            "(2026-08-10: two such orders froze R47 for two days with no counter anywhere)",
        )
        self.live_order_swept_after_fill_total = Counter(
            _pn("live_order_swept_after_fill_total"),
            "Fully filled orders found by the TTL sweep; their pending slot was NOT released a second time",
        )
        self.live_order_completed_by_fill_total = Counter(
            _pn("live_order_completed_by_fill_total"),
            "Orders removed from live_orders because cumulative fills reached the order quantity",
        )
        self.order_repeat_terminal_total = Counter(
            _pn("order_repeat_terminal_total"),
            "Terminal order status events for an order that had already ended, reported with remaining_qty=0 "
            "so the strategy does not release its pending slot twice",
            ["status"],
        )
        self.cancel_unconfirmed_total = Counter(
            _pn("cancel_unconfirmed_total"),
            "Dispatched cancels whose order was still live after HFT_CANCEL_CONFIRM_S "
            "(outcome=retried: re-sent once; gave_up: second look, left to the TTL sweep)",
            ["outcome"],
        )
        for _outcome in ("retried", "gave_up"):
            self.cancel_unconfirmed_total.labels(outcome=_outcome)
        self.order_outcome_unknown_total = Counter(
            _pn("order_outcome_unknown_total"),
            "Mutating broker calls that timed out with their outcome unknown, by op",
            ["op"],
        )
        for _op in ("place_order", "update_order"):
            self.order_outcome_unknown_total.labels(op=_op)
        self.cancel_retry_total = Counter(
            _pn("cancel_retry_total"),
            "Result of the single re-sent cancel for an unconfirmed cancel",
            ["result"],
        )
        self.api_guard_timeout_total = Counter(
            _pn("api_guard_timeout_total"),
            "API semaphore guard timeouts (not counted as circuit breaker failures)",
        )
        self.shadow_orders_total = Counter(
            _pn("shadow_orders_total"),
            "Shadow orders intercepted (not sent to broker)",
            ["strategy", "symbol", "side"],
        )
        self.shadow_mode_active = Gauge(
            _pn("shadow_mode_active"),
            "Shadow order mode status (1=enabled, 0=disabled)",
        )

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
        self.order_normalization_failed_total = Counter(
            _pn("order_normalization_failed_total"),
            "Order events dropped by normalization, by reason",
            ["reason"],
        )
        self.order_attribution_unresolved_total = Counter(
            _pn("order_attribution_unresolved_total"),
            "Order callbacks normalized but not attributable to an order_key",
            ["present"],
        )
        self.synthetic_fill_id_total = Counter(
            _pn("synthetic_fill_id_total"),
            "Fills with synthesized fill_id (broker omitted seqno)",
        )
        self.startup_reconciler_missing_fills_total = Counter(
            _pn("startup_reconciler_missing_fills_total"),
            "Bug #32 backfill: fills found at broker but missing from CH at startup",
            ["result"],
        )
        self.startup_reconciler_run_seconds = Histogram(
            _pn("startup_reconciler_run_seconds"),
            "Bug #32 backfill: end-to-end runtime of one startup reconcile pass",
            buckets=(0.05, 0.1, 0.5, 1.0, 2.5, 5.0, 10.0, 30.0),
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
        self.e2e_order_latency_ns = Histogram(
            _pn("e2e_order_latency_ns"),
            "Order-command-to-fill latency in ns (INCLUDES passive book residence "
            "time; NOT broker RTT -- see pipeline_latency_ns{stage=api_place_order})",
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

        self.recorder_bridge_drops_total = Counter(
            _pn("recorder_bridge_drops_total"),
            "Events dropped by recorder bridge due to full recorder queue",
            ["topic"],
        )
        self.recorder_direct_drops_total = Counter(
            _pn("recorder_direct_drops_total"),
            "Direct-path recorder queue drops in MarketDataService",
        )
        self.recorder_intent_drop_total = Counter(
            _pn("recorder_intent_drop_total"),
            "OrderIntent recorder drops in StrategyRunner producer hook (queue full)",
        )
        self.recorder_degraded_mode = Gauge(
            _pn("recorder_degraded_mode"),
            "Recorder degraded mode active (1=degraded, 0=normal)",
        )
        self.recorder_degraded_total = Counter(
            _pn("recorder_degraded_total"),
            "Times recorder entered degraded mode",
        )
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
        self.event_loop_probe_lag_ms = Histogram(
            _pn("event_loop_probe_lag_ms"),
            "Event loop scheduling overshoot measured by an idle probe task (ms)",
            buckets=[0.1, 0.25, 0.5, 1.0, 2.0, 5.0, 10.0, 25.0, 50.0, 100.0, 250.0, 500.0, 1000.0],
        )
        self.supervisor_tick_duration_ms = Histogram(
            _pn("supervisor_tick_duration_ms"),
            "Duration of one supervisor tick body, excluding the 1 Hz sleep (ms)",
            buckets=[0.1, 0.25, 0.5, 1.0, 2.0, 5.0, 10.0, 25.0, 50.0, 100.0, 250.0, 500.0, 1000.0],
        )
        self.startup_warnings_total = Counter(
            _pn("startup_warnings_total"),
            "Startup warnings by component",
            ["component"],
        )

        self.circuit_breaker_state = Gauge(
            _pn("circuit_breaker_state"),
            "Circuit breaker state (0=closed, 1=open)",
            ["component"],
        )
        self.dlq_size_total = Counter(
            _pn("dlq_size_total"),
            "Dead Letter Queue cumulative entry count",
            ["source"],
        )
        self.order_dlq_adds_total = Counter(
            _pn("order_dlq_adds_total"),
            "Orders written to the order dead-letter queue, by RejectionReason value",
            ["reason"],
        )
        self.reconciliation_discrepancy_count = Gauge(
            _pn("reconciliation_discrepancy_count"),
            "Number of position discrepancies detected",
        )
        self.reconciliation_sync_total = Counter(
            _pn("reconciliation_sync_total"),
            "Reconciliation sync outcomes",
            ["result"],
        )
        self.reconciliation_sync_duration_seconds = Histogram(
            _pn("reconciliation_sync_duration_seconds"),
            "Reconciliation sync duration in seconds",
            buckets=[0.01, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0],
        )
        self.reconciliation_discrepancy_total = Counter(
            _pn("reconciliation_discrepancy_total"),
            "Reconciliation discrepancies by severity",
            ["severity"],
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
        self.reconciliation_not_comparable = Gauge(
            _pn("reconciliation_not_comparable"),
            "1 when the broker snapshot cannot confirm platform positions (e.g. sim order routing)",
        )
        self.recorder_insert_retry_total = Counter(
            _pn("recorder_insert_retry_total"),
            "Recorder batch insert retry count",
            ["table", "result"],
        )
        self.recorder_insert_batches_total = Counter(
            _pn("recorder_insert_batches_total"),
            "Recorder insert batch final outcomes",
            ["table", "result"],
        )
        self.feed_gap_by_symbol_seconds = Gauge(
            _pn("feed_gap_by_symbol_seconds"),
            "Feed gap per symbol (seconds since last tick)",
            ["symbol"],
        )
        self.feed_gap_latched_silent_symbols_total = Counter(
            _pn("feed_gap_latched_silent_symbols_total"),
            "Latched symbols de-latched by get_active_feed_gap_s "
            "(reason captured in 'action' label, e.g. 'unsubscribed')",
            ["symbol", "action"],
        )

        self.shioaji_keepalive_failures_total = Counter(
            _pn("shioaji_keepalive_failures_total"),
            "Shioaji keep-alive check failures",
        )
        self.order_session_warmup_total = Counter(
            _pn("order_session_warmup_total"),
            "Order-session warm-up outcomes ahead of a market open",
            ["result"],
        )
        self.shioaji_session_down_total = Counter(
            _pn("shioaji_session_down_total"),
            "Shioaji broker session-down notifications delivered by the SDK",
        )
        self.quote_version_switch_total = Counter(
            _pn("quote_version_switch_total"),
            "Quote version switches (upgrade/downgrade)",
            ["direction"],
        )
        self.quote_schema_mismatch_total = Counter(
            _pn("quote_schema_mismatch_total"),
            "Quote callback payload schema mismatches rejected by schema guard",
            ["expected", "reason"],
        )
        self.shioaji_contract_lookup_errors_total = Counter(
            _pn("shioaji_contract_lookup_errors_total"),
            "Contract lookup failures by symbol",
            ["code"],
        )
        self.latency_spans_dropped_total = Counter(
            _pn("latency_spans_dropped_total"),
            "Latency spans dropped due to queue/buffer overflow",
        )
        self.clickhouse_connection_health = Gauge(
            _pn("clickhouse_connection_health"),
            "ClickHouse connection health (1=healthy, 0=unhealthy)",
        )
        self.wal_corrupt_files_total = Counter(
            _pn("wal_corrupt_files_total"),
            "Corrupt WAL files quarantined",
        )
        self.wal_orphan_tmp_cleaned_total = Counter(
            _pn("wal_orphan_tmp_cleaned_total"),
            "Orphan WAL tempfiles cleaned up after writer crash / SIGKILL",
            ["location"],
        )

        self.wal_batch_flush_total = Counter(
            _pn("wal_batch_flush_total"),
            "WAL batch flush operations at market close",
            ["result"],
        )
        self.wal_batch_flush_retry_total = Counter(
            _pn("wal_batch_flush_retry_total"),
            "WAL batch flush retry attempts",
        )
        self.session_refresh_total = Counter(
            _pn("session_refresh_total"),
            "Preventive session refresh operations",
            ["result"],
        )
        self.market_open_grace_active = Gauge(
            _pn("market_open_grace_active"),
            "Whether market open grace period is active (1=active, 0=inactive)",
        )
        self.market_trading_hours_active = Gauge(
            _pn("market_trading_hours_active"),
            "Whether the market-data watchdog currently considers the market open",
        )
        self.wal_directory_size_bytes = Gauge(
            _pn("wal_directory_size_bytes"),
            "Active-tier WAL directory size in bytes (legacy; use wal_directory_bytes for tiered)",
        )
        self.wal_file_count = Gauge(
            _pn("wal_file_count"),
            "Active-tier WAL file count (legacy; use wal_file_count_tiered for tiered)",
        )
        self.wal_oldest_file_age_seconds = Gauge(
            _pn("wal_oldest_file_age_seconds"),
            "Age of oldest active-tier WAL file in seconds",
        )
        self.wal_directory_bytes = Gauge(
            _pn("wal_directory_bytes"),
            "WAL directory size in bytes by tier (active=top-level *.jsonl, archive=archive/, dlq=dlq/)",
            ["tier"],
        )
        self.wal_file_count_tiered = Gauge(
            _pn("wal_file_count_tiered"),
            "WAL file count by tier (active=top-level, archive=archive/, dlq=dlq/)",
            ["tier"],
        )

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

        self.gateway_dedup_hits_total = Counter(
            _pn("gateway_dedup_hits_total"),
            "Idempotency cache hits (duplicate intents suppressed)",
        )
        self.gateway_reject_total = Counter(
            _pn("gateway_reject_total"),
            "Gateway rejected intents by reason",
            ["reason"],
        )
        self.gateway_dispatch_latency_ns = Histogram(
            _pn("gateway_dispatch_latency_ns"),
            "End-to-end gateway dispatch latency (ns): dedup→policy→exposure→risk→dispatch",
            buckets=[1_000, 5_000, 10_000, 50_000, 100_000, 500_000, 1_000_000, 5_000_000],
        )
        self.gateway_intent_channel_depth = Gauge(
            _pn("gateway_intent_channel_depth"),
            "Current depth of LocalIntentChannel queue",
        )
        self.gateway_dlq_size = Gauge(
            _pn("gateway_dlq_size"),
            "Gateway DLQ expired-intent count",
        )
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

        self.wal_mode = Gauge(
            _pn("wal_mode"),
            "Current recorder WAL mode (0=direct, 1=wal_first)",
        )
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
        self.wal_backlog_files = Gauge(
            _pn("wal_backlog_files"),
            "Number of WAL files pending replay",
        )
        self.wal_drain_eta_seconds = Gauge(
            _pn("wal_drain_eta_seconds"),
            "Estimated seconds to drain WAL backlog at current throughput",
        )
        self.disk_pressure_level = Gauge(
            _pn("disk_pressure_level"),
            "Current disk pressure level (0=ok, 1=warn, 2=critical, 3=halt)",
        )

        self.alpha_signal_events_total = Counter(
            _pn("alpha_signal_events_total"),
            "Alpha signal decisions by outcome",
            ["strategy", "outcome"],
        )
        self.stormguard_toxicity_score = Gauge(
            _pn("stormguard_toxicity_score"),
            "Latest drift-burst toxicity score [0,1] for the reference instrument",
            ["symbol"],
        )
        self.stormguard_escalations_total = Counter(
            _pn("stormguard_escalations_total"),
            "StormGuard escalations by target state",
            ["to_state"],
        )
        self.stormguard_latency_input_armed = Gauge(
            _pn("stormguard_latency_input_armed"),
            "Whether a StormGuard latency input has thresholds configured (1) or is unarmed (0)",
            ["input"],
        )
        self.stormguard_latency_input_max_us = Gauge(
            _pn("stormguard_latency_input_max_us"),
            "Largest value seen for a StormGuard latency input this process (microseconds)",
            ["input"],
        )
        self.drift_burst_detected_total = Counter(
            _pn("drift_burst_detected_total"),
            "Drift-burst detections by reference symbol and toxicity type",
            ["symbol", "toxicity_type"],
        )
        self.track_gate_intents_filtered_total = Counter(
            _pn("track_gate_intents_filtered_total"),
            "Intents dropped by the TrackGate session-phase filter",
            ["strategy", "symbol", "phase"],
        )
        self.alpha_last_signal_ts = Gauge(
            _pn("alpha_last_signal_ts"),
            "Unix timestamp of last non-flat alpha signal",
            ["strategy"],
        )

        self.position_age_seconds = Gauge(
            _pn("position_age_seconds"),
            "Seconds since last fill for a non-zero position (per strategy,symbol)",
            ["strategy", "symbol"],
        )
        self.position_stuck_alerts_total = Counter(
            _pn("position_stuck_alerts_total"),
            "Position-stuck telegram alerts emitted",
            ["strategy", "symbol"],
        )
        self.alpha_gate_results_total = Counter(
            _pn("alpha_gate_results_total"),
            "Alpha gate evaluation results",
            ["alpha_id", "gate", "result"],
        )
        self.alpha_promotion_results_total = Counter(
            _pn("alpha_promotion_results_total"),
            "Alpha promotion decisions",
            ["alpha_id", "result"],
        )
        self.alpha_canary_actions_total = Counter(
            _pn("alpha_canary_actions_total"),
            "Alpha canary state transitions",
            ["alpha_id", "action"],
        )
        self.alpha_kill_results_total = Counter(
            _pn("alpha_kill_results_total"),
            "Alpha kill-ledger appends grouped by gate and reason class",
            ["alpha_id", "gate", "reason_class"],
        )
        self.strategy_exceptions_total = Counter(
            _pn("strategy_exceptions_total"),
            "Strategy exceptions by type and handler method",
            ["strategy", "exception_type", "method"],
        )
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
        self.quote_watchdog_recovery_attempts_total = Counter(
            _pn("quote_watchdog_recovery_attempts_total"),
            "Quote watchdog recovery attempts by action",
            ["action"],
        )
        self.shioaji_quote_route_total = Counter(
            _pn("shioaji_quote_route_total"),
            "Shioaji quote callback route outcomes",
            ["result"],
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
            ["thread", "conn_id"],
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
        self.shioaji_login_slot_timeouts_total = Counter(
            _pn("shioaji_login_slot_timeouts_total"),
            "Session refresh proceeded without the login slot after waiting out its timeout",
        )
        self.feed_session_conflict_total = Counter(
            _pn("feed_session_conflict_total"),
            "Detected another runtime holding the feed session at startup (Redis preflight)",
            ["role"],
        )
        self.feed_session_lease_ops_total = Counter(
            _pn("feed_session_lease_ops_total"),
            "Redis feed session lease operation outcomes",
            ["op", "result"],
        )
        self.shioaji_logout_failed_total = Counter(
            _pn("shioaji_logout_failed_total"),
            "Shioaji logout() calls that raised while a session was being discarded (it may not have been released)",
            ["context"],
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
            ["result"],
        )
        self.md_callback_drop_total = Counter(
            _pn("md_callback_drop_total"),
            "Market data callback drops (broker thread)",
            ["reason"],
        )
        self.feature_plane_updates_total = Counter(
            _pn("feature_plane_updates_total"),
            "FeatureEngine runtime update outcomes",
            ["result", "feature_set"],
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
            ["flag"],
        )
        self.feature_shadow_parity_checks_total = Counter(
            _pn("feature_shadow_parity_checks_total"),
            "Feature shadow parity checks",
            ["feature_set", "result"],
        )
        self.feature_shadow_parity_mismatch_total = Counter(
            _pn("feature_shadow_parity_mismatch_total"),
            "Feature shadow parity mismatches",
            ["feature_set", "feature_id"],
        )
        self.feature_profile_activations_total = Counter(
            _pn("feature_profile_activations_total"),
            "Feature profile activations / rollbacks",
            ["feature_set", "profile_id", "action"],
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
            ["result"],
        )
        for result in ("ok", "error"):
            self.contract_refresh_total.labels(result=result)
        self.contract_cache_last_success_ts = Gauge(
            _pn("contract_cache_last_success_ts"),
            "Unix timestamp of the last contract cache write from a verified broker fetch",
        )
        self.contract_refresh_symbols_changed_total = Counter(
            _pn("contract_refresh_symbols_changed_total"),
            "Symbol changes detected after contract refresh",
            ["change"],
        )
        self.contract_update_events_total = Counter(
            _pn("contract_update_events_total"),
            "Contract-change events pushed by the broker over SYS/CONTRACT",
            ["action", "security_type"],
        )
        self.contract_update_last_event_ts = Gauge(
            _pn("contract_update_last_event_ts"),
            "Unix timestamp of the last broker contract-change announcement",
        )
        self.contract_event_callback_registered = Gauge(
            _pn("contract_event_callback_registered"),
            "1 if the SYS/CONTRACT push callback is registered on this facade, else 0",
            ["conn_id"],
        )

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

        self.exec_fill_data_loss_total = Counter(
            _pn("exec_fill_data_loss_total"),
            "Fills LOST because WAL writer unavailable and recorder queue full",
        )

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
        self.fill_dlq_persist_failed_total = Counter(
            _pn("fill_dlq_persist_failed_total"),
            "Orphaned fills quarantined because they could not be serialised or written to the DLQ file",
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
        self.audit_put_cross_thread_total = Counter(
            _pn("audit_put_cross_thread_total"),
            "I-M2: AuditWriter._put invocations from a non-loop thread "
            "(routed via call_soon_threadsafe instead of direct put_nowait)",
            ["table"],
        )
        self.audit_overflow_total = Counter(
            _pn("audit_overflow_total"),
            "Audit events spilled into the secondary overflow deque "
            "(primary asyncio.Queue was full at put_nowait time)",
            ["table"],
        )
        self.audit_persist_failures_total = Counter(
            _pn("audit_persist_failures_total"),
            "Audit batch writes that raised an exception against ClickHouse "
            "(payloads then fall through to the structlog audit_fallback path)",
            ["table", "reason"],
        )
        self.bot_handler_errors_total = Counter(
            _pn("bot_handler_errors_total"),
            "Telegram bot handler exceptions caught by the error handler "
            "(would have crashed the polling loop pre-P1-c)",
            ["exception"],
        )
        self.bot_dead_data_alerts_total = Counter(
            _pn("bot_dead_data_alerts_total"),
            "Consecutive-empty-attempt threshold breaches in the bot scheduler "
            "(N pushes in a row returned no_data for every configured symbol)",
            ["session"],
        )
        self.bot_rate_limited_total = Counter(
            _pn("bot_rate_limited_total"),
            "TelegramSender messages skipped due to client-side rate limit "
            "(non-critical sends within the rate-limit window)",
            ["critical"],
        )
        self.hft_build_info = Gauge(
            _pn("build_info"),
            "Build identity for this process (constant 1) — git_sha/build_ts describe the image, "
            "code_sha hashes the source tree actually running (bind mounts make these differ)",
            ["git_sha", "build_ts", "code_sha"],
        )
        try:
            _git_sha = os.environ.get("HFT_GIT_SHA", "unknown") or "unknown"
            _build_ts = os.environ.get("HFT_BUILD_TS", "unknown") or "unknown"
            self.hft_build_info.labels(git_sha=_git_sha, build_ts=_build_ts, code_sha=running_code_sha()).set(1)
        except Exception:
            pass
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

        self.recorder_reinject_circuit_breaker_drops_total = Counter(
            _pn("recorder_reinject_circuit_breaker_drops_total"),
            "Rows dropped by reinject circuit breaker after consecutive double-faults",
            ["table"],
        )

        self.strategy_events_received_total = Counter(
            _pn("strategy_events_received_total"),
            "Events dispatched to a strategy's handle_event (pre-call)",
            ["strategy_id"],
        )
        self.alias_resolution_coverage_ratio = Gauge(
            _pn("alias_resolution_coverage_ratio"),
            "Fraction of configured broker aliases propagated into SymbolMetadata (0.0-1.0; NaN when none configured)",
        )
        self.alias_map_size = Gauge(
            _pn("alias_map_size"),
            "Number of aliases the broker client resolved (0 disables alias-driven rebinds)",
        )
        self.strategy_bound_live_symbols = Gauge(
            _pn("strategy_bound_live_symbols"),
            "Count of a strategy's symbols that are actually present in the subscribed feed",
            ["strategy_id"],
        )
        self.front_month_subscribed = Gauge(
            _pn("front_month_subscribed"),
            "1 when the feed carries the exchange-rule front month of a traded futures root",
            ["root"],
        )
        self.front_month_strategy_on_front = Gauge(
            _pn("front_month_strategy_on_front"),
            "1 when every symbol a strategy holds for a futures root is the exchange-rule front month",
            ["strategy_id", "root"],
        )
        self.front_month_check_timestamp_seconds = Gauge(
            _pn("front_month_check_timestamp_seconds"),
            "Unix time of the last front-month watch evaluation",
        )
        self.feed_connect_gate_blocked_total = Counter(
            _pn("feed_connect_gate_blocked_total"),
            "Times a connect safety gate refused the connect before subscribing",
            ["gate"],
        )
        self.reconciliation_drift_streak = Gauge(
            _pn("reconciliation_drift_streak"),
            "Consecutive reconciliation observations of the same drift (resets to 0 on resolve)",
            ["symbol"],
        )
        self.reconciliation_untrusted_sample_streak = Gauge(
            _pn("reconciliation_untrusted_sample_streak"),
            "Consecutive reconciliation samples skipped as untrustworthy (straddled fill or partial broker snapshot)",
        )

        try:
            import psutil

            self.system_cpu_usage = Gauge(_pn("system_cpu_usage"), "CPU Usage Percent")
            self.system_memory_usage = Gauge(_pn("system_memory_usage"), "Memory Usage Percent")

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
        except Exception as _exc:
            pass


def get_metrics() -> "MetricsRegistry | None":
    """Return the MetricsRegistry singleton, or None if not yet initialised.

    Safe to call from any module — returns None rather than raising if the
    singleton has not been constructed yet (e.g. during unit tests that do not
    call MetricsRegistry.get() first).
    """
    return MetricsRegistry._instance
