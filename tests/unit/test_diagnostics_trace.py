from __future__ import annotations

import json
import os
from unittest.mock import patch

import pytest

from hft_platform.diagnostics.replay import build_timeline, filter_traces, render_timeline_markdown, summarize_trace
from hft_platform.diagnostics.trace import DecisionTraceSampler, get_trace_sampler


# ---------------------------------------------------------------------------
# Existing integration test (kept as-is)
# ---------------------------------------------------------------------------

def test_trace_sampler_writes_and_replays(tmp_path):
    sampler = DecisionTraceSampler(enabled=True, sample_every=1, out_dir=str(tmp_path), max_bytes_per_file=1000000)
    sampler.emit(stage="gateway_reject", trace_id="t1", payload={"reason": "X"})
    sampler.emit(stage="risk_reject", trace_id="t1", payload={"reason": "Y"})
    files = list(tmp_path.glob("*.jsonl"))
    assert files
    lines = files[0].read_text(encoding="utf-8").splitlines()
    assert len(lines) == 2
    recs = [json.loads(x) for x in lines]
    filtered = filter_traces(recs, trace_id="t1")
    summary = summarize_trace(filtered)
    assert summary["count"] == 2
    assert summary["stages"]["gateway_reject"] == 1
    tl = build_timeline(filtered)
    assert tl["summary"]["count"] == 2
    assert len(tl["timeline"]) == 2
    md = render_timeline_markdown(tl)
    assert "Incident Timeline" in md
    assert "gateway_reject" in md


# ---------------------------------------------------------------------------
# DecisionTraceSampler construction
# ---------------------------------------------------------------------------

def test_sampler_disabled_emits_nothing(tmp_path):
    sampler = DecisionTraceSampler(enabled=False, sample_every=1, out_dir=str(tmp_path), max_bytes_per_file=1_000_000)
    sampler.emit(stage="test", trace_id="x", payload={"k": "v"})
    assert list(tmp_path.glob("*.jsonl")) == []


def test_sampler_post_init_creates_lock():
    sampler = DecisionTraceSampler(enabled=True, sample_every=1, out_dir="/tmp", max_bytes_per_file=1_000_000)
    assert sampler._lock is not None


def test_sampler_sampling_rate_skips_events(tmp_path):
    # sample_every=3 means only every 3rd call (counter == 0) writes a record
    sampler = DecisionTraceSampler(enabled=True, sample_every=3, out_dir=str(tmp_path), max_bytes_per_file=1_000_000)
    for i in range(9):
        sampler.emit(stage="s", trace_id="t", payload={"i": i})
    files = list(tmp_path.glob("*.jsonl"))
    assert files
    lines = files[0].read_text().splitlines()
    # 9 calls / 3 sample_every = 3 records written
    assert len(lines) == 3


def test_sampler_emit_record_has_required_fields(tmp_path):
    sampler = DecisionTraceSampler(enabled=True, sample_every=1, out_dir=str(tmp_path), max_bytes_per_file=1_000_000)
    sampler.emit(stage="risk_check", trace_id="abc123", payload={"symbol": "2330"})
    files = list(tmp_path.glob("*.jsonl"))
    record = json.loads(files[0].read_text().strip())
    assert "ts_ns" in record
    assert record["stage"] == "risk_check"
    assert record["trace_id"] == "abc123"
    assert isinstance(record["ts_ns"], int)


def test_sampler_emit_creates_output_directory(tmp_path):
    nested_dir = tmp_path / "deep" / "nested" / "dir"
    sampler = DecisionTraceSampler(enabled=True, sample_every=1, out_dir=str(nested_dir), max_bytes_per_file=1_000_000)
    sampler.emit(stage="test", trace_id="t", payload={})
    assert nested_dir.is_dir()
    assert list(nested_dir.glob("*.jsonl"))


def test_sampler_rollover_when_file_exceeds_max_bytes(tmp_path):
    # Set max_bytes very small so the second write triggers rollover
    sampler = DecisionTraceSampler(enabled=True, sample_every=1, out_dir=str(tmp_path), max_bytes_per_file=1)
    sampler.emit(stage="s1", trace_id="t1", payload={"x": "a" * 100})
    sampler.emit(stage="s2", trace_id="t2", payload={"x": "b" * 100})
    files = sorted(tmp_path.glob("*.jsonl"))
    # Should have rolled over to a new file
    assert len(files) >= 2


def test_sampler_empty_trace_id_handled(tmp_path):
    sampler = DecisionTraceSampler(enabled=True, sample_every=1, out_dir=str(tmp_path), max_bytes_per_file=1_000_000)
    sampler.emit(stage="edge", trace_id="", payload={})
    files = list(tmp_path.glob("*.jsonl"))
    record = json.loads(files[0].read_text().strip())
    assert record["trace_id"] == ""


def test_sampler_emit_suppresses_exceptions(tmp_path):
    # Even if the file write fails, emit must not raise
    sampler = DecisionTraceSampler(enabled=True, sample_every=1, out_dir="/nonexistent_root/bad/path", max_bytes_per_file=1_000_000)
    # Should not raise even though directory creation will fail on read-only root
    try:
        sampler.emit(stage="s", trace_id="t", payload={})
    except Exception as exc:  # noqa: BLE001
        pytest.fail(f"emit() raised unexpectedly: {exc}")


# ---------------------------------------------------------------------------
# from_env constructor
# ---------------------------------------------------------------------------

def test_from_env_defaults():
    env = {
        "HFT_DIAG_TRACE_ENABLED": "0",
        "HFT_DIAG_TRACE_SAMPLE_EVERY": "100",
        "HFT_DIAG_TRACE_DIR": "outputs/decision_traces",
        "HFT_DIAG_TRACE_MAX_BYTES": "25000000",
    }
    with patch.dict(os.environ, env, clear=False):
        sampler = DecisionTraceSampler.from_env()
    assert sampler.enabled is False
    assert sampler.sample_every == 100
    assert sampler.out_dir == "outputs/decision_traces"
    assert sampler.max_bytes_per_file == 25_000_000


def test_from_env_enabled_true_variants():
    for val in ("1", "true", "yes", "on", "True", "YES"):
        with patch.dict(os.environ, {"HFT_DIAG_TRACE_ENABLED": val}, clear=False):
            sampler = DecisionTraceSampler.from_env()
        assert sampler.enabled is True, f"Expected enabled=True for value {val!r}"


def test_from_env_invalid_sample_every_falls_back_to_default():
    with patch.dict(os.environ, {"HFT_DIAG_TRACE_SAMPLE_EVERY": "not_a_number"}, clear=False):
        sampler = DecisionTraceSampler.from_env()
    assert sampler.sample_every == 100


def test_from_env_invalid_max_bytes_falls_back_to_default():
    with patch.dict(os.environ, {"HFT_DIAG_TRACE_MAX_BYTES": "bad"}, clear=False):
        sampler = DecisionTraceSampler.from_env()
    assert sampler.max_bytes_per_file == 25_000_000


def test_from_env_sample_every_clamped_to_minimum_one():
    with patch.dict(os.environ, {"HFT_DIAG_TRACE_SAMPLE_EVERY": "0"}, clear=False):
        sampler = DecisionTraceSampler.from_env()
    assert sampler.sample_every == 1


def test_from_env_max_bytes_clamped_to_minimum():
    with patch.dict(os.environ, {"HFT_DIAG_TRACE_MAX_BYTES": "1"}, clear=False):
        sampler = DecisionTraceSampler.from_env()
    assert sampler.max_bytes_per_file == 1_000_000


def test_from_env_custom_dir():
    with patch.dict(os.environ, {"HFT_DIAG_TRACE_DIR": "/custom/trace/dir"}, clear=False):
        sampler = DecisionTraceSampler.from_env()
    assert sampler.out_dir == "/custom/trace/dir"


# ---------------------------------------------------------------------------
# get_trace_sampler singleton
# ---------------------------------------------------------------------------

def test_get_trace_sampler_returns_sampler_instance():
    import hft_platform.diagnostics.trace as trace_module
    # Reset module-level singleton for isolation
    original = trace_module._SAMPLER
    trace_module._SAMPLER = None
    try:
        sampler = get_trace_sampler()
        assert isinstance(sampler, DecisionTraceSampler)
    finally:
        trace_module._SAMPLER = original


def test_get_trace_sampler_returns_same_instance():
    import hft_platform.diagnostics.trace as trace_module
    original = trace_module._SAMPLER
    trace_module._SAMPLER = None
    try:
        s1 = get_trace_sampler()
        s2 = get_trace_sampler()
        assert s1 is s2
    finally:
        trace_module._SAMPLER = original


# ---------------------------------------------------------------------------
# _rollover_path edge cases
# ---------------------------------------------------------------------------

def test_rollover_path_returns_first_nonexistent_candidate(tmp_path):
    sampler = DecisionTraceSampler(enabled=True, sample_every=1, out_dir=str(tmp_path), max_bytes_per_file=1_000_000)
    from pathlib import Path
    base = tmp_path / "20260405.jsonl"
    base.write_text("x")
    result = sampler._rollover_path(base)
    assert result == tmp_path / "20260405.001.jsonl"


def test_rollover_path_returns_overflow_when_all_candidates_full(tmp_path):
    sampler = DecisionTraceSampler(enabled=True, sample_every=1, out_dir=str(tmp_path), max_bytes_per_file=1)
    from pathlib import Path
    base = tmp_path / "20260405.jsonl"
    # Create all 999 candidates with content > max_bytes_per_file
    for i in range(1, 1000):
        cand = tmp_path / f"20260405.{i:03d}.jsonl"
        cand.write_text("xx")  # 2 bytes > max_bytes_per_file=1
    result = sampler._rollover_path(base)
    assert result == tmp_path / "20260405.overflow.jsonl"
