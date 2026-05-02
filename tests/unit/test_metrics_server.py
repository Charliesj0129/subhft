from prometheus_client.core import CounterMetricFamily, GaugeMetricFamily

from hft_platform.observability import metrics_server


class GoodCollector:
    def collect(self):
        gauge = GaugeMetricFamily(
            "platform_reduce_only_active",
            "Whether reduce-only is active",
        )
        gauge.add_metric([], 1.0)

        counter = CounterMetricFamily(
            "feed_events",
            "Feed events",
            labels=["type"],
        )
        counter.add_metric(["tick"], 7.0)

        yield gauge
        yield counter


class BadCollector:
    def collect(self):
        raise TypeError("corrupted collector")


def test_partial_metrics_uses_sample_names_without_duplication(monkeypatch):
    monkeypatch.setattr(
        metrics_server.REGISTRY,
        "_names_to_collectors",
        {
            "platform_reduce_only_active": GoodCollector(),
            "broken_metric": BadCollector(),
        },
    )
    status_headers = []

    def start_response(status, headers):
        status_headers.append((status, headers))

    body = b"".join(metrics_server._collect_partial(start_response)).decode()

    assert "platform_reduce_only_active 1.0" in body
    assert 'feed_events_total{type="tick"} 7.0' in body
    assert "platform_reduce_only_activeplatform_reduce_only_active" not in body
    assert "feed_eventsfeed_events_total" not in body
    assert "# skipped 1 corrupted collector(s)" in body
    assert status_headers[0][0] == "200 OK"
