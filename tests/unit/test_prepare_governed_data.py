from __future__ import annotations

import argparse
import json
from pathlib import Path

import numpy as np

import research.factory as factory
from research.tools.prepare_governed_data import (
    _RESEARCH_DTYPE,
    audit_governed_bundle,
    prepare_clickhouse_export,
    prepare_governed_data,
)


def test_prepare_governed_data_generates_governed_bundle(tmp_path: Path) -> None:
    raw_path = tmp_path / "TXFC6_20260306.jsonl"
    raw_path.write_text(
        "\n".join(
            [
                json.dumps(
                    {
                        "symbol": "TXFC6",
                        "type": "Tick",
                        "ingest_ts": 1772757005017786099,
                        "price_scaled": 33150000000,
                        "volume": 1,
                        "seq_no": 1,
                    }
                ),
                json.dumps(
                    {
                        "symbol": "TXFC6",
                        "type": "BidAsk",
                        "ingest_ts": 1772757005018085849,
                        "bids_price": [33121000000],
                        "bids_vol": [2],
                        "asks_price": [33288000000],
                        "asks_vol": [3],
                        "seq_no": 2,
                    }
                ),
                json.dumps(
                    {
                        "symbol": "TXFC6",
                        "type": "Tick",
                        "ingest_ts": 1772757006018085849,
                        "price_scaled": 33266000000,
                        "volume": 2,
                        "seq_no": 3,
                    }
                ),
                json.dumps(
                    {
                        "symbol": "TXFC6",
                        "type": "BidAsk",
                        "ingest_ts": 1772757007018085849,
                        "bids_price": [33260000000],
                        "bids_vol": [4],
                        "asks_price": [33288000000],
                        "asks_vol": [5],
                        "seq_no": 4,
                    }
                ),
            ]
        )
        + "\n",
        encoding="utf-8",
    )

    out_dir = tmp_path / "processed" / "ofi_execution_126"
    outputs = prepare_governed_data(
        argparse.Namespace(
            alpha_id="ofi_execution_126",
            input=str(raw_path),
            out_dir=str(out_dir),
            symbol="TXFC6",
            tag=None,
            split="full",
            owner="tests",
            source="clickhouse_export",
            source_type="real",
            price_scale=1_000_000,
            limit=None,
            paper_ref=[],
            chunk_size=2,
        )
    )

    research_path = Path(outputs["research_path"])
    hftbt_path = Path(outputs["hftbt_path"])
    snapshot_path = Path(outputs["hftbt_snapshot_path"])
    research_meta_path = Path(outputs["research_meta_path"])
    hftbt_meta_path = Path(outputs["hftbt_meta_path"])

    assert research_path.exists()
    assert hftbt_path.exists()
    assert snapshot_path.exists()
    assert research_meta_path.exists()
    assert hftbt_meta_path.exists()

    primary = np.load(research_path, allow_pickle=False)
    primary_arr = np.asarray(primary["data"])
    assert primary_arr.dtype == _RESEARCH_DTYPE
    assert primary_arr.shape == (4,)
    assert primary_arr[0]["bid_px"] == 33150.0
    assert primary_arr[0]["ask_px"] == 33150.0
    assert primary_arr[0]["mid_price"] == 33150.0
    assert primary_arr[0]["volume"] == 1.0
    assert primary_arr[1]["bid_px"] == 33121.0
    assert primary_arr[1]["ask_px"] == 33288.0
    assert primary_arr[1]["volume"] == 0.0
    assert primary_arr[2]["mid_price"] == 33266.0
    assert primary_arr[2]["volume"] == 2.0
    primary.close()

    hbt = np.load(hftbt_path, allow_pickle=False)
    hbt_arr = np.asarray(hbt["data"])
    assert hbt_arr.shape[0] == 8
    assert int(hbt_arr[0]["exch_ts"]) == 1772757005018085849
    hbt.close()

    snapshot = np.load(snapshot_path, allow_pickle=False)
    snapshot_arr = np.asarray(snapshot["data"])
    assert snapshot_arr.shape[0] == 2
    snapshot.close()

    research_meta = json.loads(research_meta_path.read_text(encoding="utf-8"))
    assert research_meta["generator"] == "prepare_governed_data"
    assert research_meta["paper_refs"] == []
    assert research_meta["bundle"]["layout"] == "research_hftbt_bundle_v1"
    assert research_meta["rows"] == 4
    assert research_meta["symbols"] == ["TXFC6"]
    assert research_meta["parameters"]["price_scale"] == 1_000_000
    assert research_meta["regimes_covered"] == ["real_market"]

    hftbt_meta = json.loads(hftbt_meta_path.read_text(encoding="utf-8"))
    assert hftbt_meta["generator"] == "prepare_governed_data"
    assert hftbt_meta["rows"] == 8
    assert hftbt_meta["symbols"] == ["TXFC6"]
    assert hftbt_meta["parameters"]["source_name"] == "clickhouse_export"
    assert hftbt_meta["regimes_covered"] == ["real_market"]


def test_prepare_clickhouse_export_infers_symbol_and_limit(tmp_path: Path) -> None:
    raw_path = tmp_path / "TXFC6_20260306.jsonl"
    raw_path.write_text(
        "\n".join(
            [
                json.dumps(
                    {
                        "symbol": "TXFC6",
                        "type": "BidAsk",
                        "ingest_ts": 1,
                        "bids_price": [1000000],
                        "bids_vol": [2],
                        "asks_price": [1010000],
                        "asks_vol": [3],
                    }
                ),
                json.dumps(
                    {
                        "symbol": "TXFC6",
                        "type": "Tick",
                        "ingest_ts": 2,
                        "price_scaled": 1005000,
                        "volume": 5,
                    }
                ),
                json.dumps(
                    {
                        "symbol": "TXFC6",
                        "type": "Tick",
                        "ingest_ts": 3,
                        "price_scaled": 1007000,
                        "volume": 6,
                    }
                ),
            ]
        )
        + "\n",
        encoding="utf-8",
    )

    bundle = prepare_clickhouse_export(
        input_path=raw_path,
        output_dir=tmp_path / "bundle",
        alpha_id="queue_imbalance",
        owner="tests",
        split="full",
        symbol=None,
        source="clickhouse_export",
        price_scale=1_000_000,
        limit=2,
        chunk_size=1,
    )

    arr = np.load(bundle.primary_data, allow_pickle=False)["data"]
    assert arr.shape[0] == 2
    meta = json.loads(bundle.primary_meta.read_text(encoding="utf-8"))
    assert meta["symbols"] == ["TXFC6"]


def test_bundle_audit_warns_on_missing_hftbt(tmp_path: Path) -> None:
    primary_path = tmp_path / "alpha_research.npz"
    np.savez_compressed(
        str(primary_path),
        data=np.zeros(1, dtype=_RESEARCH_DTYPE),
    )
    audit = audit_governed_bundle(primary_path)
    assert audit["ok"] is True
    assert "missing_hftbt" in audit["warnings"]
    assert "missing_hftbt_snapshot" in audit["warnings"]


def test_prepare_governed_data_is_classified_as_core_tool() -> None:
    assert "prepare_governed_data.py" in factory.CORE_TOOL_FILES
