"""Regression tests for config/monitoring/alerts/rules.yaml alert expressions.

These tests catch structural mistakes in the Prometheus alert YAML that would
silently produce false positives or false negatives in production.

Scope: YAML structure and PromQL expression invariants only. Full PromQL
semantic correctness requires `promtool test rules` and is not covered here.
"""

from __future__ import annotations

from pathlib import Path

import yaml

RULES_PATH = Path(__file__).resolve().parents[2] / "config" / "monitoring" / "alerts" / "rules.yaml"


def _load_alerts_by_name() -> dict[str, dict]:
    with RULES_PATH.open() as f:
        data = yaml.safe_load(f)
    alerts: dict[str, dict] = {}
    for group in data.get("groups", []):
        for rule in group.get("rules", []):
            name = rule.get("alert")
            if name:
                alerts[name] = rule
    return alerts


def test_alpha_signal_silent_gates_on_nonzero_timestamp():
    """AlphaSignalSilent must not fire when `alpha_last_signal_ts` is 0.

    The gauge defaults to 0 for any registered strategy that has not yet emitted
    an intent (e.g. config-disabled strategies that still have executor entries
    built in StrategyRunner). Without a `> 0` gate, `time() - 0` always exceeds
    300 and the alert fires from engine startup forever.
    """
    alerts = _load_alerts_by_name()
    assert "AlphaSignalSilent" in alerts, "AlphaSignalSilent alert missing from rules.yaml"
    expr = alerts["AlphaSignalSilent"]["expr"]
    assert "alpha_last_signal_ts > 0" in expr, (
        "AlphaSignalSilent expression must gate on a non-zero gauge value to prevent "
        "false positives for strategies that have never emitted an intent. "
        f"Current expression: {expr!r}"
    )


def test_alpha_signal_silent_uses_any_alpha_decision_activity():
    """AlphaSignalSilent must not fire while a strategy is actively deciding flat.

    `alpha_last_signal_ts` is intentionally updated only for non-flat intents.
    Strategies can remain healthy while emitting only `flat` outcomes, so the
    alert must use `alpha_signal_events_total` activity to detect real pipeline
    silence instead of treating no new non-flat signal as no alpha activity.
    """
    alerts = _load_alerts_by_name()
    assert "AlphaSignalSilent" in alerts, "AlphaSignalSilent alert missing from rules.yaml"
    expr = alerts["AlphaSignalSilent"]["expr"]
    assert "increase(alpha_signal_events_total[5m])" in expr, (
        "AlphaSignalSilent must gate on recent alpha decision activity so flat "
        f"decisions do not page as pipeline silence. Current expression: {expr!r}"
    )
    assert 'outcome="intent"' not in expr, (
        f"AlphaSignalSilent must count both intent and flat outcomes. Current expression: {expr!r}"
    )


def test_feature_quality_flags_spike_excludes_design_time_signals():
    """FeatureQualityFlagsSpike must only page on true corruption flags.

    Three flags represent feature-state hygiene events that are NOT corruption:

    - ``partial``: crossed/empty book warmup-style updates; accepted by strategy
      feature gating.
    - ``state_reset``: emitted by ``FeatureEngine.reset_symbol(s)`` /
      ``reset_all`` after legitimate facade reconnect/warmup. Each emission is
      a one-tick signal that the next feature update for the symbol is fresh
      state; thinly-traded symbols trickle in for hours after a single startup
      reset, so bundling it with corruption causes false-positive paging.
    - ``stale_input``: feed-staleness is covered by ``FeedGapCritical`` and
      ``FeedFreshness*`` rules directly, not by the feature-quality bundle.

    Only ``gap`` (lost data) and ``out_of_order`` (sequence inversion) belong
    here -- both indicate the downstream feature pipeline cannot trust its
    inputs. Reconnect-storm visibility belongs on a dedicated rule keyed off
    ``facade_warmup_reset`` / ``feed_resubscribe_total`` once those counters
    exist.
    """
    alerts = _load_alerts_by_name()
    assert "FeatureQualityFlagsSpike" in alerts
    expr = alerts["FeatureQualityFlagsSpike"]["expr"]
    for benign in ("partial", "state_reset", "stale_input"):
        assert benign not in expr, (
            f"FeatureQualityFlagsSpike must not include {benign!r} "
            f"(design-time signal, not corruption). Current expression: {expr!r}"
        )
    for corrupt in ("gap", "out_of_order"):
        assert corrupt in expr, (
            f"FeatureQualityFlagsSpike must continue to page on {corrupt!r}. Current expression: {expr!r}"
        )


def test_feed_gap_critical_gates_on_trading_hours():
    """FeedGapCritical must not fire during exchange holidays or closed sessions."""
    alerts = _load_alerts_by_name()
    assert "FeedGapCritical" in alerts
    expr = alerts["FeedGapCritical"]["expr"]
    assert "market_trading_hours_active == 1" in expr, (
        "FeedGapCritical must use the runtime trading-hours gauge so a restart "
        f"during holidays/off-hours does not page Telegram. Current expression: {expr!r}"
    )


def test_shioaji_watchdog_thread_down_gates_on_trading_hours():
    """ShioajiWatchdogThreadDown must not page during holidays/off-hours.

    The quote watchdog intentionally skips recovery outside trading hours. If
    the broker session is refreshed or logged out during a closed market, the
    thread liveness gauge can be 0 without live market-data risk.
    """
    alerts = _load_alerts_by_name()
    assert "ShioajiWatchdogThreadDown" in alerts
    expr = alerts["ShioajiWatchdogThreadDown"]["expr"]
    assert "market_trading_hours_active == 1" in expr, (
        "ShioajiWatchdogThreadDown must use the runtime trading-hours gauge so "
        f"weekends/holidays do not page Telegram. Current expression: {expr!r}"
    )


def test_shioaji_crash_signature_detected_gates_on_trading_hours():
    """ShioajiCrashSignatureDetected must not page during holidays/off-hours.

    The Shioaji Solace C library intermittently corrupts the Python heap during
    `subscribe_symbol` and segfaults the engine, which Docker `restart_policy=always`
    relaunches. Outside trading hours those restarts are operational noise — the
    engine is not handling live market data and we cannot fix the broker C lib.
    Same rationale as ShioajiWatchdogThreadDown.
    """
    alerts = _load_alerts_by_name()
    assert "ShioajiCrashSignatureDetected" in alerts
    expr = alerts["ShioajiCrashSignatureDetected"]["expr"]
    assert "market_trading_hours_active == 1" in expr, (
        "ShioajiCrashSignatureDetected must use the runtime trading-hours gauge "
        f"so weekends/holidays do not page Telegram on each engine restart. "
        f"Current expression: {expr!r}"
    )


def test_feed_reconnect_failure_ratio_high_gates_on_trading_hours():
    """FeedReconnectFailureRatioHigh must not page during holidays/off-hours.

    Reconnect retries during an engine restart loop on a closed market are
    operational noise. The actionable feed-reconnect failures happen during
    trading hours when live market data is at risk.
    """
    alerts = _load_alerts_by_name()
    assert "FeedReconnectFailureRatioHigh" in alerts
    expr = alerts["FeedReconnectFailureRatioHigh"]["expr"]
    assert "market_trading_hours_active == 1" in expr, (
        "FeedReconnectFailureRatioHigh must use the runtime trading-hours gauge "
        f"so weekends/holidays do not page Telegram. Current expression: {expr!r}"
    )


def test_feature_plane_latency_threshold_above_measured_p99():
    """FeaturePlaneLatencyP99High threshold must be realistic for the deploy target.

    Measured baseline on WSL+Docker (2026-04-25): P50=77us, P95=154us, P99=191us.
    The original 50us (5e4 ns) threshold was below P50 and fired continuously.
    The threshold should be high enough to ride normal variance but still catch
    genuine regressions. This test guards against accidentally reverting to the
    pathologically-tight 50us value.
    """
    alerts = _load_alerts_by_name()
    assert "FeaturePlaneLatencyP99High" in alerts
    expr = alerts["FeaturePlaneLatencyP99High"]["expr"]
    assert "> 5e4" not in expr, (
        "50us threshold is below the measured P50 (77us) on the current deploy "
        "target. Use 5e5 (500us) or tune against a fresh measurement."
    )
    assert "> 5e5" in expr or "> 500000" in expr, f"Expected threshold >= 500us. Current expression: {expr!r}"


def test_backup_stale_gates_on_nonzero_timestamp():
    """BackupStale must gate on `hft_backup_last_success_ts > 0`.

    Same bug class as AlphaSignalSilent: the backup cron runs as a one-shot
    `docker compose exec hft-engine python -c ...` subprocess whose MetricsRegistry
    is separate from the engine's scrape target. The gauge therefore never leaves
    0, and `0 < time() - 172800` is always true — the alert fires forever despite
    backups succeeding daily. Until the metric pipeline is fixed (Pushgateway,
    textfile collector, or engine-side file mtime polling), the gate prevents
    the false positive.
    """
    alerts = _load_alerts_by_name()
    assert "BackupStale" in alerts
    expr = alerts["BackupStale"]["expr"]
    assert "hft_backup_last_success_ts > 0" in expr, (
        f"BackupStale must gate on a non-zero gauge. Current expression: {expr!r}"
    )


def test_execution_gateway_heartbeat_stale_gates_on_nonzero_timestamp():
    """ExecutionGatewayHeartbeatStale must not fire when heartbeat is 0.

    When HFT_GATEWAY_ENABLED=0 the gateway never advances
    `execution_gateway_heartbeat_ts`, so it stays at the default 0. Without
    the `> 0` gate `(time() - 0) > 60` is always true and the alert fires
    forever on engines that intentionally don't run the gateway. Same bug
    class as AlphaSignalSilent and BackupStale.
    """
    alerts = _load_alerts_by_name()
    assert "ExecutionGatewayHeartbeatStale" in alerts
    expr = alerts["ExecutionGatewayHeartbeatStale"]["expr"]
    assert "execution_gateway_heartbeat_ts > 0" in expr, (
        "ExecutionGatewayHeartbeatStale must gate on a non-zero heartbeat so "
        "engines with HFT_GATEWAY_ENABLED=0 do not page continuously. "
        f"Current expression: {expr!r}"
    )


def test_feed_subscription_permanently_failed_alerts_on_emitted_metric():
    """A permanently-failed subscription must page — it is the only signal that a
    stale ``config/symbols.yaml`` (expired month code after a contract roll) has
    left the engine subscribing to a contract the broker has dropped.

    Root cause this guards (2026-05-21 May roll): pool-mode engines no longer
    auto-rebuild ``symbols.yaml`` (Fix-1, 2026-05-23), so an un-regenerated YAML
    keeps literal ``TXFE6``/``MXFE6`` codes that the broker has delisted. Those
    subscriptions cross ``HFT_SUB_RETRY_MAX_ATTEMPTS`` and stop retrying, bumping
    ``feed_subscription_permanent_failures_total`` (emitted at
    ``quote_runtime._bump_permanent_metric``). Before this alert the condition was
    recorded but never routed to Telegram, so it accrued silently for days.
    """
    alerts = _load_alerts_by_name()
    assert "FeedSubscriptionPermanentlyFailed" in alerts, (
        "FeedSubscriptionPermanentlyFailed alert missing from rules.yaml — a "
        "stale post-roll symbols.yaml would page nobody."
    )
    expr = alerts["FeedSubscriptionPermanentlyFailed"]["expr"]
    assert "feed_subscription_permanent_failures_total" in expr, (
        "Alert must key off the counter that quote_runtime actually emits at "
        f"permanent failure. Current expression: {expr!r}"
    )
    # House invariant (same as FeedGapCritical / ShioajiCrashSignatureDetected):
    # gate on trading hours so off-hours Shioaji restart loops do not page.
    assert "market_trading_hours_active == 1" in expr, (
        "FeedSubscriptionPermanentlyFailed must gate on the trading-hours gauge "
        f"so weekend/holiday restart noise does not page Telegram. Current: {expr!r}"
    )


def test_redis_connection_down_alert_removed():
    """RedisConnectionDown must not exist — engine does not probe Redis.

    The `redis_connection_health` gauge has no setter anywhere in the codebase
    (engine container lacks the `redis` Python module; only the separate
    monitor container uses Redis via `hft_platform.monitor._redis_*`). The
    alert therefore always fires on a gauge that is default-initialised to 0.
    """
    alerts = _load_alerts_by_name()
    assert "RedisConnectionDown" not in alerts, (
        "RedisConnectionDown alert references a gauge that no engine-side code "
        "ever sets. Remove the alert (and the dead gauge) until Redis health is "
        "probed from the component that actually uses Redis."
    )
