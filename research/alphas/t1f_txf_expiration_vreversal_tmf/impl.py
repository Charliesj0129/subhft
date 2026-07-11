"""Governed artifact marker for T1-F V0 viability research.

The executable V0 detector lives in ``research.t1.regime_viability`` so this
candidate package can satisfy the factory artifact contract without creating
live strategy wiring.  T1-F is not promotion-eligible until Gate C emits a
governed edge scorecard with latency/cost/parity evidence -- and, distinctly
for this candidate, until the paired dataset spans enough monthly settlements
to clear the V0 sample floor (see README "Structural sample blocker").
"""

from __future__ import annotations

from pathlib import Path

ALPHA_ID = "t1f_txf_expiration_vreversal_tmf"
SPEC_PATH = Path(__file__).with_name("spec.yaml")


def is_promotion_eligible_v0() -> bool:
    """V0 viability artifacts are never paper/live promotion evidence."""
    return False
