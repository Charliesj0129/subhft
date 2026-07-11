"""Governed artifact marker for T1-B V0 viability research.

The executable V0 detector lives in ``research.t1.regime_viability`` so this
candidate package can satisfy the factory artifact contract without creating
live strategy wiring.  T1-B is not promotion-eligible until Gate C emits a
governed edge scorecard with latency/cost/parity evidence.
"""

from __future__ import annotations

from pathlib import Path

ALPHA_ID = "t1b_txf_volcompress_tmf"
SPEC_PATH = Path(__file__).with_name("spec.yaml")


def is_promotion_eligible_v0() -> bool:
    """V0 viability artifacts are never paper/live promotion evidence."""
    return False

