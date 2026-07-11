"""Governor orchestration: deterministic `draft`, gated `generate`.

`draft_briefs` is pure-deterministic (summary → briefs). `generate_from_briefs`
enforces the per-family approval gate, freezes the non-deterministic LLM drop as
an artifact (so re-runs reuse it instead of re-calling DeepSeek), hands it to the
existing `generate_drop`, and records a `governor_manifest.json` provenance
sidecar. Nothing here touches the frozen scored path.

Robustness for the paid-spend gate: the raw drop and the manifest are written
write-then-rename (atomic), so a crash mid-write can never leave a truncated
artifact a later run would "reuse" as complete; and the manifest is persisted in
a `finally` block, so spend already incurred for earlier families is always
recorded even if a later family fails.
"""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
from pathlib import Path
from typing import Any, Protocol

from research.candidate_loop.generate import generate_drop
from research.candidate_loop.governor.brief import parse_brief, render_brief
from research.candidate_loop.governor.signals import (
    GovernorConfig,
    classify_focus,
    extract_signals,
    n_target_for,
)


class _Client(Protocol):
    def generate_candidates(self, *, base_prompt: str, brief_body: str, n: int) -> list[str]: ...


def _atomic_write(path: Path, content: str) -> None:
    """Write ``content`` to ``path`` via a temp file + rename (atomic).

    Guards the frozen raw drop and the manifest against a crash mid-write that
    would otherwise leave a truncated file a later run treats as complete.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(content)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def draft_briefs(
    *,
    summary_path: Path,
    out_dir: Path,
    cfg: GovernorConfig,
    generated_at: str,
) -> list[Path]:
    summary = json.loads(summary_path.read_text())
    source_run_id = str(summary.get("run_id", ""))
    out_dir.mkdir(parents=True, exist_ok=True)
    written: list[Path] = []
    for family in sorted(summary.get("per_family", {})):
        signals = extract_signals(summary, family)
        focus = classify_focus(signals, cfg)
        text = render_brief(
            signals,
            focus=focus,
            n_target=n_target_for(focus, cfg),
            source_run_id=source_run_id,
            generated_at=generated_at,
        )
        path = out_dir / f"{family}.md"
        path.write_text(text, encoding="utf-8")
        written.append(path)
    return written


def generate_from_briefs(
    *,
    steering_dir: Path,
    gen_run_id: str,
    cfg: GovernorConfig,
    client: _Client,
    prompts_dir: Path,
    candidates_root: Path,
    generated_at: str,
) -> dict[str, Any]:
    manifest: dict[str, Any] = {
        "gen_run_id": gen_run_id,
        "governor_version": cfg.governor_version,
        "model": cfg.model_name,
        "generated_at": generated_at,
        "families": {},
        "skipped_unapproved": [],
    }
    raw_root = candidates_root / gen_run_id / "_governor_raw"
    manifest_path = candidates_root / gen_run_id / "governor_manifest.json"

    def _persist() -> None:
        manifest["skipped_unapproved"] = sorted(manifest["skipped_unapproved"])
        _atomic_write(manifest_path, json.dumps(manifest, indent=2, sort_keys=True))

    try:
        for brief_path in sorted(steering_dir.glob("*.md")):
            text = brief_path.read_text(encoding="utf-8")
            brief = parse_brief(text)
            # A human approves a specific file; its frontmatter family must match
            # the filename, or the approval intent and the generated family could
            # silently diverge. Fail closed on mismatch.
            if brief.family != brief_path.stem:
                raise ValueError(
                    f"{brief_path}: brief family {brief.family!r} does not match "
                    f"filename stem {brief_path.stem!r}"
                )
            if not brief.approved:
                manifest["skipped_unapproved"].append(brief.family)
                continue
            raw_path = raw_root / f"{brief.family}.jsonl"
            reused = raw_path.exists()
            if not reused:
                base_prompt = (prompts_dir / f"{brief.family}.md").read_text(encoding="utf-8")
                lines = client.generate_candidates(
                    base_prompt=base_prompt, brief_body=brief.body, n=brief.n_target
                )
                _atomic_write(raw_path, "\n".join(lines) + "\n")
            family_file = generate_drop(
                gen_run_id=gen_run_id,
                family=brief.family,
                prompt_path=prompts_dir / f"{brief.family}.md",
                from_jsonl=raw_path,
                generation_model=cfg.model_name,
                generated_at=generated_at,
                candidates_root=candidates_root,
            )
            manifest["families"][brief.family] = {
                "source_run_id": brief.source_run_id,
                "steering_path": str(brief_path),
                "steering_sha256": hashlib.sha256(text.encode("utf-8")).hexdigest(),
                "focus": brief.focus,
                "n_target": brief.n_target,
                "model": cfg.model_name,
                "raw_drop": str(raw_path),
                "family_file": str(family_file),
                "reused": reused,
                "generated_at": generated_at,
            }
    finally:
        _persist()
    return manifest


__all__ = ["draft_briefs", "generate_from_briefs"]
