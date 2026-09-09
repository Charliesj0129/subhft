"""The broker order path must be measurable above StormGuard's own threshold.

On 2026-09-09 a production latency report was filed against the wrong metric.
``e2e_order_latency_ns`` was read as if it were the broker round-trip, and it
is not: it runs from ``OrderCommand.created_ns`` to ``FillEvent.ingest_ts_ns``,
so for a passive maker it is dominated by the time the quote rests in the book.
The report claimed a 20x regression; the correct instrument,
``pipeline_latency_ns{stage="api_place_order"}``, did not support it.

Reading the correct instrument then hit a second problem. Its top finite
bucket was 1 s, and StormGuard escalates to STORM at exactly 1 s of order RTT
(``RiskThresholds.order_rtt_storm_us``). ``histogram_quantile`` cannot
interpolate into ``+Inf``, so it returns the top finite bound for anything
heavier -- the live day-session p99 read a flat 1000.0 ms for four consecutive
hours. The one region the breaker actually fires in was the one region the
histogram could not describe, and a saturated quantile looks exactly like a
stable one.

These tests pin both halves: the buckets reach past the threshold, and the
help text says what the e2e metric actually measures.
"""

from prometheus_client import CollectorRegistry, Histogram

from hft_platform.observability.metrics import MetricsRegistry


def _bucket_bounds(metric) -> list[float]:
    """Upper bounds of a Histogram's finite buckets, in order."""
    return [float(b) for b in metric._upper_bounds if b != float("inf")]


class TestThePipelineHistogramReachesPastTheStormThreshold:
    def test_a_three_second_broker_call_is_not_pushed_into_the_infinity_bucket(self):
        MetricsRegistry._instance = None
        bounds = _bucket_bounds(MetricsRegistry.get().pipeline_latency_ns)
        # 3.013641 s is the real worst order RTT recorded on THESHOW, exported
        # as stormguard_latency_input_max_us{input="order_rtt"} on 2026-09-09.
        worst_seen_ns = 3_013_641_000
        assert any(b >= worst_seen_ns for b in bounds), (
            f"the worst observed broker call ({worst_seen_ns / 1e9:.2f}s) lands in +Inf; "
            f"top finite bucket is {max(bounds) / 1e9:.2f}s"
        )

    def test_the_buckets_straddle_the_storm_threshold_they_must_describe(self):
        from hft_platform.risk.storm_guard import RiskThresholds

        MetricsRegistry._instance = None
        bounds = _bucket_bounds(MetricsRegistry.get().pipeline_latency_ns)
        storm_ns = RiskThresholds().order_rtt_storm_us * 1_000
        assert storm_ns > 0, "order_rtt STORM threshold is disarmed; this test is vacuous"
        assert any(b > storm_ns for b in bounds), (
            "no bucket sits above the order_rtt STORM threshold, so a quantile can "
            "never distinguish 'just over the line' from 'not answering at all'"
        )

    def test_the_buckets_stay_sorted_and_unique(self):
        MetricsRegistry._instance = None
        bounds = _bucket_bounds(MetricsRegistry.get().pipeline_latency_ns)
        assert bounds == sorted(bounds)
        assert len(bounds) == len(set(bounds))

    def test_a_three_second_observation_is_counted_below_the_top_bucket(self):
        """End-to-end through prometheus_client, not just the bound list."""
        MetricsRegistry._instance = None
        bounds = _bucket_bounds(MetricsRegistry.get().pipeline_latency_ns)
        registry = CollectorRegistry()
        probe = Histogram("probe_ns", "doc", ["stage"], buckets=bounds, registry=registry)
        probe.labels(stage="api_place_order").observe(3_013_641_000)

        # Read the buckets back off the collector rather than guessing how
        # prometheus_client formats a float in an ``le`` label.
        counts = {s.labels["le"]: s.value for m in probe.collect() for s in m.samples if s.name.endswith("_bucket")}
        assert counts.get("+Inf") == 1.0, "the observation was not recorded at all"
        finite = {le: v for le, v in counts.items() if le != "+Inf"}
        top_le = max(finite, key=lambda le: float(le))
        assert finite[top_le] == 1.0, (
            f"3s observation fell past the top finite bucket ({top_le}); "
            "histogram_quantile would saturate instead of reporting it"
        )


class TestTheE2eMetricSaysWhatItMeasures:
    def test_the_help_text_denies_being_a_broker_round_trip(self):
        MetricsRegistry._instance = None
        doc = MetricsRegistry.get().e2e_order_latency_ns._documentation
        lowered = doc.lower()
        assert "rtt" in lowered or "round" in lowered, (
            "help text does not mention broker RTT at all, so nothing warns the "
            f"next reader off the misreading that produced a false report: {doc!r}"
        )
        assert "not" in lowered, f"help text does not deny being broker RTT: {doc!r}"

    def test_the_help_text_names_the_metric_to_use_instead(self):
        MetricsRegistry._instance = None
        doc = MetricsRegistry.get().e2e_order_latency_ns._documentation
        assert "api_place_order" in doc, f"help text denies what it is without naming what to use instead: {doc!r}"
