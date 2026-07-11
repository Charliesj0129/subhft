"""§11 generator contract: frontmatter, provenance header, drop shape, round-trip."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from research.candidate_loop.generate import (
    HEADER_KEY,
    DropShapeError,
    build_header,
    family_from_filename,
    family_jsonl_path,
    generate_drop,
    ingest_jsonl,
    prompt_sha256,
    read_family_jsonl,
    read_prompt_frontmatter,
    write_family_jsonl,
)

PROMPT = """---
prompt_id: microprice__v1
schema_ref: research/candidate_loop/prompts/v1/candidate.schema.json
primitive_version: prim_v1
---

# body
"""


@pytest.fixture()
def prompt_path(tmp_path: Path) -> Path:
    path = tmp_path / "microprice.md"
    path.write_text(PROMPT, encoding="utf-8")
    return path


class TestPromptFrontmatter:
    def test_parses_prompt_id_and_versions(self, prompt_path: Path) -> None:
        fm = read_prompt_frontmatter(prompt_path)
        assert fm["prompt_id"] == "microprice__v1"
        assert fm["primitive_version"] == "prim_v1"

    def test_missing_frontmatter_rejected(self, tmp_path: Path) -> None:
        path = tmp_path / "bare.md"
        path.write_text("# no frontmatter\n")
        with pytest.raises(DropShapeError, match="frontmatter"):
            read_prompt_frontmatter(path)

    def test_missing_prompt_id_rejected(self, tmp_path: Path) -> None:
        path = tmp_path / "noid.md"
        path.write_text("---\nschema_ref: x\n---\nbody\n")
        with pytest.raises(DropShapeError, match="prompt_id"):
            read_prompt_frontmatter(path)

    def test_prompt_sha256_tracks_exact_bytes(self, prompt_path: Path) -> None:
        before = prompt_sha256(prompt_path)
        prompt_path.write_text(PROMPT + "\nedited", encoding="utf-8")
        assert prompt_sha256(prompt_path) != before


class TestHeader:
    def test_header_carries_full_provenance(self, prompt_path: Path) -> None:
        header = build_header(prompt_path, "template_v1", "smoke_001", "2026-06-12T00:00:00+00:00")
        assert header[HEADER_KEY] is True
        assert header["prompt_id"] == "microprice__v1"
        assert header["prompt_sha256"] == prompt_sha256(prompt_path)
        assert header["generation_model"] == "template_v1"
        assert header["generation_run_id"] == "smoke_001"


class TestIngest:
    def test_rejects_non_json_line(self, tmp_path: Path) -> None:
        src = tmp_path / "drop.jsonl"
        src.write_text('{"a": 1}\nnot json\n')
        with pytest.raises(DropShapeError, match="not valid JSON"):
            ingest_jsonl(src)

    def test_rejects_non_object_line(self, tmp_path: Path) -> None:
        src = tmp_path / "drop.jsonl"
        src.write_text("[1, 2]\n")
        with pytest.raises(DropShapeError, match="not a JSON object"):
            ingest_jsonl(src)

    def test_count_mismatch_rejected(self, tmp_path: Path) -> None:
        src = tmp_path / "drop.jsonl"
        src.write_text('{"a": 1}\n{"b": 2}\n')
        with pytest.raises(DropShapeError, match="expected 3"):
            ingest_jsonl(src, expected_count=3)

    def test_skips_blank_and_preexisting_header_lines(self, tmp_path: Path) -> None:
        src = tmp_path / "drop.jsonl"
        src.write_text('{"_header": true, "prompt_id": "x"}\n\n{"a": 1}\n')
        assert ingest_jsonl(src, expected_count=1) == ['{"a":1}']


class TestFamilyFiles:
    def test_round_trip_header_and_lines(self, tmp_path: Path, prompt_path: Path) -> None:
        header = build_header(prompt_path, "m", "r", "t")
        out = write_family_jsonl(tmp_path / "family=microprice.jsonl", header, ['{"a":1}', '{"b":2}'])
        got_header, lines = read_family_jsonl(out)
        assert got_header is not None
        assert got_header["prompt_id"] == "microprice__v1"
        assert lines == ['{"a":1}', '{"b":2}']

    def test_undecodable_lines_pass_through_for_validator(self, tmp_path: Path) -> None:
        path = tmp_path / "family=microprice.jsonl"
        path.write_text('{"_header": true}\n{broken\n')
        header, lines = read_family_jsonl(path)
        assert header == {"_header": True}
        assert lines == ["{broken"]

    def test_family_from_filename(self) -> None:
        assert family_from_filename(Path("family=trade_flow.jsonl")) == "trade_flow"
        with pytest.raises(DropShapeError):
            family_from_filename(Path("trade_flow.jsonl"))

    def test_family_jsonl_path_layout(self, tmp_path: Path) -> None:
        assert family_jsonl_path(tmp_path, "g1", "microprice") == (
            tmp_path / "g1" / "family=microprice.jsonl"
        )


class TestGenerateDrop:
    def test_end_to_end(self, tmp_path: Path, prompt_path: Path) -> None:
        src = tmp_path / "drop.jsonl"
        src.write_text('{"name": "x"}\n{"name": "y"}\n')
        out = generate_drop(
            gen_run_id="g1",
            family="microprice",
            prompt_path=prompt_path,
            from_jsonl=src,
            expected_count=2,
            generation_model="template_v1",
            generated_at="2026-06-12T00:00:00+00:00",
            candidates_root=tmp_path / "candidates",
        )
        assert out == tmp_path / "candidates" / "g1" / "family=microprice.jsonl"
        first = json.loads(out.read_text().splitlines()[0])
        assert first[HEADER_KEY] is True

    def test_unknown_family_rejected(self, tmp_path: Path, prompt_path: Path) -> None:
        src = tmp_path / "drop.jsonl"
        src.write_text('{"a": 1}\n')
        with pytest.raises(DropShapeError, match="unknown family"):
            generate_drop(
                gen_run_id="g1",
                family="momentum",
                prompt_path=prompt_path,
                from_jsonl=src,
                generation_model="m",
                generated_at="t",
                candidates_root=tmp_path,
            )
