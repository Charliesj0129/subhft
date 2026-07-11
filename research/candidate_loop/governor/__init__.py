"""Candidate loop v1.1 governor (spec 2026-06-14).

Upstream-only feedback layer: reads ``failure_summary.json``, derives
deterministic per-family steering briefs a human approves, then generates the
next round's candidates via DeepSeek. Imports nothing from the frozen scored
path; the frozen loop never imports this package.
"""
