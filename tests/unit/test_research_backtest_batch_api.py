from __future__ import annotations

import hashlib
import json

import numpy as np

from research.registry.scorecard import compute_scorecard


def test_compute_scorecard_reads_data_meta_provenance(tmp_path):
    data_path = tmp_path / "synthetic.npy"
    np.save(data_path, np.asarray([1.0, 2.0, 3.0], dtype=np.float64))
    fingerprint = hashlib.sha256(data_path.read_bytes()[:1024]).hexdigest()

    meta_path = tmp_path / "synthetic.npy.meta.json"
    meta_path.write_text(
        json.dumps(
            {
                "rng_seed": 42,
                "generator_script": "research/tools/synth_lob_gen.py",
                "data_ul": 5,
            }
        ),
        encoding="utf-8",
    )

    scorecard = compute_scorecard(
        {"sharpe_oos": 1.2, "regime_ic": {"volatile": 0.08}},
        data_meta_path=str(meta_path),
    )
    assert scorecard.rng_seed == 42
    assert scorecard.generator_script == "research/tools/synth_lob_gen.py"
    assert scorecard.data_ul == 5
    assert scorecard.data_fingerprint == fingerprint
    assert scorecard.regime_ic["volatile"] == 0.08
