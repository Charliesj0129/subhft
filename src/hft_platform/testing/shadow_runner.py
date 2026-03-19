"""WU-19: Shadow trading runner.

Provides a ``RecordingOrderAdapter`` that captures ``OrderCommand`` objects
without ever calling a real broker, and a ``run()`` coroutine that drives a
strategy callback over a sequence of events while recording all emitted
commands.  Useful for offline validation and precision-law checks.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Callable, Sequence

from hft_platform.contracts.strategy import OrderCommand, OrderIntent


@dataclass(slots=True)
class ShadowResult:
    """Outcome of a shadow trading session."""

    commands: list[OrderCommand] = field(default_factory=list)
    precision_violations: list[str] = field(default_factory=list)
    duration_ns: int = 0


class RecordingOrderAdapter:
    """Drop-in adapter that records ``OrderCommand`` objects without side effects.

    No broker SDK is imported or called.  This satisfies the shadow-trading
    requirement where we want to observe what *would* have been sent.
    """

    def __init__(self) -> None:
        self.commands: list[OrderCommand] = []

    def submit(self, cmd: OrderCommand) -> None:
        self.commands.append(cmd)

    # Convenience alias matching OrderAdapter.execute signature shape
    async def execute(self, cmd: OrderCommand) -> None:  # noqa: D401
        self.commands.append(cmd)


def _check_precision(intent: OrderIntent) -> list[str]:
    """Validate that price/qty fields obey the Precision Law (scaled int)."""
    violations: list[str] = []
    if not isinstance(intent.price, int):
        violations.append(f"price is {type(intent.price).__name__}, expected int")
    if not isinstance(intent.qty, int):
        violations.append(f"qty is {type(intent.qty).__name__}, expected int")
    return violations


async def run(
    events: Sequence[Any],
    strategy_callback: Callable[[Any], Sequence[OrderIntent] | None],
) -> ShadowResult:
    """Execute a shadow trading session.

    Parameters
    ----------
    events:
        Iterable of market events (e.g. ``TickEvent``).
    strategy_callback:
        ``(event) -> list[OrderIntent] | None``.  Called once per event.

    Returns
    -------
    ShadowResult
        Captured commands, precision violations, and wall-clock duration.
    """
    adapter = RecordingOrderAdapter()
    result = ShadowResult()

    start = time.perf_counter_ns()

    cmd_id = 0
    for event in events:
        intents = strategy_callback(event)
        if not intents:
            continue
        for intent in intents:
            # Precision check
            violations = _check_precision(intent)
            result.precision_violations.extend(violations)

            cmd_id += 1
            cmd = OrderCommand(
                cmd_id=cmd_id,
                intent=intent,
                deadline_ns=0,
                storm_guard_state=intent.timestamp_ns,  # placeholder
                created_ns=time.perf_counter_ns(),
            )
            adapter.submit(cmd)

    result.commands = adapter.commands
    result.duration_ns = time.perf_counter_ns() - start
    return result
