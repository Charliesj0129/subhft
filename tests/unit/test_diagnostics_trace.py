from __future__ import annotations

from hft_platform.diagnostics.replay import build_timeline, filter_traces, render_timeline_markdown, summarize_trace
from hft_platform.diagnostics.trace import DecisionTraceSampler


def test_trace_sampler_writes_and_replays(tmp_path):
    sampler = DecisionTraceSampler(enabled=True, sample_every=1, out_dir=str(tmp_path), max_bytes_per_file=1000000)
    sampler.emit(stage='gateway_reject', trace_id='t1', payload={'reason': 'X'})
    sampler.emit(stage='risk_reject', trace_id='t1', payload={'reason': 'Y'})
    files = list(tmp_path.glob('*.jsonl'))
    assert files
    lines = files[0].read_text(encoding='utf-8').splitlines()
    assert len(lines) == 2
    import json
    recs = [json.loads(x) for x in lines]
    filtered = filter_traces(recs, trace_id='t1')
    summary = summarize_trace(filtered)
    assert summary['count'] == 2
    assert summary['stages']['gateway_reject'] == 1
    tl = build_timeline(filtered)
    assert tl["summary"]["count"] == 2
    assert len(tl["timeline"]) == 2
    md = render_timeline_markdown(tl)
    assert "Incident Timeline" in md
    assert "gateway_reject" in md
