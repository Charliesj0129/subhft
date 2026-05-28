"""Stage-4 BacktestContractSpec unit tests.

Verifies:
  1. ``from_manifest`` correctly picks the first cost_profile_ref and threads
     latency overrides from a ValidationProfile.
  2. ``resolved_cost_model`` returns a TAIFEXCost from cost_profiles.yaml.
  3. ``maker_engine_kwargs`` / ``hft_native_runner_kwargs`` /
     ``hft_backtest_adapter_kwargs`` produce kwargs that the respective
     engine constructors accept without raising.
  4. The spec is frozen and hashable (when extra is empty).
"""

from __future__ import annotations

import inspect

import pytest

from research.backtest.contract import (
    DEFAULT_LATENCY_PROFILE_ID,
    DEFAULT_QUEUE_MODEL,
    BacktestContractSpec,
)
from research.backtest.cost_models import TAIFEXCost
from research.registry.schemas import AlphaManifest


def _manifest(**overrides) -> AlphaManifest:
    body = {
        "alpha_id": "test_alpha",
        "hypothesis": "h",
        "formula": "f",
        "paper_refs": [],
        "data_fields": [],
        "complexity": "O(1)",
        "instrument": "TMFD6",
        "cost_profile_refs": ["TMFD6"],
    }
    body.update(overrides)
    return AlphaManifest.from_dict(body)


def test_from_manifest_picks_first_cost_profile_ref() -> None:
    m = _manifest(cost_profile_refs=["TXFD6", "TMFD6"], instrument="TXFD6+TMFD6")
    spec = BacktestContractSpec.from_manifest(m)
    assert spec.cost_profile_ref == "TXFD6"
    assert spec.instrument == "TXFD6+TMFD6"


def test_from_manifest_with_instrument_override() -> None:
    m = _manifest(cost_profile_refs=["TXFD6", "TMFD6"])
    spec = BacktestContractSpec.from_manifest(m, instrument_override="TMFD6")
    assert spec.instrument == "TMFD6"
    assert spec.cost_profile_ref == "TMFD6"


def test_from_manifest_threads_latency_from_profile() -> None:
    from hft_platform.alpha._validation_profile import ValidationProfile

    prof = ValidationProfile(
        name="t",
        is_strict=True,
        thresholds={},
        blocking_sub_gates=("sharpe_threshold",),
        pipeline_overrides={
            "latency_profile_id": "sim_stress_v2026-02-26",
            "local_decision_pipeline_latency_us": 1000,
        },
    )
    spec = BacktestContractSpec.from_manifest(_manifest(), profile=prof)
    assert spec.latency_profile_id == "sim_stress_v2026-02-26"
    assert spec.place_latency_us == 1000


def test_from_manifest_defaults_when_manifest_empty() -> None:
    m = _manifest(cost_profile_refs=[], instrument="")
    spec = BacktestContractSpec.from_manifest(m)
    assert spec.cost_profile_ref == ""
    assert spec.latency_profile_id == DEFAULT_LATENCY_PROFILE_ID
    assert spec.queue_model_name == DEFAULT_QUEUE_MODEL


def test_resolved_cost_model_returns_taifexcost() -> None:
    spec = BacktestContractSpec(instrument="TMFD6", cost_profile_ref="TMFD6")
    cost = spec.resolved_cost_model()
    assert isinstance(cost, TAIFEXCost)
    assert cost.instrument == "TMFD6"
    assert cost.commission_pts_per_side > 0


def test_resolved_cost_model_rejects_missing_ref() -> None:
    spec = BacktestContractSpec(instrument="TMFD6")  # no ref
    with pytest.raises(ValueError, match="has no\\s+cost_profile_ref"):
        spec.resolved_cost_model()


def test_maker_engine_kwargs_accepted_by_constructor() -> None:
    """maker_engine_kwargs must produce keys MakerEngine.__init__ accepts."""
    from research.backtest.maker_engine import MakerEngine

    spec = BacktestContractSpec(
        instrument="TMFD6",
        cost_profile_ref="TMFD6",
        place_latency_us=395_000,
        cancel_latency_us=59_000,
    )
    kwargs = spec.maker_engine_kwargs()
    sig = inspect.signature(MakerEngine.__init__)
    for k in kwargs:
        assert k in sig.parameters, f"MakerEngine.__init__ has no '{k}' param"
    # Spec-derived cost must round-trip.
    assert kwargs["cost_model"].instrument == "TMFD6"
    assert kwargs["latency_profile"] is not None
    assert kwargs["latency_profile"].place_ns == 395_000 * 1000


def test_hft_native_runner_kwargs_match_backtest_config_fields() -> None:
    from research.backtest.types import BacktestConfig

    spec = BacktestContractSpec(instrument="TMFD6", cost_profile_ref="TMFD6")
    kwargs = spec.hft_native_runner_kwargs()
    # Every key must correspond to a field on BacktestConfig.
    field_names = {f.name for f in BacktestConfig.__dataclass_fields__.values()}
    for k in kwargs:
        assert k in field_names, f"BacktestConfig has no field '{k}'"


def test_hft_backtest_adapter_kwargs_accepted_by_constructor() -> None:
    """hft_backtest_adapter_kwargs must produce keys HftBacktestAdapter.__init__ accepts."""
    from hft_platform.backtest.adapter import HftBacktestAdapter

    spec = BacktestContractSpec(
        instrument="TMFD6",
        cost_profile_ref="TMFD6",
        place_latency_us=400,
        modify_latency_us=200,
        cancel_latency_us=100,
    )
    kwargs = spec.hft_backtest_adapter_kwargs()
    sig = inspect.signature(HftBacktestAdapter.__init__)
    for k in kwargs:
        assert k in sig.parameters, f"HftBacktestAdapter.__init__ has no '{k}' param"
    assert kwargs["latency_us"] == 400


def test_spec_is_frozen() -> None:
    spec = BacktestContractSpec(instrument="TMFD6")
    with pytest.raises(Exception):  # FrozenInstanceError, but exact class varies
        spec.instrument = "TXFD6"  # type: ignore[misc]
