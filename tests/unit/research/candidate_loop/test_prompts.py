"""Committed prompts are fresh, §11-conformant, and their examples validate."""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from research.candidate_loop.generate import read_prompt_frontmatter
from research.candidate_loop.schema import FAMILIES, PRIMITIVE_SIGNATURES, TRANSFORM_SIGNATURES
from research.candidate_loop.tools.render_prompts import (
    FAMILY_EXAMPLES,
    SCHEMA_FILENAME,
    render_all,
    render_prompt,
    render_schema_json,
)
from research.candidate_loop.validator import ValidCandidate, validate_batch

PROMPTS_DIR = Path(__file__).resolve().parents[4] / "research" / "candidate_loop" / "prompts" / "v1"
ALL_FAMILIES = sorted(FAMILIES)


class TestCoverageAndFreshness:
    def test_every_family_has_a_prompt_file(self) -> None:
        for family in ALL_FAMILIES:
            assert (PROMPTS_DIR / f"{family}.md").exists(), family

    @pytest.mark.parametrize("family", ALL_FAMILIES)
    def test_committed_prompt_matches_fresh_render(self, family: str) -> None:
        committed = (PROMPTS_DIR / f"{family}.md").read_text(encoding="utf-8")
        assert committed == render_prompt(family), (
            f"{family}.md is stale; rerun python -m research.candidate_loop.tools.render_prompts"
        )

    def test_committed_schema_json_matches_fresh_render(self) -> None:
        committed = (PROMPTS_DIR / SCHEMA_FILENAME).read_text(encoding="utf-8")
        assert committed == render_schema_json(), (
            f"{SCHEMA_FILENAME} is stale; rerun python -m research.candidate_loop.tools.render_prompts"
        )


class TestRenderAll:
    def test_render_all_writes_six_prompts_plus_schema(self, tmp_path: Path) -> None:
        written = render_all(tmp_path)
        assert len(written) == 7
        names = {p.name for p in written}
        assert names == {f"{f}.md" for f in ALL_FAMILIES} | {SCHEMA_FILENAME}
        for path in written:
            assert path.read_text(encoding="utf-8")


class TestFrontmatter:
    @pytest.mark.parametrize("family", ALL_FAMILIES)
    def test_frontmatter_contract(self, family: str) -> None:
        fm = read_prompt_frontmatter(PROMPTS_DIR / f"{family}.md")
        assert fm["prompt_id"] == f"{family}__v1"
        assert fm["primitive_version"] == "prim_v1"
        schema_ref = Path(fm["schema_ref"])
        assert schema_ref.name == SCHEMA_FILENAME
        assert (PROMPTS_DIR / schema_ref.name).exists()


class TestBody:
    @pytest.mark.parametrize("family", ALL_FAMILIES)
    def test_embeds_every_primitive_and_transform_signature(self, family: str) -> None:
        body = (PROMPTS_DIR / f"{family}.md").read_text(encoding="utf-8")
        for name in PRIMITIVE_SIGNATURES:
            assert re.search(rf"\b{name}\(", body), f"{family}.md missing primitive {name}"
        for name in TRANSFORM_SIGNATURES:
            assert re.search(rf"\b{name}\(", body), f"{family}.md missing transform {name}"
        assert "LABEL ONLY" in body

    @pytest.mark.parametrize("family", ALL_FAMILIES)
    def test_names_its_own_family(self, family: str) -> None:
        body = (PROMPTS_DIR / f"{family}.md").read_text(encoding="utf-8")
        assert f'`family` MUST be `"{family}"`' in body


class TestExamples:
    def test_every_example_validates_under_the_frozen_validator(self) -> None:
        lines = [
            json.dumps(FAMILY_EXAMPLES[family], sort_keys=True, separators=(",", ":"))
            for family in ALL_FAMILIES
        ]
        results = validate_batch(lines)
        for family, result in zip(ALL_FAMILIES, results):
            assert isinstance(result, ValidCandidate), (
                f"{family} example invalid: {getattr(result, 'detail', '')}"
            )
            assert result.candidate.family == family
