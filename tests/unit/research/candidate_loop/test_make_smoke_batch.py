"""Template expander: 6x20 valid unique candidates, headered §11 family files."""

from __future__ import annotations

import json
from pathlib import Path

from research.candidate_loop.generate import HEADER_KEY, read_family_jsonl
from research.candidate_loop.schema import FAMILIES
from research.candidate_loop.tools.make_smoke_batch import (
    GENERATION_MODEL,
    PER_FAMILY,
    build_candidates,
    write_smoke_batch,
)
from research.candidate_loop.validator import ValidCandidate, validate_batch

PROMPTS_DIR = Path(__file__).resolve().parents[4] / "research" / "candidate_loop" / "prompts" / "v1"
FIXTURE = (
    Path(__file__).resolve().parents[4]
    / "research"
    / "candidate_loop"
    / "fixtures"
    / "validator_matrix_12.jsonl"
)


class TestGrids:
    def test_twenty_candidates_per_family_all_families(self) -> None:
        families = build_candidates()
        assert set(families) == set(FAMILIES)
        for cands in families.values():
            assert len(cands) == PER_FAMILY

    def test_all_120_validate_with_unique_formula_hashes(self) -> None:
        families = build_candidates()
        lines = [
            json.dumps(c, sort_keys=True, separators=(",", ":"))
            for family in sorted(families)
            for c in families[family]
        ]
        results = validate_batch(lines)
        assert all(isinstance(r, ValidCandidate) for r in results)
        hashes = {r.formula_hash for r in results if isinstance(r, ValidCandidate)}
        assert len(hashes) == 120

    def test_smoke_hashes_disjoint_from_fixture_hashes(self) -> None:
        """Running the fixture batch into CH first must not kill smoke candidates."""
        fixture_lines = [ln for ln in FIXTURE.read_text().splitlines() if ln.strip()]
        fixture_hashes = {
            r.formula_hash for r in validate_batch(fixture_lines) if isinstance(r, ValidCandidate)
        }
        families = build_candidates()
        smoke_lines = [
            json.dumps(c, sort_keys=True, separators=(",", ":"))
            for family in sorted(families)
            for c in families[family]
        ]
        smoke_hashes = {
            r.formula_hash for r in validate_batch(smoke_lines) if isinstance(r, ValidCandidate)
        }
        assert not (fixture_hashes & smoke_hashes)


class TestWrite:
    def test_writes_six_headered_family_files(self, tmp_path: Path) -> None:
        paths = write_smoke_batch(
            "smoke_test",
            candidates_root=tmp_path,
            prompts_dir=PROMPTS_DIR,
            generated_at="2026-06-12T00:00:00+00:00",
        )
        assert len(paths) == 6
        total = 0
        for path in paths:
            assert path.parent == tmp_path / "smoke_test"
            header, lines = read_family_jsonl(path)
            assert header is not None
            assert header[HEADER_KEY] is True
            assert header["generation_model"] == GENERATION_MODEL
            assert header["generation_run_id"] == "smoke_test"
            assert header["prompt_id"].endswith("__v1")
            assert len(header["prompt_sha256"]) == 64
            total += len(lines)
        assert total == 120
