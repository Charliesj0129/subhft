"""Governor runner: draft briefs, enforce approval gate, freeze raw drop, manifest."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from research.candidate_loop.governor.runner import draft_briefs, generate_from_briefs
from research.candidate_loop.governor.signals import load_governor_config

REPO = Path(__file__).resolve().parents[4]
CFG = load_governor_config(REPO / "config" / "research" / "candidate_loop" / "governor_v1.yaml")
PROMPTS = REPO / "research" / "candidate_loop" / "prompts" / "v1"
FIXED_TS = "2026-06-14T00:00:00+00:00"


def _summary() -> dict:
    return {
        "run_id": "smoke_001",
        "per_family": {
            "trade_flow": {
                "candidates": 20,
                "survival_rate": 0.10,
                "ic_distribution_survivors": {"p10": 0.0, "p50": 0.114, "p90": 0.2},
                "cost_failure_rate": 0.55,
                "maker_cost_failure_rate": 0.40,
                "maker_rescuable_count": 2,
                "duplicate_rate": 0.05,
                "reduced_day_coverage_count": 7,
                "near_misses": [],
                "common_failure_patterns": [],
            }
        },
    }


class _FakeClient:
    """Stand-in DeepSeekClient: records calls, returns canned JSONL."""

    def __init__(self) -> None:
        self.calls = 0

    def generate_candidates(self, *, base_prompt: str, brief_body: str, n: int) -> list[str]:
        self.calls += 1
        return [
            json.dumps({"family": "trade_flow", "name": f"tf_{i}", "formula": "x"}, sort_keys=True)
            for i in range(n)
        ]


def test_draft_briefs_writes_unapproved_per_family(tmp_path):
    summary_path = tmp_path / "failure_summary.json"
    summary_path.write_text(json.dumps(_summary()))
    out_dir = tmp_path / "steering"
    paths = draft_briefs(summary_path=summary_path, out_dir=out_dir, cfg=CFG, generated_at=FIXED_TS)
    assert [p.name for p in paths] == ["trade_flow.md"]
    assert "approved: false" in (out_dir / "trade_flow.md").read_text()


def test_generate_refuses_unapproved_brief(tmp_path):
    steering = tmp_path / "steering"
    steering.mkdir()
    summary_path = tmp_path / "fs.json"
    summary_path.write_text(json.dumps(_summary()))
    draft_briefs(summary_path=summary_path, out_dir=steering, cfg=CFG, generated_at=FIXED_TS)
    client = _FakeClient()
    candidates_root = tmp_path / "candidates"
    manifest = generate_from_briefs(
        steering_dir=steering,
        gen_run_id="gen_001",
        cfg=CFG,
        client=client,
        prompts_dir=PROMPTS,
        candidates_root=candidates_root,
        generated_at=FIXED_TS,
    )
    assert client.calls == 0
    assert manifest["skipped_unapproved"] == ["trade_flow"]
    assert manifest["families"] == {}


def test_generate_from_approved_brief_freezes_drop_and_writes_manifest(tmp_path):
    steering = tmp_path / "steering"
    steering.mkdir()
    summary_path = tmp_path / "fs.json"
    summary_path.write_text(json.dumps(_summary()))
    draft_briefs(summary_path=summary_path, out_dir=steering, cfg=CFG, generated_at=FIXED_TS)
    brief_path = steering / "trade_flow.md"
    brief_path.write_text(brief_path.read_text().replace("approved: false", "approved: true"))

    client = _FakeClient()
    candidates_root = tmp_path / "candidates"
    manifest = generate_from_briefs(
        steering_dir=steering,
        gen_run_id="gen_001",
        cfg=CFG,
        client=client,
        prompts_dir=PROMPTS,
        candidates_root=candidates_root,
        generated_at=FIXED_TS,
    )
    assert client.calls == 1
    fam = manifest["families"]["trade_flow"]
    assert fam["focus"] == "amplify"
    assert fam["model"] == "deepseek-chat"
    assert len(fam["steering_sha256"]) == 64
    # raw drop frozen + family file written through the existing generate path
    assert (candidates_root / "gen_001" / "_governor_raw" / "trade_flow.jsonl").exists()
    family_file = candidates_root / "gen_001" / "family=trade_flow.jsonl"
    assert family_file.exists()
    header = json.loads(family_file.read_text().splitlines()[0])
    assert header["generation_model"] == "deepseek-chat"
    assert (candidates_root / "gen_001" / "governor_manifest.json").exists()


def test_generate_is_idempotent_reuses_frozen_drop(tmp_path):
    steering = tmp_path / "steering"
    steering.mkdir()
    summary_path = tmp_path / "fs.json"
    summary_path.write_text(json.dumps(_summary()))
    draft_briefs(summary_path=summary_path, out_dir=steering, cfg=CFG, generated_at=FIXED_TS)
    brief_path = steering / "trade_flow.md"
    brief_path.write_text(brief_path.read_text().replace("approved: false", "approved: true"))

    candidates_root = tmp_path / "candidates"
    kwargs = dict(
        steering_dir=steering,
        gen_run_id="gen_001",
        cfg=CFG,
        prompts_dir=PROMPTS,
        candidates_root=candidates_root,
        generated_at=FIXED_TS,
    )
    first = _FakeClient()
    generate_from_briefs(client=first, **kwargs)
    second = _FakeClient()
    manifest = generate_from_briefs(client=second, **kwargs)
    assert first.calls == 1
    assert second.calls == 0  # reused frozen drop
    assert manifest["families"]["trade_flow"]["reused"] is True


def _summary_two() -> dict:
    fam = {
        "candidates": 20,
        "survival_rate": 0.10,
        "ic_distribution_survivors": {"p10": 0.0, "p50": 0.114, "p90": 0.2},
        "cost_failure_rate": 0.55,
        "maker_cost_failure_rate": 0.40,
        "maker_rescuable_count": 2,
        "duplicate_rate": 0.05,
        "reduced_day_coverage_count": 7,
        "near_misses": [],
        "common_failure_patterns": [],
    }
    return {"run_id": "smoke_001", "per_family": {"microprice": dict(fam), "trade_flow": dict(fam)}}


class _FailingClient:
    """Succeeds on the first family, then raises (simulated mid-run API failure)."""

    def __init__(self) -> None:
        self.calls = 0

    def generate_candidates(self, *, base_prompt: str, brief_body: str, n: int) -> list[str]:
        self.calls += 1
        if self.calls >= 2:
            raise RuntimeError("simulated DeepSeek failure")
        return [json.dumps({"name": f"c_{i}", "formula": "x"}, sort_keys=True) for i in range(n)]


def test_generate_mixed_approved_and_unapproved(tmp_path):
    steering = tmp_path / "steering"
    steering.mkdir()
    summary_path = tmp_path / "fs.json"
    summary_path.write_text(json.dumps(_summary_two()))
    draft_briefs(summary_path=summary_path, out_dir=steering, cfg=CFG, generated_at=FIXED_TS)
    # approve trade_flow only; leave microprice unapproved
    tf = steering / "trade_flow.md"
    tf.write_text(tf.read_text().replace("approved: false", "approved: true"))
    client = _FakeClient()
    candidates_root = tmp_path / "candidates"
    manifest = generate_from_briefs(
        steering_dir=steering,
        gen_run_id="gen_001",
        cfg=CFG,
        client=client,
        prompts_dir=PROMPTS,
        candidates_root=candidates_root,
        generated_at=FIXED_TS,
    )
    assert client.calls == 1
    assert list(manifest["families"]) == ["trade_flow"]
    assert manifest["skipped_unapproved"] == ["microprice"]


def test_generate_rejects_family_filename_mismatch(tmp_path):
    steering = tmp_path / "steering"
    steering.mkdir()
    summary_path = tmp_path / "fs.json"
    summary_path.write_text(json.dumps(_summary()))
    draft_briefs(summary_path=summary_path, out_dir=steering, cfg=CFG, generated_at=FIXED_TS)
    src = steering / "trade_flow.md"
    src.write_text(src.read_text().replace("approved: false", "approved: true"))
    # rename so the filename stem (microprice) no longer matches the frontmatter
    # family (trade_flow) — an approval-intent divergence that must fail closed.
    src.rename(steering / "microprice.md")
    client = _FakeClient()
    with pytest.raises(ValueError):
        generate_from_briefs(
            steering_dir=steering,
            gen_run_id="gen_001",
            cfg=CFG,
            client=client,
            prompts_dir=PROMPTS,
            candidates_root=tmp_path / "candidates",
            generated_at=FIXED_TS,
        )
    assert client.calls == 0  # rejected before any spend


def test_manifest_persisted_when_later_family_fails(tmp_path):
    steering = tmp_path / "steering"
    steering.mkdir()
    summary_path = tmp_path / "fs.json"
    summary_path.write_text(json.dumps(_summary_two()))
    draft_briefs(summary_path=summary_path, out_dir=steering, cfg=CFG, generated_at=FIXED_TS)
    for name in ("microprice.md", "trade_flow.md"):
        p = steering / name
        p.write_text(p.read_text().replace("approved: false", "approved: true"))
    client = _FailingClient()
    candidates_root = tmp_path / "candidates"
    with pytest.raises(RuntimeError):
        generate_from_briefs(
            steering_dir=steering,
            gen_run_id="gen_001",
            cfg=CFG,
            client=client,
            prompts_dir=PROMPTS,
            candidates_root=candidates_root,
            generated_at=FIXED_TS,
        )
    # microprice (sorted first) succeeded and is recorded despite trade_flow failing
    manifest = json.loads((candidates_root / "gen_001" / "governor_manifest.json").read_text())
    assert "microprice" in manifest["families"]
    assert "trade_flow" not in manifest["families"]
    assert (candidates_root / "gen_001" / "_governor_raw" / "microprice.jsonl").exists()
    assert not (candidates_root / "gen_001" / "_governor_raw" / "trade_flow.jsonl").exists()
