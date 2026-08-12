from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest
import yaml

from hft_platform.contracts.alpha import AlphaStatus
from research.combinatorial.promote import promote_smma_candidate
from research.combinatorial.smma_alpha_adapter import SMMACompiledAlpha

_EXPRESSION = "close_l3_atr14_distance"


def _load_impl(path: Path):
    module_name = f"_smma_promotion_test_{path.parent.name}"
    spec = importlib.util.spec_from_file_location(module_name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    try:
        spec.loader.exec_module(module)
        return module
    finally:
        sys.modules.pop(module_name, None)


def test_promote_smma_candidate_writes_screen_only_draft(tmp_path) -> None:
    candidate_spec = {
        "candidate_id": "candidate-123",
        "root": "TXF",
        "timeframe_min": 60,
        "expression": _EXPRESSION,
        "horizon": "1h",
        "direction": 1,
        "threshold": 0.5,
    }
    alpha_dir = promote_smma_candidate(
        _EXPRESSION,
        alpha_id="smma_test_candidate",
        owner="research",
        instrument="TXFD6",
        candidate_spec=candidate_spec,
        out_dir=tmp_path,
    )

    manifest = yaml.safe_load((alpha_dir / "manifest.yaml").read_text(encoding="utf-8"))
    metadata = manifest["experiment_metadata"]
    assert manifest["status"] == AlphaStatus.DRAFT.value
    assert metadata["family"] == "smma"
    assert metadata["candidate_spec"] == candidate_spec
    assert metadata["screen_only_required"] is True
    assert metadata["promotion_eligible"] is False
    assert "live registry" in (alpha_dir / "README.md").read_text(encoding="utf-8")

    module = _load_impl(alpha_dir / "impl.py")
    alpha = module.ALPHA_CLASS()
    assert isinstance(alpha, SMMACompiledAlpha)
    for index in range(20):
        close = 100.0 + index
        signal = alpha.update(open=close - 0.2, high=close + 0.8, low=close - 0.8, close=close)
    assert isinstance(signal, float)


def test_promote_smma_candidate_refuses_overwrite(tmp_path) -> None:
    kwargs = {
        "alpha_id": "smma_test_candidate",
        "owner": "research",
        "instrument": "TMFD6",
        "candidate_spec": {"candidate_id": "candidate-123"},
        "out_dir": tmp_path,
    }
    promote_smma_candidate(_EXPRESSION, **kwargs)
    with pytest.raises(FileExistsError):
        promote_smma_candidate(_EXPRESSION, **kwargs)
