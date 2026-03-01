from __future__ import annotations

import argparse
import json
from pathlib import Path

import numpy as np

import research.tools.data_governance as data_governance
import research.tools.paper_prototype as paper_prototype


def test_paper_to_prototype_updates_index(monkeypatch, tmp_path: Path) -> None:
    paper_index = tmp_path / "research" / "knowledge" / "paper_index.json"
    paper_index.parent.mkdir(parents=True, exist_ok=True)
    paper_index.write_text(
        json.dumps(
            {
                "120": {
                    "ref": "120",
                    "arxiv_id": "2408.03594",
                    "title": "Order Flow Imbalance Study",
                    "alphas": [],
                }
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(paper_prototype, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(paper_prototype, "PAPER_INDEX", paper_index)

    class _Proc:
        returncode = 0
        stdout = "Scaffolded alpha artifact"
        stderr = ""

    monkeypatch.setattr("subprocess.run", lambda *a, **k: _Proc())
    rc = paper_prototype.cmd_paper_to_prototype(
        argparse.Namespace(
            paper_ref="120",
            alpha_id="ofi_from_paper",
            complexity="O1",
            force=False,
        )
    )
    assert rc == 0
    payload = json.loads(paper_index.read_text(encoding="utf-8"))
    assert "ofi_from_paper" in payload["120"]["alphas"]


def test_stamp_and_validate_data_meta(tmp_path: Path) -> None:
    data_path = tmp_path / "feed.npy"
    arr = np.zeros(6, dtype=[("best_bid", "i8"), ("best_ask", "i8"), ("qty", "f8")])
    np.save(data_path, arr)

    rc = data_governance.cmd_stamp_data_meta(
        argparse.Namespace(
            data=str(data_path),
            dataset_id="feed_v1",
            source_type="real",
            source="unit_test",
            owner="charlie",
            schema_version=1,
            symbols="2330",
            split="full",
            out=None,
        )
    )
    assert rc == 0


def test_stamp_and_validate_data_meta_unstructured_array(tmp_path: Path) -> None:
    data_path = tmp_path / "matrix.npy"
    arr = np.zeros((6, 2), dtype=np.float64)
    np.save(data_path, arr)

    rc = data_governance.cmd_stamp_data_meta(
        argparse.Namespace(
            data=str(data_path),
            dataset_id="matrix_v1",
            source_type="synthetic",
            source="unit_test",
            owner="charlie",
            schema_version=1,
            symbols="TXF",
            split="full",
            out=None,
        )
    )
    assert rc == 0
    rc = data_governance.cmd_validate_data_meta(
        argparse.Namespace(
            data=str(data_path),
            meta=None,
        )
    )
    assert rc == 0
    rc = data_governance.cmd_validate_data_meta(
        argparse.Namespace(
            data=str(data_path),
            meta=None,
        )
    )
    assert rc == 0
