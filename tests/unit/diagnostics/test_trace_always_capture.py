"""Tests for DecisionTraceSampler.emit_always (L5 — order-bearing 100% capture).

The standard ``emit`` path applies the ``sample_every`` modulo so only every
Nth record is written. Order-bearing events (every ``OrderIntent`` produced by
a strategy, every ``RiskDecision``, every dispatch outcome) MUST NOT be
sampled — every one needs a complete trace chain so an explanation can be
reconstructed.

``emit_always`` keeps the ``enabled`` gate and best-effort error swallowing
but skips the sampling counter entirely.
"""

from __future__ import annotations

import json

from hft_platform.diagnostics.trace import DecisionTraceSampler


class TestEmitAlways:
    """emit_always must bypass sample_every but still respect enabled."""

    def test_emit_always_writes_every_record_when_sample_every_is_high(self, tmp_path):
        """sample_every=1000 would hide 999/1000 of normal emits — emit_always
        must still produce every record."""
        sampler = DecisionTraceSampler(
            enabled=True,
            sample_every=1000,
            out_dir=str(tmp_path),
            max_bytes_per_file=1_000_000,
        )

        for i in range(5):
            sampler.emit_always(stage="order_enqueue_api", trace_id=f"t{i}", payload={"i": i})

        files = list(tmp_path.glob("*.jsonl"))
        assert len(files) == 1
        lines = files[0].read_text(encoding="utf-8").splitlines()
        assert len(lines) == 5

        recs = [json.loads(line) for line in lines]
        assert [r["payload"]["i"] for r in recs] == [0, 1, 2, 3, 4]
        assert all(r["stage"] == "order_enqueue_api" for r in recs)

    def test_emit_always_respects_disabled_flag(self, tmp_path):
        """Disabled sampler must drop emit_always too — diagnostics must not
        leak when the global toggle is off."""
        sampler = DecisionTraceSampler(
            enabled=False,
            sample_every=1,
            out_dir=str(tmp_path),
            max_bytes_per_file=1_000_000,
        )

        sampler.emit_always(stage="order_dispatch_ok", trace_id="t1", payload={"cmd_id": 42})

        assert list(tmp_path.glob("*.jsonl")) == []

    def test_emit_always_does_not_advance_sample_counter(self, tmp_path):
        """emit_always must not touch ``_counter`` so a parallel emit() stream
        keeps its own modulo-N rhythm undisturbed."""
        sampler = DecisionTraceSampler(
            enabled=True,
            sample_every=3,
            out_dir=str(tmp_path),
            max_bytes_per_file=1_000_000,
        )

        sampler.emit_always(stage="order_enqueue_api", trace_id="a", payload={})
        sampler.emit_always(stage="order_enqueue_api", trace_id="b", payload={})
        sampler.emit_always(stage="order_enqueue_api", trace_id="c", payload={})

        for i in range(3):
            sampler.emit(stage="market_event", trace_id=f"m{i}", payload={"i": i})

        lines = sorted(tmp_path.glob("*.jsonl"))[0].read_text(encoding="utf-8").splitlines()
        assert len(lines) == 4
        recs = [json.loads(line) for line in lines]
        always_emits = [r for r in recs if r["stage"] == "order_enqueue_api"]
        sampled_emits = [r for r in recs if r["stage"] == "market_event"]
        assert len(always_emits) == 3
        assert len(sampled_emits) == 1
        assert sampled_emits[0]["payload"]["i"] == 2

    def test_emit_always_swallows_io_errors(self, tmp_path, monkeypatch):
        """emit_always must never raise into the hot path — diagnostics is
        best-effort, like emit()."""
        sampler = DecisionTraceSampler(
            enabled=True,
            sample_every=1,
            out_dir=str(tmp_path),
            max_bytes_per_file=1_000_000,
        )

        def _boom(*args, **kwargs):
            raise OSError("disk full")

        monkeypatch.setattr("pathlib.Path.open", _boom)

        sampler.emit_always(stage="order_dispatch_error", trace_id="t1", payload={"err": "x"})

    def test_emit_always_preserves_trace_id_and_stage(self, tmp_path):
        """Output schema must match emit() so downstream consumers (replay,
        timeline) keep working."""
        sampler = DecisionTraceSampler(
            enabled=True,
            sample_every=100,
            out_dir=str(tmp_path),
            max_bytes_per_file=1_000_000,
        )

        sampler.emit_always(stage="risk_check", trace_id="abc-123", payload={"approved": True})

        rec = json.loads(list(tmp_path.glob("*.jsonl"))[0].read_text(encoding="utf-8").strip())
        assert rec["stage"] == "risk_check"
        assert rec["trace_id"] == "abc-123"
        assert rec["payload"] == {"approved": True}
        assert "ts_ns" in rec
