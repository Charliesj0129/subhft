"""Unit tests for data quality profiler (Unit 7)."""

from __future__ import annotations

from hft_platform.data_quality.profiler import DataProfiler, SymbolProfile


class TestSymbolProfile:
    def test_to_dict(self):
        profile = SymbolProfile(
            symbol="2330",
            date="2026-03-18",
            tick_count=1000,
            min_price_scaled=5000000,
            max_price_scaled=5100000,
            median_volume=100.0,
            mean_spread_scaled=500.0,
            gap_count=2,
            max_gap_seconds=8.5,
            anomaly_flags=["gap_count=2"],
        )
        d = profile.to_dict()
        assert d["symbol"] == "2330"
        assert d["tick_count"] == 1000
        assert "gap_count=2" in d["anomaly_flags"]


class TestDataProfiler:
    def test_empty_records(self):
        profiler = DataProfiler()
        profile = profiler.profile_symbol("2330", "2026-03-18", [])
        assert profile.tick_count == 0
        assert "no_data" in profile.anomaly_flags

    def test_normal_data_no_anomalies(self):
        profiler = DataProfiler()
        records = [
            {"price_scaled": 5000000 + i * 100, "volume": 100, "timestamp_ns": 1000000000 * i} for i in range(100)
        ]
        profile = profiler.profile_symbol("2330", "2026-03-18", records)
        assert profile.tick_count == 100
        assert profile.min_price_scaled == 5000000
        assert profile.max_price_scaled == 5000000 + 99 * 100
        # No anomalies with this well-behaved data
        price_outlier_flags = [f for f in profile.anomaly_flags if "price_outlier" in f]
        volume_spike_flags = [f for f in profile.anomaly_flags if "volume_spike" in f]
        assert len(volume_spike_flags) == 0

    def test_price_outlier_detection(self):
        profiler = DataProfiler(price_sigma_threshold=3.0)
        # 99 normal prices + 1 extreme outlier
        records = [{"price_scaled": 5000000, "volume": 100, "timestamp_ns": 1000000000 * i} for i in range(99)]
        records.append({"price_scaled": 99999999, "volume": 100, "timestamp_ns": 99000000000})
        profile = profiler.profile_symbol("2330", "2026-03-18", records)
        assert any("price_outlier" in f for f in profile.anomaly_flags)

    def test_volume_spike_detection(self):
        profiler = DataProfiler(volume_spike_multiplier=5.0)
        records = [{"price_scaled": 5000000, "volume": 100, "timestamp_ns": 1000000000 * i} for i in range(98)]
        # Add 2 volume spikes (>5x median)
        records.append({"price_scaled": 5000000, "volume": 1000, "timestamp_ns": 98000000000})
        records.append({"price_scaled": 5000000, "volume": 2000, "timestamp_ns": 99000000000})
        profile = profiler.profile_symbol("2330", "2026-03-18", records)
        assert any("volume_spike" in f for f in profile.anomaly_flags)

    def test_gap_detection(self):
        profiler = DataProfiler(gap_threshold_seconds=5.0)
        records = [{"price_scaled": 5000000, "volume": 100, "timestamp_ns": 1000000000 * i} for i in range(10)]
        # Add a 10-second gap
        records.append({"price_scaled": 5000000, "volume": 100, "timestamp_ns": 20000000000})
        profile = profiler.profile_symbol("2330", "2026-03-18", records)
        assert profile.gap_count >= 1
        assert profile.max_gap_seconds >= 5.0
        assert any("gap_count" in f for f in profile.anomaly_flags)

    def test_with_spread_field(self):
        profiler = DataProfiler()
        records = [
            {"price_scaled": 5000000, "volume": 100, "timestamp_ns": i * 1000000000, "spread_scaled": 500}
            for i in range(10)
        ]
        profile = profiler.profile_symbol("2330", "2026-03-18", records)
        assert profile.mean_spread_scaled == 500.0

    def test_single_record(self):
        profiler = DataProfiler()
        records = [{"price_scaled": 5000000, "volume": 100, "timestamp_ns": 1000000000}]
        profile = profiler.profile_symbol("2330", "2026-03-18", records)
        assert profile.tick_count == 1
        assert profile.gap_count == 0
