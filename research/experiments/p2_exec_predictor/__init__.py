"""Pivot 2 — Execution / marketability predictor.

Trains P(fill | LOB) and P(adverse | filled, LOB) on the L2 panel
(``research/data/derived/{tmf,txf}_full_2026/``).

Goal: provide an infrastructure layer that any future strategy can use to
gate its quotes — rejecting placements where the joint expectation
``P_fill * E[markout_post_cost | fill]`` is non-positive.

Out of scope here: any strategy logic, any live deployment, any new
regime cuts. The target is mechanical (price-level traded-through) so
this lane cannot suffer the F1-C / R65 cohort-flip pathology.
"""
