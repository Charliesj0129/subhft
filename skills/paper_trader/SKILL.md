---
name: Paper Trade Architect
description: Expert capability to run rigorous, Level-3 simulations (Digital Twin) for HFT strategies.
---

# Skill: Paper Trade Architect

## 1. Philosophy: The Digital Twin

You do not "test" strategies. You "subject them to reality".
The Paper Trade environment must be mathematically indistinguishable from the Live environment, minus the actual financial transfer.

## 2. Capability: Shadow Ledger Management

When asked to "Audit" or "verify revenue":

1.  **Do NOT** use `api.balance`.
2.  **USE** the local `shadow_ledger.db`.
3.  **Calculate**:
    - **Gross PnL**: $\sum (ExitPrice - EntryPrice) * Vol$.
    - **Net PnL**: $Gross - (Fees + Tax)$.
    - **Ghost Rate**: % of orders filled in Sim that had < 10% probability of fill in Live (based on Queue depth).

## 3. Capability: Microstructure Forensics

If a strategy performs well in Paper but failed in Live:

1.  **Latency Mismatch**: Check if `sim_latency` distribution matches `live_latency` logs.
2.  **Queue Jumping**: Did the Sim assume immediate fills entering the queue? (Verify `OPI_Sim` logic).
3.  **Impact Blindness**: Did the Sim account for the fact that _our_ order would have eaten the liquidity?

## 4. Workflow: Enabling the Twin

To enable this mode for a user session:

1.  **Inject the Proxy**: Replace `api = sj.Shioaji()` with `api = ShioajiRealSim()`.
2.  **Load Physics**: Ensure `hft-sim-engine` is running in background.
3.  **Sync Data**: Ensure `market_data` stream is piping to both Strategy and Sim Engine.

## 5. Commands

- `@audit`: Generate a PnL report from the Shadow Ledger.
- `@verify_physics`: Compare Sim execution timestamps vs Tick timestamps to check for causal violations (Time Travel).
