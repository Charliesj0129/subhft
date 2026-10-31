"""The governed research pipeline must not move, create or delete files in the checkout.

Its preflight used to be ``research.factory optimize`` (init, converge-tools,
clean, audit), and ``converge-tools`` ``shutil.move``d every non-core
``research/tools/*.py`` into ``legacy/``. research v2 C6 left the factory with
only the read-only ``audit`` and ``index``; the preflight runs ``audit`` alone.
"""

from __future__ import annotations

import argparse
from pathlib import Path

import research.factory as factory
import research.pipeline as pipeline


def test_pipeline_preflight_runs_only_the_read_only_audit(monkeypatch, tmp_path: Path) -> None:
    seen: list[argparse.Namespace] = []
    monkeypatch.setattr(factory, "cmd_audit", lambda ns: seen.append(ns) or 0)

    rc = pipeline._run_factory_audit(tmp_path / "audit.json", data_paths=["d.npy"], allow_audit_warnings=False)

    assert rc == 0
    assert len(seen) == 1
    assert seen[0].out == str(tmp_path / "audit.json")
    assert seen[0].fail_on_warning is True
    assert seen[0].data == ["d.npy"]


def test_pipeline_preflight_allows_warnings_only_when_asked(monkeypatch, tmp_path: Path) -> None:
    seen: list[argparse.Namespace] = []
    monkeypatch.setattr(factory, "cmd_audit", lambda ns: seen.append(ns) or 1)

    rc = pipeline._run_factory_audit(tmp_path / "audit.json", data_paths=None, allow_audit_warnings=True)

    assert rc == 1
    assert seen[0].fail_on_warning is False
    assert seen[0].data == []


def test_factory_exposes_only_audit_and_index() -> None:
    sub = next(a for a in factory.build_parser()._actions if isinstance(a, argparse._SubParsersAction))

    assert set(sub.choices) == {"audit", "index"}
    for mutating in ("cmd_init", "cmd_clean", "cmd_converge_tools", "cmd_optimize"):
        assert not hasattr(factory, mutating)
