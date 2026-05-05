"""L6 — CLI screen vs validate split tests.

Covers:
  * ``hft alpha validate`` without ``--profile`` exits with code 2.
  * ``hft alpha validate --profile <loose>`` exits with code 2.
  * ``hft alpha screen`` stamps ``screen_only=true`` on the scorecard.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest


def _validate_args(profile: str | None = None, **overrides: Any) -> argparse.Namespace:
    base: dict[str, Any] = {
        "alpha_id": "TEST_ALPHA",
        "data": ["data/synthetic.npz"],
        "is_oos_split": 0.7,
        "signal_threshold": 0.3,
        "max_position": 5,
        "min_sharpe_oos": 0.0,
        "max_abs_drawdown": 0.3,
        "skip_gate_b_tests": False,
        "pytest_timeout": 300,
        "experiments_dir": "research/experiments",
        "out": None,
        "profile": profile,
    }
    base.update(overrides)
    return argparse.Namespace(**base)


def test_validate_without_profile_exits_2(capsys: pytest.CaptureFixture[str]) -> None:
    from hft_platform.cli._alpha import cmd_alpha_validate

    with pytest.raises(SystemExit) as excinfo:
        cmd_alpha_validate(_validate_args(profile=None))
    assert excinfo.value.code == 2
    err = capsys.readouterr().err
    assert "--profile is required" in err
    assert "screen" in err.lower()


def test_validate_with_loose_profile_exits_2(
    capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    from hft_platform.cli._alpha import cmd_alpha_validate

    loose = tmp_path / "loose.yaml"
    loose.write_text(
        "name: loose_test\nis_strict: false\n"
        "thresholds: {}\nblocking_sub_gates: []\n"
    )

    with pytest.raises(SystemExit) as excinfo:
        cmd_alpha_validate(_validate_args(profile=str(loose)))
    assert excinfo.value.code == 2
    err = capsys.readouterr().err
    assert "is not strict" in err


def test_validate_with_missing_profile_path_exits_2(
    capsys: pytest.CaptureFixture[str],
) -> None:
    from hft_platform.cli._alpha import cmd_alpha_validate

    with pytest.raises(SystemExit) as excinfo:
        cmd_alpha_validate(_validate_args(profile="/nonexistent/path/to/profile.yaml"))
    assert excinfo.value.code == 2
    err = capsys.readouterr().err
    assert "profile" in err.lower()


def test_screen_stamps_screen_only_on_scorecard(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from hft_platform.cli import _alpha as cli_alpha

    scorecard_path = tmp_path / "scorecard.json"
    scorecard_path.write_text(json.dumps({"sharpe_oos": 0.5, "max_drawdown": -0.1}))

    fake_result = SimpleNamespace(
        passed=True,
        scorecard_path=str(scorecard_path),
        to_dict=lambda: {
            "alpha_id": "TEST_ALPHA",
            "passed": True,
            "scorecard_path": str(scorecard_path),
        },
    )

    def fake_run_alpha_validation(_config: Any) -> Any:
        return fake_result

    fake_module = SimpleNamespace(
        ValidationConfig=SimpleNamespace,
        run_alpha_validation=fake_run_alpha_validation,
    )
    import sys as _sys

    monkeypatch.setitem(_sys.modules, "hft_platform.alpha.validation", fake_module)

    args = argparse.Namespace(
        alpha_id="TEST_ALPHA",
        data=["data/synthetic.npz"],
        is_oos_split=0.7,
        signal_threshold=0.3,
        max_position=5,
        min_sharpe_oos=0.0,
        max_abs_drawdown=0.3,
        skip_gate_b_tests=False,
        pytest_timeout=300,
        experiments_dir="research/experiments",
        out=None,
    )

    cli_alpha.cmd_alpha_screen(args)

    payload = json.loads(scorecard_path.read_text())
    assert payload["screen_only"] is True
    assert payload["screen_profile"] == "loose_default"
    assert "screen_timestamp" in payload
    assert payload["sharpe_oos"] == 0.5
