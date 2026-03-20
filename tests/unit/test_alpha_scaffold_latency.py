"""Unit tests for latency profile auto-population in scaffold (Unit 4)."""

from __future__ import annotations

from research.registry.schemas import AlphaManifest


class TestAlphaManifestLatencyDefault:
    def test_default_latency_profile(self):
        """New AlphaManifest should have default latency profile."""
        m = AlphaManifest(
            alpha_id="test_alpha",
            hypothesis="Test",
            formula="x",
            paper_refs=(),
            data_fields=("price",),
            complexity="O(1)",
        )
        assert m.latency_profile == "sim_p95_v2026-02-26"

    def test_explicit_none_latency_profile(self):
        """Explicitly setting None still works for legacy manifests."""
        m = AlphaManifest(
            alpha_id="test_alpha",
            hypothesis="Test",
            formula="x",
            paper_refs=(),
            data_fields=("price",),
            complexity="O(1)",
            latency_profile=None,
        )
        assert m.latency_profile is None

    def test_custom_latency_profile(self):
        """Custom latency profile overrides default."""
        m = AlphaManifest(
            alpha_id="test_alpha",
            hypothesis="Test",
            formula="x",
            paper_refs=(),
            data_fields=("price",),
            complexity="O(1)",
            latency_profile="custom_p99_v2026-01-01",
        )
        assert m.latency_profile == "custom_p99_v2026-01-01"

    def test_from_dict_without_latency_still_works(self):
        """Loading from dict without latency_profile falls back gracefully."""
        data = {
            "alpha_id": "old_alpha",
            "hypothesis": "Test",
            "formula": "x",
            "paper_refs": [],
            "data_fields": ["price"],
            "complexity": "O(1)",
        }
        m = AlphaManifest.from_dict(data)
        # from_dict explicitly checks for presence — None if not in dict
        assert m.latency_profile is None

    def test_from_dict_with_latency(self):
        """Loading from dict with latency_profile preserves it."""
        data = {
            "alpha_id": "new_alpha",
            "hypothesis": "Test",
            "formula": "x",
            "paper_refs": [],
            "data_fields": ["price"],
            "complexity": "O(1)",
            "latency_profile": "sim_p95_v2026-02-26",
        }
        m = AlphaManifest.from_dict(data)
        assert m.latency_profile == "sim_p95_v2026-02-26"


class TestScaffoldLatencyProfile:
    def test_scaffold_template_includes_latency(self):
        """Verify scaffold render_impl generates latency_profile."""
        from research.tools.alpha_scaffold import render_impl

        code = render_impl(
            alpha_id="test_alpha",
            paper_refs=[],
            complexity="O(1)",
            hypothesis="Test hypothesis",
            formula="signal = f(x)",
            data_fields=("price", "volume"),
        )
        assert 'latency_profile="sim_p95_v2026-02-26"' in code
