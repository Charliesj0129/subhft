"""Forward the `hft alpha` research subcommands to ``python -m research alpha`` (ADR-005, research v2 C5).

The platform parser still defines these subcommands' arguments (``--help``,
argument errors and the docs contract check stay on ``hft``), but their bodies
live in ``research/registry/alpha_cli.py`` and run in a child process, so the
platform never imports research code. ``research/`` is dev-only and absent
from production images; there the forward fails with a message instead of an
import error.
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parents[3]


def forward_to_research(args: argparse.Namespace) -> None:
    """Marker handler: ``hft`` main() forwards the raw argv instead of calling this."""
    raise RuntimeError("forward_to_research is dispatched by hft_platform.cli.main, not called")


def run_research_alpha(argv: list[str], *, root: Path = _PROJECT_ROOT) -> int:
    """Run ``python -m research alpha <argv>`` from the checkout at ``root``; return its exit code."""
    if not (root / "research" / "__main__.py").is_file():
        print(
            f"`hft alpha {argv[0] if argv else ''}` needs the research/ checkout, which this install does not have",
            file=sys.stderr,
        )
        return 2
    env = dict(os.environ)
    env["PYTHONPATH"] = os.pathsep.join(p for p in (str(root), env.get("PYTHONPATH", "")) if p)
    return subprocess.run([sys.executable, "-m", "research", "alpha", *argv], env=env, check=False).returncode
