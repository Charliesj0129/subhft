from __future__ import annotations

import numpy as np

from research.tools.synth_lob_gen import SyntheticLOBConfig, generate_lob_data
from research.tools.vm_ul import DataUL, validate_meta_ul


def test_generate_lob_data_shape_and_fields() -> None:
    cfg = SyntheticLOBConfig(n_rows=512, rng_seed=42)
    arr, meta = generate_lob_data(cfg)

    assert arr.shape == (512,)
    assert arr.dtype.names == (
        "bid_qty",
        "ask_qty",
        "bid_px",
        "ask_px",
        "mid_price",
        "spread_bps",
        "volume",
        "local_ts",
    )
    assert meta["rows"] == 512
    assert meta["data_ul"] == 5


def test_generate_lob_data_ul5_meta_compliance() -> None:
    cfg = SyntheticLOBConfig(n_rows=128, rng_seed=7)
    _arr, meta = generate_lob_data(cfg)
    ok, missing = validate_meta_ul(meta, DataUL.UL5)

    assert ok is True
    assert missing == []
    assert set(meta["regimes_covered"]).issubset(set(cfg.regimes))
    assert len(meta["regimes_covered"]) >= 1


def test_generate_lob_data_rng_seed_deterministic() -> None:
    cfg = SyntheticLOBConfig(n_rows=256, rng_seed=99)
    arr1, meta1 = generate_lob_data(cfg)
    arr2, meta2 = generate_lob_data(cfg)

    assert np.array_equal(arr1, arr2)
    assert meta1["data_fingerprint"] == meta2["data_fingerprint"]

