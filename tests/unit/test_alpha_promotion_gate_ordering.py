"""Tests for Gate C pass verification in promote_alpha() (I-05).

Covers:
- _verify_gate_c_passed raises ValueError when meta.json shows gate_c=False
- _verify_gate_c_passed succeeds when meta.json shows gate_c=True
- _verify_gate_c_passed emits a WARNING and proceeds when meta.json is missing
- _verify_gate_c_passed raises ValueError when gate_status key is missing from meta.json
- promote_alpha() raises ValueError (propagated from _verify_gate_c_passed) when gate_c=False
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from hft_platform.alpha._validation_profile import ValidationProfile
from hft_platform.alpha.promotion import _verify_gate_c_passed


def _strict_profile() -> ValidationProfile:
    return ValidationProfile(
        name="test", is_strict=True, thresholds={}, blocking_sub_gates=("sharpe_threshold",)
    )

# ---------------------------------------------------------------------------
# Unit tests for _verify_gate_c_passed
# ---------------------------------------------------------------------------


def _write_scorecard(directory: Path) -> Path:
    """Write a minimal valid scorecard.json and return its path."""
    sc = {
        "sharpe_oos": 2.0,
        "max_drawdown": 0.05,
        "turnover": 1.0,
        "correlation_pool_max": 0.3,
    }
    path = directory / "scorecard.json"
    path.write_text(json.dumps(sc))
    return path


def _write_meta(directory: Path, *, gate_c: bool) -> None:
    meta = {"gate_status": {"gate_c": gate_c}}
    (directory / "meta.json").write_text(json.dumps(meta))


def test_verify_gate_c_raises_when_gate_c_false(tmp_path: Path) -> None:
    """Promotion must be blocked when meta.json records gate_c=False."""
    sc_path = _write_scorecard(tmp_path)
    _write_meta(tmp_path, gate_c=False)

    with pytest.raises(ValueError, match="Gate C has not passed"):
        _verify_gate_c_passed(sc_path)


def test_verify_gate_c_passes_when_gate_c_true(tmp_path: Path) -> None:
    """No exception raised when meta.json records gate_c=True."""
    sc_path = _write_scorecard(tmp_path)
    _write_meta(tmp_path, gate_c=True)

    # Must not raise
    result = _verify_gate_c_passed(sc_path)

    # Gate C passed — function returns None (no exception, no side effects)
    assert result is None


def test_verify_gate_c_warns_and_proceeds_when_no_meta(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """Legacy scorecard with no meta.json should proceed with a WARNING.

    structlog writes to stdout by default in non-configured environments, so
    we capture stdout instead of using caplog (which only sees the stdlib
    logging bridge).
    """
    sc_path = _write_scorecard(tmp_path)
    # No meta.json written

    # Must not raise
    _verify_gate_c_passed(sc_path)

    captured = capsys.readouterr()
    # structlog emits the event key in the rendered output
    assert "gate_c_verification_skipped_no_meta" in captured.out or "meta.json" in captured.out, (
        f"Expected a warning about missing meta.json in stdout; got: {captured.out!r}"
    )


def test_verify_gate_c_raises_when_gate_status_missing(tmp_path: Path) -> None:
    """meta.json without a gate_status key should block promotion."""
    sc_path = _write_scorecard(tmp_path)
    (tmp_path / "meta.json").write_text(json.dumps({"other_key": "value"}))

    with pytest.raises(ValueError, match="Gate C has not passed"):
        _verify_gate_c_passed(sc_path)


# ---------------------------------------------------------------------------
# Integration: promote_alpha propagates the Gate C error
# ---------------------------------------------------------------------------


def test_promote_alpha_raises_when_gate_c_false(tmp_path: Path) -> None:
    """promote_alpha() must propagate ValueError from _verify_gate_c_passed."""
    from hft_platform.alpha.promotion import PromotionConfig, promote_alpha

    sc_path = _write_scorecard(tmp_path)
    _write_meta(tmp_path, gate_c=False)

    config = PromotionConfig(
        alpha_id="test_alpha_i05",
        owner="test_owner",
        project_root=str(tmp_path),
        scorecard_path=str(sc_path),
        validation_profile=_strict_profile(),
    )

    with pytest.raises(ValueError, match="Gate C has not passed"):
        promote_alpha(config)
