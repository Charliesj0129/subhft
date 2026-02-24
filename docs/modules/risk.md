# risk

## Purpose

Pre-trade risk checks and safety controls. Every OrderIntent must pass through risk before becoming an OrderCommand.

## Key Files

| File                  | Key Class                                    | Purpose                                        |
| --------------------- | -------------------------------------------- | ---------------------------------------------- |
| `risk/engine.py`      | `RiskEngine`                                 | Main risk pipeline (evaluate → approve/reject) |
| `risk/validators.py`  | `PriceBandValidator`, `MaxNotionalValidator` | Pluggable validators                           |
| `risk/storm_guard.py` | `StormGuardFSM`                              | System-wide safety state machine               |
| `risk/base.py`        | `BaseValidator`                              | Validator interface                            |

## Processing Flow

```
OrderIntent → RiskEngine.evaluate()
  → foreach validator: validate(intent) → (pass/fail, reason)
  → if all pass → RiskDecision(approved=True)
  → RiskEngine.create_command(intent) → OrderCommand
  → else → RiskDecision(approved=False, reason_code="...")
```

## StormGuardFSM

| State  | Value | Behavior                                            |
| ------ | ----- | --------------------------------------------------- |
| NORMAL | 0     | All orders allowed                                  |
| WARM   | 1     | Warning state, enhanced logging                     |
| STORM  | 2     | Reduced order flow, cancel-only for some strategies |
| HALT   | 3     | **ALL order flow stopped**                          |

Triggers: latency spikes, excessive feed gaps, manual override, strategy circuit breaker.

## Adding a New Validator

```python
from hft_platform.risk.base import BaseValidator

class MyValidator(BaseValidator):
    def validate(self, intent) -> tuple[bool, str]:
        if intent.qty > self.config.max_qty:
            return False, "EXCEEDS_MAX_QTY"
        return True, "OK"
```

Register in `config/risk.yaml` under `validators:`.

## Configuration

- `config/strategy_limits.yaml`: Per-strategy position/notional limits.
- `config/risk.yaml`: Global risk config and validator list.

## Gotchas

- Risk evaluation is **synchronous CPU-only** — no I/O allowed. This ensures sub-microsecond latency.
- StormGuard state is propagated via bus AND embedded in OrderCommand for downstream awareness.
- In gateway mode, risk is called as step 4 of the 7-step pipeline (after dedup, policy, exposure).
