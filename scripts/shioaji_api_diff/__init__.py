"""Shioaji SDK version-diff tooling.

Reverse-engineers the Shioaji broker-SDK API surface across versions by pure
static/dynamic introspection (NO live broker connection), classifies each
change from the platform's perspective, and emits committed golden snapshots +
a human runbook that drives the shioaji upgrade go/no-go decision.

Subcommands (``python -m scripts.shioaji_api_diff <cmd>``):
  orchestrate   Install each version in a throwaway /tmp venv and capture its surface.
  diff          Structured diff + classification between two captured surfaces.
  report        diff + emit machine JSON and the Markdown runbook.
  guard-regen   Recapture the CURRENTLY-installed shioaji surface into its golden.

The in-venv capture engine lives in ``_capture_entrypoint`` and imports only the
standard library plus ``shioaji`` so it runs in a bare throwaway venv.
"""

from __future__ import annotations

__all__ = ["__version__"]

# Bumped when the snapshot JSON format or classification semantics change.
__version__ = "1.0.0"
