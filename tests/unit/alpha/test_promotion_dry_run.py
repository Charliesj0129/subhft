"""Tests for the dry-run no-mutation contract of ``promote_alpha``.

Codex adversarial-review 2026-05-06 finding 6 (MEDIUM) identified that the
``--dry-run`` plumbing in ``hft alpha promote`` suppressed kill-ledger and
promotion-config writes but did NOT suppress
``_update_manifest_status`` calls on Gate D / Gate E pass, which mutate
``research/alphas/<alpha_id>/impl.py`` (durable working-tree write).

Research v2 (ADR-005 section 6) removed that rewriter: status is derived
from the verdict ledger. These tests pin that ``impl.py`` stays
byte-identical on a Gate D/E pass whether or not ``dry_run`` is set.
"""

from __future__ import annotations

import json
from pathlib import Path

from hft_platform.alpha._validation_profile import ValidationProfile
from hft_platform.alpha.promotion import PromotionConfig, promote_alpha

_IMPL_PY_TEMPLATE = '''"""Synthetic alpha impl.py used by promotion-dry-run tests."""

from hft_platform.alpha.types import AlphaStatus


# The status literal the removed regex rewriter used to change.
status=AlphaStatus.GATE_C
'''


def _strict_profile() -> ValidationProfile:
    return ValidationProfile(
        name="dry_run_test",
        is_strict=True,
        thresholds={},
        blocking_sub_gates=("sharpe_threshold",),
    )


def _write_alpha_fixture(
    tmp_path: Path,
    alpha_id: str,
    *,
    sharpe: float = 1.6,
    max_drawdown: float = -0.08,
    turnover: float = 0.2,
    corr: float = 0.3,
) -> tuple[Path, Path]:
    alpha_dir = tmp_path / "research" / "alphas" / alpha_id
    alpha_dir.mkdir(parents=True, exist_ok=True)

    impl_path = alpha_dir / "impl.py"
    impl_path.write_text(_IMPL_PY_TEMPLATE, encoding="utf-8")

    scorecard_path = alpha_dir / "scorecard.json"
    scorecard_path.write_text(
        json.dumps(
            {
                "sharpe_oos": sharpe,
                "max_drawdown": max_drawdown,
                "turnover": turnover,
                "correlation_pool_max": corr,
                "latency_profile": "sim_p95_v2026-02-26",
                "replay_parity": {"match_pct": 100.0},
                # Gate D's cost-uncertainty audit errors without this payload.
                "strategy_type": "taker",
                "cost_uncertainty": {"passed": True, "metrics": {"p95_lower_bound_pts": 0.5}},
            },
            indent=2,
            sort_keys=True,
        ),
        encoding="utf-8",
    )

    return impl_path, scorecard_path


def _make_config(tmp_path: Path, alpha_id: str, *, dry_run: bool) -> PromotionConfig:
    return PromotionConfig(
        alpha_id=alpha_id,
        owner="charlie",
        project_root=str(tmp_path),
        shadow_sessions=6,
        drift_alerts=0,
        execution_reject_rate=0.0,
        validation_profile=_strict_profile(),
        dry_run=dry_run,
    )


# ---------------------------------------------------------------------------
# dry_run=True: impl.py must NOT be mutated on Gate D / Gate E pass
# ---------------------------------------------------------------------------


def test_dry_run_gate_d_pass_does_not_mutate_impl_status(tmp_path: Path) -> None:
    impl_path, _ = _write_alpha_fixture(tmp_path, "dry_run_alpha_d")
    before = impl_path.read_text(encoding="utf-8")
    assert "status=AlphaStatus.GATE_C" in before

    result = promote_alpha(_make_config(tmp_path, "dry_run_alpha_d", dry_run=True))

    after = impl_path.read_text(encoding="utf-8")
    assert after == before, "impl.py was mutated during dry_run=True"
    assert result.gate_d_passed is True


def test_dry_run_full_promotion_pass_does_not_mutate_impl_status(tmp_path: Path) -> None:
    """Gate D and Gate E both pass under dry_run=True and impl.py is unchanged."""
    impl_path, _ = _write_alpha_fixture(tmp_path, "dry_run_alpha_de")
    before = impl_path.read_text(encoding="utf-8")

    result = promote_alpha(_make_config(tmp_path, "dry_run_alpha_de", dry_run=True))

    after = impl_path.read_text(encoding="utf-8")
    assert after == before, "impl.py was mutated during dry_run=True (Gate E path)"
    assert result.gate_d_passed is True
    assert result.gate_e_passed is True


def test_dry_run_does_not_write_promotion_config(tmp_path: Path) -> None:
    """Adjacent contract: dry_run also suppresses the
    ``config/strategy_promotions/<alpha_id>.yaml`` write. This is already
    covered by existing dry-run plumbing; pinned here so a regression in
    ``write_promotion_config`` gating is caught."""
    _write_alpha_fixture(tmp_path, "dry_run_alpha_yaml")

    result = promote_alpha(_make_config(tmp_path, "dry_run_alpha_yaml", dry_run=True))

    assert result.approved
    assert result.promotion_config_path is None, "dry_run should not write promotion YAML"


# ---------------------------------------------------------------------------
# dry_run=False: the promotion runs for real and still leaves impl.py alone
# ---------------------------------------------------------------------------


def test_non_dry_run_promotion_pass_does_not_mutate_impl_status(tmp_path: Path) -> None:
    impl_path, _ = _write_alpha_fixture(tmp_path, "wet_run_alpha_d")
    before = impl_path.read_text(encoding="utf-8")

    result = promote_alpha(_make_config(tmp_path, "wet_run_alpha_d", dry_run=False))

    assert result.gate_d_passed is True
    assert result.gate_e_passed is True
    assert result.promotion_config_path is not None, "the wet run must really have promoted"
    assert impl_path.read_text(encoding="utf-8") == before, "promotion rewrote impl.py status"
