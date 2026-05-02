"""Platform-wide constants for strategy identification and position attribution."""

# Strategy ID assigned to positions that originate from manual broker operations,
# reconciliation auto-corrections, or broker-only recovery (no checkpoint).
# Replaces the former "*" wildcard which caused recovery positions to leak
# into per-strategy queries via net_qty_for_symbol().
MANUAL_STRATEGY_ID: str = "MANUAL"
