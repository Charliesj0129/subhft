"""Gate C maker lane must not fall back to instant-RTT when the latency profile is unusable."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from hft_platform.alpha import _gate_c
from hft_platform.alpha._validation_types import ValidationConfig
from hft_platform.alpha.gate_c_runtime import GateCMakerRuntime, GateCRuntime, GateCTakerRuntime
from hft_platform.alpha.latency_profiles import resolve_profile


class _Latency:
    def __init__(self, place_ns: int, cancel_ns: int) -> None:
        self.place_ns = place_ns
        self.cancel_ns = cancel_ns


def _resolver(table: dict[str, dict[str, Any]]) -> Any:
    def resolve(name: str) -> dict[str, Any]:
        return table[name]

    return resolve


GOOD = {"p": {"submit_ack_latency_ms": 395.0, "cancel_ack_latency_ms": 59.0}}


def test_a_resolvable_profile_becomes_the_injected_latency() -> None:
    profile, failure = _gate_c._resolve_maker_latency_profile("p", _resolver(GOOD), _Latency)

    assert failure is None
    assert (profile.place_ns, profile.cancel_ns) == (395_000_000, 59_000_000)


def test_an_empty_profile_name_is_reported_missing_not_defaulted() -> None:
    profile, failure = _gate_c._resolve_maker_latency_profile("", _resolver(GOOD), _Latency)

    assert profile is None
    assert failure is not None and failure["reason"] == "latency_profile_missing"


def test_an_unknown_profile_name_is_reported_unresolved() -> None:
    profile, failure = _gate_c._resolve_maker_latency_profile("nope", _resolver(GOOD), _Latency)

    assert profile is None
    assert failure is not None
    assert failure["reason"] == "latency_profile_unresolved"
    assert failure["profile"] == "nope"


def test_a_profile_missing_a_latency_field_is_reported_unresolved() -> None:
    table = {"p": {"submit_ack_latency_ms": 395.0}}

    profile, failure = _gate_c._resolve_maker_latency_profile("p", _resolver(table), _Latency)

    assert profile is None
    assert failure is not None and failure["reason"] == "latency_profile_unresolved"


class _Source:
    def health_check(self) -> None:
        return None


class _ForbiddenEngine:
    constructed = False

    def __init__(self, **_: Any) -> None:
        type(self).constructed = True


def _runtime() -> GateCRuntime:
    maker = GateCMakerRuntime(
        load_cost_profile=lambda _instrument: object(),
        queue_depletion_fill=lambda **_: object(),
        clickhouse_source=_Source,
        latency_profile=_Latency,
        maker_engine=_ForbiddenEngine,
        result_store=object,
    )
    return GateCRuntime(
        hft_native_runner=object,
        ensure_hftbt_npz=lambda _path: None,
        backtest_config=object,
        walk_forward_config=object,
        compute_scorecard=lambda *_a, **_k: None,
        load_maker_runtime=lambda: maker,
        load_taker_runtime=lambda: GateCTakerRuntime(lambda _i: object(), object, object),
    )


def _alpha(latency_profile: str) -> Any:
    manifest = SimpleNamespace(alpha_id="x", strategy_type="maker", instrument="TMFD6", latency_profile=latency_profile)
    return SimpleNamespace(manifest=manifest)


@pytest.mark.parametrize(
    ("profile", "reason"),
    [("", "latency_profile_missing"), ("not_a_profile", "latency_profile_unresolved")],
)
def test_gate_c_fails_without_running_the_backtest_when_the_profile_is_unusable(
    tmp_path: Path, profile: str, reason: str
) -> None:
    _ForbiddenEngine.constructed = False
    config = ValidationConfig(alpha_id="x", data_paths=[])

    report, run_id, _, _, _ = _gate_c.run_gate_c(
        _alpha(profile), config, tmp_path, [], tmp_path / "exp", runtime=_runtime()
    )

    assert report.passed is False
    assert report.details["reason"] == reason
    assert run_id == ""
    assert _ForbiddenEngine.constructed is False


def test_the_alias_for_r52_resolves_to_the_measured_shioaji_numbers() -> None:
    resolved = resolve_profile("v2026-04-24_measured")

    assert float(resolved["submit_ack_latency_ms"]) == 395.0
    assert float(resolved["cancel_ack_latency_ms"]) == 59.0
