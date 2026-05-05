"""L5: cli/_run.py enforces 100% trace capture when a loop_id is bound.

When ``--loop r47_tmf_v1`` is set (or HFT_LOOP env var, or main.yaml.loop_id):
- The bound loop YAML must declare ``trace_policy: order_path_100pct``.
- The CLI forces ``HFT_DIAG_TRACE_ENABLED=1`` so the sampler's enabled
  gate opens (default is 0 per ``DecisionTraceSampler.from_env``).
- Wrong policy → exit 2; missing/None policy → exit 2.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from hft_platform.cli._run import _enforce_loop_trace_policy


def _write_loop_yaml(tmp_path: Path, loop_id: str, trace_policy: str | None) -> Path:
    """Create config/loops/<loop_id>.yaml under tmp_path and return repo root."""
    loops_dir = tmp_path / "config" / "loops"
    loops_dir.mkdir(parents=True, exist_ok=True)
    body = [f"loop_id: {loop_id}"]
    if trace_policy is not None:
        body.append(f"trace_policy: {trace_policy}")
    (loops_dir / f"{loop_id}.yaml").write_text("\n".join(body) + "\n", encoding="utf-8")
    return tmp_path


@pytest.fixture(autouse=True)
def _isolate_trace_env(monkeypatch):
    """Ensure HFT_DIAG_TRACE_ENABLED is not pre-set across tests."""
    monkeypatch.delenv("HFT_DIAG_TRACE_ENABLED", raising=False)


class TestEnforceLoopTracePolicy:
    """_enforce_loop_trace_policy: only fires when loop_id is set."""

    def test_no_loop_id_is_a_noop(self, monkeypatch):
        monkeypatch.delenv("HFT_DIAG_TRACE_ENABLED", raising=False)
        _enforce_loop_trace_policy({"mode": "sim"})
        assert "HFT_DIAG_TRACE_ENABLED" not in os.environ

    def test_loop_with_correct_policy_forces_trace_enabled(self, tmp_path, monkeypatch):
        repo_root = _write_loop_yaml(tmp_path, "test_loop_v1", "order_path_100pct")
        monkeypatch.chdir(repo_root)

        _enforce_loop_trace_policy({"loop_id": "test_loop_v1"})

        assert os.environ.get("HFT_DIAG_TRACE_ENABLED") == "1"

    def test_loop_with_wrong_policy_refuses_to_start(self, tmp_path, monkeypatch):
        repo_root = _write_loop_yaml(tmp_path, "test_loop_v1", "sampled_5_percent")
        monkeypatch.chdir(repo_root)

        with pytest.raises(SystemExit) as exc_info:
            _enforce_loop_trace_policy({"loop_id": "test_loop_v1"})
        assert exc_info.value.code == 2
        # Must not silently flip the env var when the policy is wrong.
        assert os.environ.get("HFT_DIAG_TRACE_ENABLED") != "1"

    def test_loop_with_missing_policy_refuses_to_start(self, tmp_path, monkeypatch):
        # No `trace_policy:` key at all — must reject, not default-allow.
        repo_root = _write_loop_yaml(tmp_path, "test_loop_v1", None)
        monkeypatch.chdir(repo_root)

        with pytest.raises(SystemExit):
            _enforce_loop_trace_policy({"loop_id": "test_loop_v1"})

    def test_missing_loop_file_is_a_noop(self, tmp_path, monkeypatch):
        # The loader (_bind_loop) already raises LoopBindingError on missing
        # loop file before settings reach cmd_run, so this branch is
        # defensive: if we somehow get here without a file, do not crash —
        # simply skip enforcement (settings did not flow through _bind_loop).
        monkeypatch.chdir(tmp_path)
        _enforce_loop_trace_policy({"loop_id": "ghost"})
        assert os.environ.get("HFT_DIAG_TRACE_ENABLED") != "1"

    def test_real_r47_tmf_v1_loop_yaml_is_compliant(self):
        """Self-test: the committed config/loops/r47_tmf_v1.yaml must declare
        the right policy so a default `hft run --loop r47_tmf_v1` succeeds.
        """
        repo_root = Path(__file__).resolve().parents[2]
        loop_path = repo_root / "config" / "loops" / "r47_tmf_v1.yaml"
        assert loop_path.exists(), f"loop YAML missing: {loop_path}"

        prior_cwd = os.getcwd()
        try:
            os.chdir(repo_root)
            _enforce_loop_trace_policy({"loop_id": "r47_tmf_v1"})
            assert os.environ.get("HFT_DIAG_TRACE_ENABLED") == "1"
        finally:
            os.chdir(prior_cwd)
            os.environ.pop("HFT_DIAG_TRACE_ENABLED", None)
