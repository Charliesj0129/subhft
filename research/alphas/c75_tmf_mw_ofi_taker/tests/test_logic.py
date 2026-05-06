"""Unit tests for c75_tmf_mw_ofi_taker (2-term post-D2 form).

Pinned contracts:

1. Frozen weights match the manifest (post-D2: 0.667 / 0.333; this is the
   regression that catches an accidental revert to the 0.6 / 0.3 / 0.1
   3-term form).
2. ``deep_depth_momentum_x1000`` is NOT consumed (no IDX_DEEP_*, no
   W_DEEP_MOMENTUM symbols on the impl module).
3. ``should_fire()`` returns 0 before warmup completes.
4. ``should_fire()`` returns 0 in anomalous spread regime even after warmup
   and even with a large signal.
5. ``should_fire()`` returns +/-1 in normal regime when signal exceeds
   1.5 * rolling stdev.
6. ``update()`` accepts FE-v3 features via either ``features=tuple`` kwarg
   or individual canonical-name kwargs.
"""

from __future__ import annotations

import pytest

from research.alphas.c75_tmf_mw_ofi_taker import impl as c75_impl
from research.alphas.c75_tmf_mw_ofi_taker.impl import (
    FIRE_THRESHOLD_STDEV_MULTIPLIER,
    FIRE_THRESHOLD_WINDOW_TICKS,
    IDX_OFI_L1_EMA5S,
    IDX_OFI_L1_EMA30S,
    IDX_SPREAD_EMA30S,
    IDX_SPREAD_EMA300S,
    SPREAD_REGIME_MULTIPLIER,
    W_OFI_5S,
    W_OFI_30S,
    WARMUP_TICKS,
    C75TmfMwOfiTakerAlpha,
)


# Position 27 features for the FE-v3 schema. Indices not used by c75 are
# zero-filled.
FEATURE_LEN = 27


def _ft(*, ofi_5s: float = 0.0, ofi_30s: float = 0.0, spread30: float = 1.0,
        spread300: float = 1.0) -> tuple[float, ...]:
    arr = [0.0] * FEATURE_LEN
    arr[IDX_OFI_L1_EMA5S] = ofi_5s
    arr[IDX_OFI_L1_EMA30S] = ofi_30s
    arr[IDX_SPREAD_EMA30S] = spread30
    arr[IDX_SPREAD_EMA300S] = spread300
    return tuple(arr)


# ---------------------------------------------------------------------------
# Contract 1 + 2: frozen weights, no deep-momentum
# ---------------------------------------------------------------------------


def test_frozen_weights_are_2_term_post_d2() -> None:
    """The renormalised 6:3 ratio must hold; sum to 1.0."""
    assert W_OFI_5S == pytest.approx(0.667)
    assert W_OFI_30S == pytest.approx(0.333)
    assert W_OFI_5S + W_OFI_30S == pytest.approx(1.0)


def test_no_deep_momentum_symbol() -> None:
    """Catch an accidental revert to the 3-term 0.6/0.3/0.1 form."""
    assert not hasattr(c75_impl, "IDX_DEEP_DEPTH_MOMENTUM_X1000")
    assert not hasattr(c75_impl, "W_DEEP_MOMENTUM")


def test_constants_match_manifest() -> None:
    assert WARMUP_TICKS == 300
    assert FIRE_THRESHOLD_WINDOW_TICKS == 300
    assert FIRE_THRESHOLD_STDEV_MULTIPLIER == pytest.approx(1.5)
    assert SPREAD_REGIME_MULTIPLIER == pytest.approx(1.5)


# ---------------------------------------------------------------------------
# Contract 3: warm-up gate
# ---------------------------------------------------------------------------


def test_should_fire_zero_before_warmup() -> None:
    alpha = C75TmfMwOfiTakerAlpha()

    # One update — way below the 300-tick warmup.
    alpha.update(features=_ft(ofi_5s=10.0, ofi_30s=10.0, spread30=1.0, spread300=1.0))

    assert not alpha.is_warm()
    assert alpha.should_fire() == 0


# ---------------------------------------------------------------------------
# Contract 4: anomalous-spread regime gate
# ---------------------------------------------------------------------------


def test_should_fire_zero_in_anomalous_spread_regime() -> None:
    alpha = C75TmfMwOfiTakerAlpha()

    # Warm up with normal-regime quiet signals (zero ofi -> zero signal,
    # zero stdev -> threshold = inf -> nothing fires anyway).
    for _ in range(WARMUP_TICKS):
        alpha.update(features=_ft(spread30=1.0, spread300=1.0))

    assert alpha.is_warm()

    # Now a tick with anomalous spread (regime closed).
    alpha.update(features=_ft(ofi_5s=10.0, ofi_30s=10.0, spread30=10.0, spread300=1.0))

    assert not alpha.spread_regime_ok()
    assert alpha.should_fire() == 0, "anomalous spread must close the gate"


# ---------------------------------------------------------------------------
# Contract 5: threshold gate (normal regime)
# ---------------------------------------------------------------------------


def test_should_fire_buy_in_normal_regime_above_threshold() -> None:
    alpha = C75TmfMwOfiTakerAlpha()

    # Warm up with alternating +/- signals to build a non-zero stdev.
    for i in range(WARMUP_TICKS):
        sign = 1.0 if i % 2 == 0 else -1.0
        alpha.update(features=_ft(ofi_5s=sign, ofi_30s=sign, spread30=1.0, spread300=1.0))

    assert alpha.is_warm()

    # Burst above threshold on the BUY side: large positive ofi.
    alpha.update(features=_ft(ofi_5s=100.0, ofi_30s=100.0, spread30=1.0, spread300=1.0))

    assert alpha.spread_regime_ok()
    assert alpha.fire_threshold() < abs(alpha.get_signal())
    assert alpha.should_fire() == 1


def test_should_fire_sell_in_normal_regime_below_threshold() -> None:
    alpha = C75TmfMwOfiTakerAlpha()

    for i in range(WARMUP_TICKS):
        sign = 1.0 if i % 2 == 0 else -1.0
        alpha.update(features=_ft(ofi_5s=sign, ofi_30s=sign, spread30=1.0, spread300=1.0))

    alpha.update(features=_ft(ofi_5s=-100.0, ofi_30s=-100.0, spread30=1.0, spread300=1.0))

    assert alpha.should_fire() == -1


# ---------------------------------------------------------------------------
# Contract 6: kwarg flexibility
# ---------------------------------------------------------------------------


def test_update_accepts_named_kwargs_without_features_tuple() -> None:
    """Mirrors the bridge enrichment in alpha_strategy_bridge._FE_KEYS_V3
    where individual canonical-name kwargs are populated alongside the
    `features` tuple."""
    alpha = C75TmfMwOfiTakerAlpha()
    alpha.update(
        ofi_l1_ema5s=2.0,
        ofi_l1_ema30s=4.0,
        spread_ema30s=1.0,
        spread_ema300s=1.0,
    )
    expected = W_OFI_5S * 2.0 + W_OFI_30S * 4.0
    assert alpha.get_signal() == pytest.approx(expected)


def test_manifest_round_trip_yaml() -> None:
    """The manifest property must produce a frozen dataclass that survives
    `to_dict() -> from_dict()` round-trip."""
    from research.registry.schemas import AlphaManifest

    alpha = C75TmfMwOfiTakerAlpha()
    m = alpha.manifest
    d = m.to_dict()
    m2 = AlphaManifest.from_dict(d)

    assert m2.alpha_id == "c75_tmf_mw_ofi_taker"
    assert m2.feature_set_version == "lob_shared_v3"
    assert m2.strategy_type == "taker"
    assert m2.instrument == "TMFD6"
    assert "0.667 * ofi_l1_ema5s" in m2.dsl_formula
    assert "0.333 * ofi_l1_ema30s" in m2.dsl_formula
    assert "deep_depth_momentum_x1000" not in m2.dsl_formula
