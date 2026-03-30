"""Options analytics package — offline pricing, Greeks, vol surface.

Float exception: Per Architecture Governance Rule 25 §11, float is permitted
in this package for offline research computation. The live_adapter module
(Phase 2) is the boundary that converts float → int/bool before any value
enters the live trading path.
"""
