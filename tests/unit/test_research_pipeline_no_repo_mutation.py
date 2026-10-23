"""The governed research pipeline must not move files in the checkout.

``research.factory.cmd_optimize`` runs ``converge-tools``, which
``shutil.move``s every non-core ``research/tools/*.py`` into ``legacy/``.
Running it as the ``make research`` preflight rewrote the tree on every run.
"""

from __future__ import annotations

import argparse
from pathlib import Path

import research.factory as factory
import research.pipeline as pipeline


def _stub_steps(monkeypatch, calls: list[str]) -> None:
    def _recorder(name: str):
        def _fn(_ns: argparse.Namespace) -> int:
            calls.append(name)
            return 0

        return _fn

    for name in ("init", "converge_tools", "clean", "audit", "index"):
        monkeypatch.setattr(factory, f"cmd_{name}", _recorder(name))


def test_pipeline_preflight_skips_converge_tools(monkeypatch, tmp_path: Path) -> None:
    calls: list[str] = []
    _stub_steps(monkeypatch, calls)

    rc = pipeline._run_factory_optimize(
        tmp_path / "optimize.json",
        tmp_path / "audit.json",
        tmp_path / "index.json",
        data_paths=[],
        allow_audit_warnings=False,
        skip_clean=True,
        skip_index=True,
    )

    assert rc == 0
    assert "converge_tools" not in calls
    assert calls == ["init", "audit"]


def test_factory_optimize_cli_still_converges_by_default(monkeypatch, tmp_path: Path) -> None:
    calls: list[str] = []
    _stub_steps(monkeypatch, calls)

    rc = factory.cmd_optimize(
        argparse.Namespace(
            out=str(tmp_path / "optimize.json"),
            audit_out=str(tmp_path / "audit.json"),
            index_out=str(tmp_path / "index.json"),
            data=[],
            allow_audit_warnings=False,
            skip_clean=True,
            skip_index=True,
        )
    )

    assert rc == 0
    assert calls == ["init", "converge_tools", "audit"]
