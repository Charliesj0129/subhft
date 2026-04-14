"""Unit tests for PlatformDegradeInputs (ops/platform_inputs.py).

Covers:
- Default field values and construction
- configure_thresholds / bind_runtime_probes
- reduce_only_reasons: all individual trigger conditions
- _feed_gap_s: get_max_feed_gap_s + within_reconnect_window gating
- _quote_flap_budget_exceeded: various flap configurations
- _redis_is_healthy: healthcheck callable, redis client ping, absent
- _wal_backlog_files: direct attr, get_health dict, event_counts, metrics fallback
- _recorder_state: healthy, degraded, missing
- _read_rss_bytes: /proc/self/status parse (happy path; OS-level)
- _metric_sample_value: collection iteration
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from hft_platform.ops.platform_inputs import (
    PlatformDegradeInputs,
    _metric_sample_value,
    _read_rss_bytes,
)

# ---------------------------------------------------------------------------
# Helpers to build a default PlatformDegradeInputs
# ---------------------------------------------------------------------------


def _make_queue(size: int = 0) -> MagicMock:
    q = MagicMock()
    q.qsize.return_value = size
    return q


def _make_inputs(
    *,
    feed_gap: float = 0.0,
    within_window: bool = True,
    pending_since: float | None = None,
    quote_flap_events=None,
    quote_flap_threshold: int = 5,
    quote_flap_window_s: float = 60.0,
    queue_size: int = 0,
    rss_bytes: int = 0,
    wal_backlog: float | None = None,
    recorder_state: str = "",
    redis_healthy: bool | None = None,
) -> PlatformDegradeInputs:
    md_service = MagicMock()
    md_service.get_max_feed_gap_s.return_value = feed_gap
    md_service.within_reconnect_window.return_value = within_window
    md_service._pending_reconnect_since = pending_since
    md_service._quote_flap_events = quote_flap_events
    md_service._quote_flap_threshold = quote_flap_threshold
    md_service._quote_flap_window_s = quote_flap_window_s

    recorder = MagicMock()
    recorder.wal_backlog_files = None  # removed attr; default to no property
    del recorder.wal_backlog_files  # force AttributeError on attr access

    if wal_backlog is not None:
        recorder.wal_backlog_files = wal_backlog

    def make_health():
        if recorder_state:
            return {"state": recorder_state}
        return {}

    recorder.get_health.side_effect = make_health

    redis_client = None
    redis_healthcheck = None
    if redis_healthy is not None:
        redis_healthcheck = MagicMock(return_value=redis_healthy)

    inp = PlatformDegradeInputs(
        md_service=md_service,
        recorder=recorder,
        raw_queue=_make_queue(queue_size),
        raw_exec_queue=_make_queue(0),
        recorder_queue=_make_queue(0),
        risk_queue=_make_queue(0),
        order_queue=_make_queue(0),
        redis_client=redis_client,
        redis_healthcheck=redis_healthcheck,
        rss_reader=lambda: rss_bytes,
    )
    return inp


# ---------------------------------------------------------------------------
# Construction and defaults
# ---------------------------------------------------------------------------


class TestConstruction:
    def test_default_threshold_values(self) -> None:
        inp = _make_inputs()
        assert inp.feed_gap_threshold_s == 120.0
        assert inp.reconnect_pending_threshold_s == 60.0
        assert inp.reconnect_flap_budget == 5
        assert inp.queue_depth_threshold == 5000
        assert inp.rss_threshold_mb == 2048
        assert inp.wal_backlog_files_threshold == 200

    def test_slots_present(self) -> None:
        inp = _make_inputs()
        assert hasattr(inp, "__slots__")

    def test_no_reasons_when_all_healthy(self) -> None:
        inp = _make_inputs()
        with patch("hft_platform.ops.platform_inputs.timebase") as tb:
            tb.now_s.return_value = 0.0
            reasons = inp.reduce_only_reasons()
        assert reasons == []


# ---------------------------------------------------------------------------
# configure_thresholds
# ---------------------------------------------------------------------------


class TestConfigureThresholds:
    def test_updates_all_fields(self) -> None:
        inp = _make_inputs()
        inp.configure_thresholds(
            feed_gap_threshold_s=30.0,
            reconnect_pending_threshold_s=10.0,
            reconnect_flap_budget=3,
            queue_depth_threshold=1000,
            rss_threshold_mb=512,
            wal_backlog_files_threshold=50,
        )
        assert inp.feed_gap_threshold_s == 30.0
        assert inp.reconnect_pending_threshold_s == 10.0
        assert inp.reconnect_flap_budget == 3
        assert inp.queue_depth_threshold == 1000
        assert inp.rss_threshold_mb == 512
        assert inp.wal_backlog_files_threshold == 50

    def test_coerces_types(self) -> None:
        inp = _make_inputs()
        inp.configure_thresholds(
            feed_gap_threshold_s="99",  # type: ignore[arg-type]
            reconnect_pending_threshold_s="20",  # type: ignore[arg-type]
            reconnect_flap_budget="7",  # type: ignore[arg-type]
            queue_depth_threshold="2000",  # type: ignore[arg-type]
            rss_threshold_mb="1024",  # type: ignore[arg-type]
            wal_backlog_files_threshold="100",  # type: ignore[arg-type]
        )
        assert isinstance(inp.feed_gap_threshold_s, float)
        assert isinstance(inp.reconnect_flap_budget, int)


# ---------------------------------------------------------------------------
# bind_runtime_probes
# ---------------------------------------------------------------------------


class TestBindRuntimeProbes:
    def test_sets_redis_client_and_healthcheck(self) -> None:
        inp = _make_inputs()
        client = MagicMock()
        check = lambda: True
        inp.bind_runtime_probes(redis_client=client, redis_healthcheck=check)
        assert inp.redis_client is client
        assert inp.redis_healthcheck is check

    def test_clears_probes_when_none_passed(self) -> None:
        inp = _make_inputs(redis_healthy=True)
        inp.bind_runtime_probes(redis_client=None, redis_healthcheck=None)
        assert inp.redis_client is None
        assert inp.redis_healthcheck is None


# ---------------------------------------------------------------------------
# _feed_gap_s
# ---------------------------------------------------------------------------


class TestFeedGapS:
    def test_returns_zero_when_no_get_max_feed_gap_s(self) -> None:
        inp = _make_inputs()
        del inp.md_service.get_max_feed_gap_s  # remove attr
        inp.md_service.get_max_feed_gap_s = None  # type: ignore[assignment]
        assert inp._feed_gap_s() == 0.0

    def test_returns_zero_outside_reconnect_window(self) -> None:
        inp = _make_inputs(feed_gap=200.0, within_window=False)
        assert inp._feed_gap_s() == 0.0

    def test_returns_gap_inside_reconnect_window(self) -> None:
        inp = _make_inputs(feed_gap=150.0, within_window=True)
        assert inp._feed_gap_s() == 150.0

    def test_no_within_reconnect_window_attr_returns_raw_gap(self) -> None:
        inp = _make_inputs(feed_gap=50.0)
        inp.md_service.within_reconnect_window = None  # type: ignore[assignment]
        assert inp._feed_gap_s() == 50.0


# ---------------------------------------------------------------------------
# _quote_flap_budget_exceeded
# ---------------------------------------------------------------------------


class TestQuoteFlapBudgetExceeded:
    def test_returns_false_when_no_flap_events_attr(self) -> None:
        inp = _make_inputs()
        inp.md_service._quote_flap_events = None
        assert inp._quote_flap_budget_exceeded() is False

    def test_returns_false_when_threshold_zero(self) -> None:
        inp = _make_inputs()
        inp.md_service._quote_flap_events = [1, 2, 3]
        inp.md_service._quote_flap_threshold = 0
        assert inp._quote_flap_budget_exceeded() is False

    def test_returns_false_when_window_zero(self) -> None:
        inp = _make_inputs()
        inp.md_service._quote_flap_events = [1, 2, 3]
        inp.md_service._quote_flap_threshold = 2
        inp.md_service._quote_flap_window_s = 0.0
        assert inp._quote_flap_budget_exceeded() is False

    def test_returns_false_when_count_below_threshold(self) -> None:
        inp = _make_inputs(
            quote_flap_events=[1, 2],
            quote_flap_threshold=5,
            quote_flap_window_s=60.0,
        )
        assert inp._quote_flap_budget_exceeded() is False

    def test_returns_true_when_count_at_threshold(self) -> None:
        inp = _make_inputs(
            quote_flap_events=[1, 2, 3, 4, 5],
            quote_flap_threshold=5,
            quote_flap_window_s=60.0,
        )
        assert inp._quote_flap_budget_exceeded() is True

    def test_returns_true_when_count_exceeds_threshold(self) -> None:
        inp = _make_inputs(
            quote_flap_events=list(range(10)),
            quote_flap_threshold=5,
            quote_flap_window_s=60.0,
        )
        assert inp._quote_flap_budget_exceeded() is True


# ---------------------------------------------------------------------------
# _redis_is_healthy
# ---------------------------------------------------------------------------


class TestRedisIsHealthy:
    def test_returns_none_when_neither_healthcheck_nor_client(self) -> None:
        inp = _make_inputs()
        assert inp._redis_is_healthy() is None

    def test_uses_healthcheck_callable_when_present(self) -> None:
        check = MagicMock(return_value=True)
        inp = _make_inputs()
        inp.redis_healthcheck = check
        assert inp._redis_is_healthy() is True
        check.assert_called_once()

    def test_healthcheck_callable_exception_returns_false(self) -> None:
        check = MagicMock(side_effect=Exception("oops"))
        inp = _make_inputs()
        inp.redis_healthcheck = check
        assert inp._redis_is_healthy() is False

    def test_uses_redis_client_ping_when_no_healthcheck(self) -> None:
        client = MagicMock()
        client.ping.return_value = True
        inp = _make_inputs()
        inp.redis_client = client
        assert inp._redis_is_healthy() is True

    def test_redis_client_ping_exception_returns_false(self) -> None:
        client = MagicMock()
        client.ping.side_effect = Exception("connection refused")
        inp = _make_inputs()
        inp.redis_client = client
        assert inp._redis_is_healthy() is False


# ---------------------------------------------------------------------------
# _wal_backlog_files
# ---------------------------------------------------------------------------


class TestWalBacklogFiles:
    def test_reads_direct_attr(self) -> None:
        inp = _make_inputs(wal_backlog=42.0)
        assert inp._wal_backlog_files() == 42.0

    def _recorder_no_attr(self) -> MagicMock:
        """A recorder MagicMock that does NOT have wal_backlog_files attr."""
        recorder = MagicMock(spec=[])  # empty spec → hasattr returns False for anything
        recorder.get_health = MagicMock()
        return recorder

    def test_reads_from_get_health_dict(self) -> None:
        inp = _make_inputs()
        inp.recorder = self._recorder_no_attr()
        inp.recorder.get_health.return_value = {"wal_backlog_files": 15}
        assert inp._wal_backlog_files() == 15.0

    def test_reads_from_event_counts_wal_fallback(self) -> None:
        inp = _make_inputs()
        inp.recorder = self._recorder_no_attr()
        inp.recorder.get_health.return_value = {"event_counts": {"wal_fallback": 77}}
        assert inp._wal_backlog_files() == 77.0

    def test_returns_none_when_get_health_not_callable(self) -> None:
        inp = _make_inputs()
        inp.recorder = self._recorder_no_attr()
        inp.recorder.get_health = None  # type: ignore[assignment]
        inp.metrics = None
        assert inp._wal_backlog_files() is None

    def test_returns_none_when_no_relevant_key(self) -> None:
        inp = _make_inputs()
        inp.recorder = self._recorder_no_attr()
        inp.recorder.get_health.return_value = {"state": "OK"}
        inp.metrics = None
        assert inp._wal_backlog_files() is None


# ---------------------------------------------------------------------------
# _recorder_state
# ---------------------------------------------------------------------------


class TestRecorderState:
    def test_returns_empty_when_no_get_health(self) -> None:
        inp = _make_inputs()
        inp.recorder.get_health = None  # type: ignore[assignment]
        assert inp._recorder_state() == ""

    def test_returns_empty_when_get_health_raises(self) -> None:
        inp = _make_inputs()
        inp.recorder.get_health.side_effect = Exception("db down")
        assert inp._recorder_state() == ""

    def test_returns_uppercased_state(self) -> None:
        inp = _make_inputs(recorder_state="degraded")
        assert inp._recorder_state() == "DEGRADED"

    def test_returns_empty_for_non_dict_health(self) -> None:
        inp = _make_inputs()
        inp.recorder.get_health.side_effect = None
        inp.recorder.get_health.return_value = "string_response"
        assert inp._recorder_state() == ""


# ---------------------------------------------------------------------------
# reduce_only_reasons — individual trigger conditions
# ---------------------------------------------------------------------------


class TestReduceOnlyReasons:
    def _now(self) -> float:
        import time

        return time.time()

    def test_feed_gap_triggers_reason(self) -> None:
        inp = _make_inputs(feed_gap=200.0, within_window=True)
        inp.feed_gap_threshold_s = 120.0
        with patch("hft_platform.ops.platform_inputs.timebase") as tb:
            tb.now_s.return_value = self._now()
            reasons = inp.reduce_only_reasons()
        assert "feed_reconnect_unhealthy" in reasons

    def test_pending_reconnect_triggers_reason(self) -> None:
        now = self._now()
        inp = _make_inputs(pending_since=now - 120.0)  # 120s ago > 60s threshold
        with patch("hft_platform.ops.platform_inputs.timebase") as tb:
            tb.now_s.return_value = now
            reasons = inp.reduce_only_reasons()
        assert "feed_reconnect_pending" in reasons

    def test_pending_reconnect_not_triggered_within_threshold(self) -> None:
        now = self._now()
        inp = _make_inputs(pending_since=now - 10.0)  # only 10s ago
        with patch("hft_platform.ops.platform_inputs.timebase") as tb:
            tb.now_s.return_value = now
            reasons = inp.reduce_only_reasons()
        assert "feed_reconnect_pending" not in reasons

    def test_pending_reconnect_suppressed_outside_reconnect_window(self) -> None:
        """Session gap (e.g. 13:35-14:55) is expected; must not trigger reduce-only."""
        now = self._now()
        inp = _make_inputs(
            pending_since=now - 120.0,  # 120s > 60s threshold
            within_window=False,  # outside reconnect window
        )
        with patch("hft_platform.ops.platform_inputs.timebase") as tb:
            tb.now_s.return_value = now
            reasons = inp.reduce_only_reasons()
        assert "feed_reconnect_pending" not in reasons

    def test_pending_reconnect_fires_inside_reconnect_window(self) -> None:
        """When inside reconnect window and pending > threshold, must fire."""
        now = self._now()
        inp = _make_inputs(
            pending_since=now - 120.0,  # 120s > 60s threshold
            within_window=True,  # inside reconnect window
        )
        with patch("hft_platform.ops.platform_inputs.timebase") as tb:
            tb.now_s.return_value = now
            reasons = inp.reduce_only_reasons()
        assert "feed_reconnect_pending" in reasons

    def test_flap_triggers_reason(self) -> None:
        inp = _make_inputs(
            quote_flap_events=list(range(10)),
            quote_flap_threshold=5,
            quote_flap_window_s=60.0,
        )
        with patch("hft_platform.ops.platform_inputs.timebase") as tb:
            tb.now_s.return_value = self._now()
            reasons = inp.reduce_only_reasons()
        assert "feed_reconnect_flapping" in reasons

    def test_flap_not_triggered_when_budget_zero(self) -> None:
        inp = _make_inputs(
            quote_flap_events=list(range(10)),
            quote_flap_threshold=5,
            quote_flap_window_s=60.0,
        )
        inp.reconnect_flap_budget = 0
        with patch("hft_platform.ops.platform_inputs.timebase") as tb:
            tb.now_s.return_value = self._now()
            reasons = inp.reduce_only_reasons()
        assert "feed_reconnect_flapping" not in reasons

    def test_queue_depth_triggers_reason(self) -> None:
        inp = _make_inputs(queue_size=6000)
        with patch("hft_platform.ops.platform_inputs.timebase") as tb:
            tb.now_s.return_value = self._now()
            reasons = inp.reduce_only_reasons()
        assert "queue_depth_exceeded" in reasons

    def test_rss_triggers_reason(self) -> None:
        inp = _make_inputs(rss_bytes=3 * 1024 * 1024 * 1024)  # 3 GB > 2048 MB
        with patch("hft_platform.ops.platform_inputs.timebase") as tb:
            tb.now_s.return_value = self._now()
            reasons = inp.reduce_only_reasons()
        assert "rss_unhealthy" in reasons

    def test_rss_not_triggered_when_threshold_zero(self) -> None:
        inp = _make_inputs(rss_bytes=3 * 1024 * 1024 * 1024)
        inp.rss_threshold_mb = 0
        with patch("hft_platform.ops.platform_inputs.timebase") as tb:
            tb.now_s.return_value = self._now()
            reasons = inp.reduce_only_reasons()
        assert "rss_unhealthy" not in reasons

    def test_redis_unhealthy_triggers_reason(self) -> None:
        inp = _make_inputs(redis_healthy=False)
        with patch("hft_platform.ops.platform_inputs.timebase") as tb:
            tb.now_s.return_value = self._now()
            reasons = inp.reduce_only_reasons()
        assert "redis_unhealthy" in reasons

    def test_wal_backlog_triggers_reason(self) -> None:
        inp = _make_inputs(wal_backlog=250.0)  # > 200 threshold
        with patch("hft_platform.ops.platform_inputs.timebase") as tb:
            tb.now_s.return_value = self._now()
            reasons = inp.reduce_only_reasons()
        assert "wal_backlog_unhealthy" in reasons

    def test_wal_backlog_not_triggered_when_threshold_zero(self) -> None:
        inp = _make_inputs(wal_backlog=250.0)
        inp.wal_backlog_files_threshold = 0
        with patch("hft_platform.ops.platform_inputs.timebase") as tb:
            tb.now_s.return_value = self._now()
            reasons = inp.reduce_only_reasons()
        assert "wal_backlog_unhealthy" not in reasons

    @pytest.mark.parametrize("state", ["DEGRADED", "CRITICAL"])
    def test_clickhouse_unhealthy_triggers_reason(self, state: str) -> None:
        inp = _make_inputs(recorder_state=state)
        with patch("hft_platform.ops.platform_inputs.timebase") as tb:
            tb.now_s.return_value = self._now()
            reasons = inp.reduce_only_reasons()
        assert "clickhouse_unhealthy" in reasons

    def test_data_loss_triggers_recorder_data_loss_reason(self) -> None:
        inp = _make_inputs(recorder_state="DATA_LOSS")
        with patch("hft_platform.ops.platform_inputs.timebase") as tb:
            tb.now_s.return_value = self._now()
            reasons = inp.reduce_only_reasons()
        assert "recorder_data_loss" in reasons
        assert "clickhouse_unhealthy" not in reasons

    def test_clickhouse_healthy_does_not_trigger(self) -> None:
        inp = _make_inputs(recorder_state="OK")
        with patch("hft_platform.ops.platform_inputs.timebase") as tb:
            tb.now_s.return_value = self._now()
            reasons = inp.reduce_only_reasons()
        assert "clickhouse_unhealthy" not in reasons

    def test_no_duplicate_reasons(self) -> None:
        # Trigger feed gap twice; result must deduplicate
        inp = _make_inputs(feed_gap=500.0, within_window=True)
        with patch("hft_platform.ops.platform_inputs.timebase") as tb:
            tb.now_s.return_value = self._now()
            reasons = inp.reduce_only_reasons()
        assert len(reasons) == len(set(reasons))


# ---------------------------------------------------------------------------
# _read_rss_bytes (module-level helper)
# ---------------------------------------------------------------------------


class TestReadRssBytes:
    def test_returns_integer(self) -> None:
        # Real /proc/self/status exists on Linux; check basic contract
        result = _read_rss_bytes()
        assert isinstance(result, int)
        assert result >= 0

    def test_returns_zero_on_oserror(self) -> None:
        with patch("builtins.open", side_effect=OSError("no file")):
            result = _read_rss_bytes()
        assert result == 0


# ---------------------------------------------------------------------------
# _metric_sample_value (module-level helper)
# ---------------------------------------------------------------------------


class TestMetricSampleValue:
    def test_returns_value_for_matching_sample(self) -> None:
        sample = MagicMock()
        sample.name = "my_metric"
        sample.labels = {}
        sample.value = 42.0

        family = MagicMock()
        family.samples = [sample]

        metric = MagicMock()
        metric.collect.return_value = [family]

        result = _metric_sample_value(metric, "my_metric")
        assert result == 42.0

    def test_returns_none_when_no_match(self) -> None:
        sample = MagicMock()
        sample.name = "other_metric"
        sample.labels = {}
        sample.value = 1.0

        family = MagicMock()
        family.samples = [sample]

        metric = MagicMock()
        metric.collect.return_value = [family]

        result = _metric_sample_value(metric, "my_metric")
        assert result is None

    def test_returns_none_when_collect_raises(self) -> None:
        metric = MagicMock()
        metric.collect.side_effect = RuntimeError("broken")
        result = _metric_sample_value(metric, "my_metric")
        assert result is None

    def test_matches_labels_filter(self) -> None:
        sample_match = MagicMock()
        sample_match.name = "my_metric"
        sample_match.labels = {"env": "prod"}
        sample_match.value = 7.0

        sample_miss = MagicMock()
        sample_miss.name = "my_metric"
        sample_miss.labels = {"env": "test"}
        sample_miss.value = 3.0

        family = MagicMock()
        family.samples = [sample_miss, sample_match]

        metric = MagicMock()
        metric.collect.return_value = [family]

        result = _metric_sample_value(metric, "my_metric", labels={"env": "prod"})
        assert result == 7.0


# ---------------------------------------------------------------------------
# Gateway queue monitoring (intent_channel + api_queue)
# ---------------------------------------------------------------------------


class TestGatewayQueueMonitoring:
    def test_intent_channel_backpressure_triggers_degrade(self) -> None:
        """intent_channel depth exceeding threshold must trigger queue_depth_exceeded."""
        inp = _make_inputs(queue_size=0)  # all legacy queues at 0
        intent_ch = _make_queue(6000)
        inp.intent_channel = intent_ch
        reasons = inp.reduce_only_reasons()
        assert "queue_depth_exceeded" in reasons

    def test_api_queue_backpressure_triggers_degrade(self) -> None:
        """api_queue depth exceeding threshold must trigger queue_depth_exceeded."""
        inp = _make_inputs(queue_size=0)
        api_q = _make_queue(6000)
        inp.api_queue = api_q
        reasons = inp.reduce_only_reasons()
        assert "queue_depth_exceeded" in reasons

    def test_none_gateway_queues_are_safe(self) -> None:
        """When intent_channel/api_queue are None, reduce_only_reasons must not crash."""
        inp = _make_inputs(queue_size=0)
        assert inp.intent_channel is None
        assert inp.api_queue is None
        reasons = inp.reduce_only_reasons()
        assert "queue_depth_exceeded" not in reasons

    def test_gateway_queues_below_threshold_no_degrade(self) -> None:
        """Gateway queues below threshold must not trigger degradation."""
        inp = _make_inputs(queue_size=0)
        inp.intent_channel = _make_queue(10)
        inp.api_queue = _make_queue(5)
        reasons = inp.reduce_only_reasons()
        assert "queue_depth_exceeded" not in reasons
