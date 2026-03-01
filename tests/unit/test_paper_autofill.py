from __future__ import annotations

import json
from pathlib import Path

from research.tools import paper_autofill


def test_infer_spec_from_ofi_text_includes_ofi_features() -> None:
    spec = paper_autofill.infer_spec_from_text(
        "Forecasting High Frequency Order Flow Imbalance",
        "The model estimates OFI and predicts near-term distribution.",
    )
    assert "ofi_l1_ema8" in spec.data_fields
    assert "depth_imbalance_ema8_ppm" in spec.data_fields
    assert "order-flow imbalance" in spec.hypothesis.lower()
    assert "alpha_t" in spec.formula


def test_suggest_alpha_id_strips_stop_words_and_symbols() -> None:
    alpha_id = paper_autofill.suggest_alpha_id("A Study of Queue Imbalance in LOB!!")
    assert alpha_id == "study_queue_imbalance_lob"


def test_infer_spec_from_paper_refs_uses_note_sections(tmp_path: Path) -> None:
    root = tmp_path
    note_dir = root / "research" / "knowledge" / "notes"
    note_dir.mkdir(parents=True, exist_ok=True)
    note_path = note_dir / "120_example.md"
    note_path.write_text(
        "\n".join(
            [
                "# Example Paper",
                "",
                "ref: 120",
                "arxiv: https://arxiv.org/abs/2408.03594v1",
                "Authors: A, B",
                "Published: 2024-08-07",
                "",
                "## Hypothesis",
                "- Custom hypothesis from note.",
                "",
                "## Candidate Formula",
                "- `alpha_t = zscore(ofi_l1_ema8_t)`",
                "",
                "## Relevant Features (lob_shared_v1)",
                "- `ofi_l1_ema8`",
                "- `spread_scaled`",
            ]
        ),
        encoding="utf-8",
    )
    index = root / "research" / "knowledge" / "paper_index.json"
    index.parent.mkdir(parents=True, exist_ok=True)
    index.write_text(
        json.dumps(
            {
                "120": {
                    "ref": "120",
                    "arxiv_id": "2408.03594v1",
                    "title": "Forecasting High Frequency Order Flow Imbalance",
                    "note_file": "research/knowledge/notes/120_example.md",
                    "tags": ["microstructure"],
                }
            }
        ),
        encoding="utf-8",
    )
    spec = paper_autofill.infer_spec_from_paper_refs(
        ["120"],
        project_root=root,
        index_path=index,
    )
    assert spec.hypothesis.startswith("Custom hypothesis from note")
    assert spec.formula == "alpha_t = zscore(ofi_l1_ema8_t)"
    assert "ofi_l1_ema8" in spec.data_fields
