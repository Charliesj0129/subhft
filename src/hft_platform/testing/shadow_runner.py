"""WU-19: Shadow trading runner."""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Callable, Sequence

from hft_platform.contracts.strategy import OrderCommand, OrderIntent


@dataclass(slots=True)
class ShadowResult:
    commands: list[OrderCommand] = field(default_factory=list)
    precision_violations: list[str] = field(default_factory=list)
    duration_ns: int = 0


class RecordingOrderAdapter:
    def __init__(self) -> None:
        self.commands: list[OrderCommand] = []

    def submit(self, cmd: OrderCommand) -> None:
        self.commands.append(cmd)

    async def execute(self, cmd: OrderCommand) -> None:
        self.commands.append(cmd)


def _check_precision(intent: OrderIntent) -> list[str]:
    violations: list[str] = []
    if not isinstance(intent.price, int):
        violations.append(f"price is {type(intent.price).__name__}, expected int")
    if not isinstance(intent.qty, int):
        violations.append(f"qty is {type(intent.qty).__name__}, expected int")
    return violations


async def run(events: Sequence[Any], strategy_callback: Callable[[Any], Sequence[OrderIntent] | None]) -> ShadowResult:
    adapter = RecordingOrderAdapter()
    result = ShadowResult()
    start = time.perf_counter_ns()
    cmd_id = 0
    for event in events:
        intents = strategy_callback(event)
        if not intents:
            continue
        for intent in intents:
            violations = _check_precision(intent)
            result.precision_violations.extend(violations)
            cmd_id += 1
            cmd = OrderCommand(
                cmd_id=cmd_id,
                intent=intent,
                deadline_ns=0,
                storm_guard_state=intent.timestamp_ns,
                created_ns=time.perf_counter_ns(),
            )
            adapter.submit(cmd)
    result.commands = adapter.commands
    result.duration_ns = time.perf_counter_ns() - start
    return result
