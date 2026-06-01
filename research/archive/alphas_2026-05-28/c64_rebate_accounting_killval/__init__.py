"""C64 R3/T5 kill-validation — rebate-accounting sweep on C33 baseline.

No strategy implementation: C64 was SELF-KILLED at Researcher T1
(see outputs/team_artifacts/alpha-research/round-3/summary.md) on the
grounds that the rebate-aware WIDENING LAYER has no physical decision
axis on TXFD6 (rebate/adverse ratio = 0.06 on TXF, vs 1.25 on TMF). Any
"rebate-aware maker" on TXFD6 collapses to passive rebate accounting on
C33's existing fills.

This module runs an empirical accounting sweep (C33 mp={1,2,3} at inst RT,
rebate-rate sweep {0, 5, 10, 15 NTD/side}) to validate Researcher T1's
arithmetic claim that passive rebate on C33 yields +2-27 NTD/day uplift
and does NOT warrant a separate C64 strategy.
"""
