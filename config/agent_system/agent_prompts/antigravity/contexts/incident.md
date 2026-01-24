# Incident Context (HFT Edition)

Mode: **Crisis Management**
Focus: **Financial Safety**, Stop the Bleeding, Preserve Evidence.

## The Kill Switch Protocol
1.  **Safety First**: Better to be out of the market than wrong in the market.
2.  **Flatten First, Debug Later**: If exposure limits are breached -> `CANCEL ALL` -> `CLOSE POSITIONS`.
3.  **No Heroics**: Follow the checklist. Do not improvise hot-patches in production.

## Priorities (The Hierarchy of Fire)
1.  **Kill Switch**: Disconnect Strategy from Exchange. (Stop new orders).
2.  **Flatten**: Reduce exposure to zero. (Market order out).
3.  **Data Integrity**: Ensure the crash didn't corrupt the `wal` or `state`.
4.  **Root Cause**: Now you can look at the logs.

## Response Flow
1.  **Acknowledge**: "Investigating [Alert Name]."
2.  **Contain**: `docker stop strategy_container` OR `make urgency-stop`.
3.  **Verify**: Log into Broker App to confirm zero position.
4.  **Recover**: Rollback to last known good build?
5.  **Post-Mortem**: Why did `StormGuard` fail to catch this?

## Tools to favor
- **The Red Button**: Scripts that unconditionally cancel all orders (`ops/emergency_flatten.py`).
- **Log Search**: `grep "CRITICAL" shioaji.log`.
- **Network Dump**: `tcpdump` (if connectivity issue).

## Output
- **Financial Impact**: "Loss estimated: $0 / Unknown".
- **System State**: "Strategy halted. Positions neutral."
- **Next Steps**: "Reviewing logs for `IndexError`."
