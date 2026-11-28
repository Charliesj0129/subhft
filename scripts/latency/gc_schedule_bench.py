"""Compare explicit GC schedules over a simulated 1 Hz supervisor hour.

Unlike gc_pause_bench.py's wall-clock gate, each process runs 3,600 ticks
without sleeping. The heap has 250,000 tracked startup lists plus a warmed
65,536-event ring, with four tracked lists per event (the ab_gc.py harness
reports about 4.5 young objects/event). This is a synthetic workload, not a
production latency prediction. JSON includes every pause for independent analysis.

Run each policy in a fresh process, interleaving at least three repetitions::

    taskset -c 12-13 uv run python scripts/latency/gc_schedule_bench.py --policy baseline
"""

from __future__ import annotations

import argparse
import gc
import json
import time

POLICIES = ("baseline", "every_tick", "adaptive", "gen1", "full_gen2", "freeze", "refreeze")


def scheduled_generations(policy: str, tick: int) -> tuple[int, ...]:
    if tick % (60 if policy == "full_gen2" else 3600) == 0:
        return (0, 2) if policy == "baseline" else (2,)
    if policy == "gen1" and tick % 60 == 0:
        return (1,)
    if policy == "adaptive":
        return (0,) if gc.get_count()[0] >= 10_000 or tick % 10 == 0 else ()
    return (0,) if policy != "baseline" or tick % 10 == 0 else ()


def run(policy: str, events_per_tick: int = 1000, ticks: int = 3600, cycle_every: int = 100) -> dict[str, object]:
    was_enabled = gc.isenabled()
    gc.disable()
    startup = [[index] for index in range(250_000)]
    ring_size = 65_536
    ring = [[index, [index], [1], [2]] for index in range(ring_size)]
    gc.collect(2)
    startup_tracked = sum(gc.is_tracked(item) for item in startup)
    ring_tracked = sum(gc.is_tracked(item) for item in ring)
    pauses: list[tuple[int, int]] = []
    tick_pauses: list[int] = []
    sequence = 0
    frozen = policy in {"freeze", "refreeze"}
    freeze_before = gc.get_freeze_count()
    if frozen:
        gc.freeze()
    frozen_count = gc.get_freeze_count()
    collected = 0

    def collect(generation: int) -> None:
        nonlocal collected
        started = time.perf_counter_ns()
        collected += gc.collect(generation)
        pauses.append((generation, time.perf_counter_ns() - started))

    try:
        for tick in range(1, ticks + 1):
            for _ in range(events_per_tick):
                sequence += 1
                event: list[object] = [sequence, [sequence], [1], [2]]
                ring[sequence % ring_size] = event
                if cycle_every and sequence % cycle_every == 0:
                    # Retain cycles for ~65 ticks so they survive gen0, and
                    # some survive gen1 before becoming unreachable in gen2.
                    event.append(event)
            before = len(pauses)
            # Baseline pays both collections on tick 3600; candidate generations
            # subsume young scans. Report their combined uninterrupted stall too.
            for generation in scheduled_generations(policy, tick):
                if policy == "refreeze" and generation == 2:
                    gc.unfreeze()
                collect(generation)
                if policy == "refreeze" and generation == 2:
                    gc.freeze()
            if len(pauses) > before:
                tick_pauses.append(sum(pause for _, pause in pauses[before:]))
        ordered = sorted(pause for _, pause in pauses)
        by_generation = {
            str(gen): {
                "count": len(values := sorted(pause for generation, pause in pauses if generation == gen)),
                "max_ns": max(values, default=0),
            }
            for gen in (0, 1, 2)
        }
        # Keep the startup heap alive until every measured collection finishes.
        assert len(startup) == startup_tracked == 250_000 and len(ring) == ring_size
        live_cycles = sum(len(item) == 5 for item in ring)
        cycles_created = sequence // cycle_every if cycle_every else 0
        expected_reclaimed = 4 * (cycles_created - live_cycles)
        assert collected >= expected_reclaimed, (collected, expected_reclaimed)
        return {
            "policy": policy,
            "ticks": ticks,
            "events_per_tick": events_per_tick,
            "startup_tracked": startup_tracked,
            "ring_events": ring_tracked,
            "frozen_count": frozen_count,
            "inherited_frozen_count": freeze_before,
            "max_ns": ordered[-1],
            "p99_ns": ordered[(len(ordered) * 99 + 99) // 100 - 1],
            "total_ns": sum(ordered),
            "max_tick_ns": max(tick_pauses),
            "collected": collected,
            "cycle_every": cycle_every,
            "runtime_cycles_created": cycles_created,
            "runtime_cycles_live": live_cycles,
            "expected_reclaimed_objects": expected_reclaimed,
            "by_generation": by_generation,
            "pauses": pauses,
        }
    finally:
        if frozen:
            gc.unfreeze()
        # Do not leave benchmark cycles or process-wide state behind.
        gc.collect(2)
        if was_enabled:
            gc.enable()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--policy", choices=POLICIES, required=True)
    parser.add_argument("--events-per-tick", type=int, default=1000)
    parser.add_argument("--cycle-every", type=int, default=100, help="retained cycle every N events; 0 disables")
    args = parser.parse_args()
    if args.events_per_tick < 1:
        parser.error("--events-per-tick must be positive")
    if args.cycle_every < 0:
        parser.error("--cycle-every must be non-negative")
    print(json.dumps(run(args.policy, args.events_per_tick, cycle_every=args.cycle_every), sort_keys=True))


if __name__ == "__main__":
    main()
