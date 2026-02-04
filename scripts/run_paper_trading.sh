#!/bin/bash
# Launch Paper Trading for Rust Alpha Strategy
# Strategies:
#   1. Alpha (Rust): Deep Imbalance + Momentum + Hawkes
#   2. Risk Management: Position Limits

echo "Building Rust Extension..."
uv run maturin develop --release

echo "Starting Paper Trading (Simulation Mode)..."
echo "Strategy: rust_alpha"
echo "Symbols: TXFB6, 2330"

# Check for Credentials
if [ -z "$SHIOAJI_API_KEY" ]; then
    echo "Warning: SHIOAJI_API_KEY not set. Using Simulation Mock (if available) or failing."
fi

# Run
uv run python3 -m hft_platform.cli run sim \
    --strategy rust_alpha \
    --strategy-module hft_platform.strategies.rust_alpha \
    --strategy-class Strategy \
    --symbols TXFB6 2330
