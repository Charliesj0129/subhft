"""Per-event-deadline exit scanners vs. the already-tested scalar originals.

`pdq_causal_uncapped_exit.py` needed deadline-array variants of
`armed_flip_exit_times` / `liquidity_exit_times_for_events` (the originals
take one shared `max_hold_s` scalar; an uncapped backstop needs a per-event
day-end deadline instead). These tests hold the ground truth fixed: with a
uniform deadline array equal to `entry_s + max_hold_s`, the deadline variant
must return exactly what the original scalar function returns.
"""

from __future__ import annotations

import numpy as np

from research.tools.pdq_causal_uncapped_exit import (
    armed_flip_exit_times_deadline,
    exit_search,
    liquidity_exit_times_deadline,
)

# `exit_search` is the same dynamically-loaded pdq_supertrend_exit_search
# module `pdq_causal_uncapped_exit` uses internally. A second, independent
# `import research.tools.pdq_supertrend_exit_search` here would register a
# second copy of its @njit(cache=True) functions under a different module
# name while sharing the same on-disk numba cache file (keyed by source
# path, not module identity) -- whichever copy compiles second reads a
# stale, mismatched cache entry and crashes. Reusing the one already-loaded
# copy avoids that collision.
armed_flip_exit_times = exit_search.armed_flip_exit_times
liquidity_exit_times_for_events = exit_search.liquidity_exit_times_for_events

MAX_HOLD_S = 900


def test_armed_flip_deadline_matches_scalar_when_deadline_equals_entry_plus_hold() -> None:
    bar_end_s = np.array([60, 120, 180, 240, 300, 360, 420], dtype=np.int64)
    states = np.array([1, 1, -1, -1, 1, 1, -1], dtype=np.int8)
    entry_s = np.array([50, 200], dtype=np.int64)
    position_dirs = np.array([1, -1], dtype=np.int8)
    execution_seconds = bar_end_s

    scalar_result = armed_flip_exit_times(
        bar_end_s,
        states,
        entry_s,
        position_dirs,
        max_hold_s=MAX_HOLD_S,
        execution_seconds=execution_seconds,
    )
    deadline_result = armed_flip_exit_times_deadline(
        bar_end_s,
        states,
        entry_s,
        position_dirs,
        entry_s + MAX_HOLD_S,
        execution_seconds=execution_seconds,
    )

    np.testing.assert_array_equal(scalar_result, deadline_result)


def test_liquidity_deadline_matches_scalar_when_deadline_equals_entry_plus_hold() -> None:
    seconds = np.arange(0, 400, 5, dtype=np.int64)
    rng = np.random.default_rng(20260709)
    depth = 100.0 + rng.normal(0, 1, size=len(seconds))
    depth[10:] *= 1.5
    spread = 4.0 - rng.normal(0, 0.01, size=len(seconds))
    spread[10:] *= 0.4
    zlogl = rng.normal(0, 0.01, size=len(seconds))
    zlogl[10:] += 1.0
    entry_indices = np.array([0], dtype=np.int64)
    entry_s = seconds[entry_indices]

    scalar_result = liquidity_exit_times_for_events(
        seconds,
        depth,
        spread,
        zlogl,
        entry_indices=entry_indices,
        entry_s=entry_s,
        max_hold_s=MAX_HOLD_S,
        min_depth_ratio=1.3,
        max_spread_ratio=0.5,
        min_zlogl_delta=0.0,
        confirmations=3,
        max_observation_gap_s=5,
    )
    deadline_result = liquidity_exit_times_deadline(
        seconds,
        depth,
        spread,
        zlogl,
        entry_indices,
        entry_s + MAX_HOLD_S,
        min_depth_ratio=1.3,
        max_spread_ratio=0.5,
        min_zlogl_delta=0.0,
        confirmations=3,
        max_observation_gap_s=5,
    )

    np.testing.assert_array_equal(scalar_result, deadline_result)
    assert scalar_result[0] >= 0


def test_armed_flip_deadline_finds_signal_beyond_old_fixed_hold_window() -> None:
    # A flip that only completes after 900s must still be found once the
    # per-event deadline is pushed past it -- this is the whole point of
    # removing the fixed hold.
    bar_end_s = np.arange(60, 2000, 60, dtype=np.int64)
    states = np.ones(len(bar_end_s), dtype=np.int8)
    flip_index = int(np.searchsorted(bar_end_s, 1200))
    states[flip_index:] = -1
    entry_s = np.array([0], dtype=np.int64)
    position_dirs = np.array([1], dtype=np.int8)

    capped = armed_flip_exit_times_deadline(
        bar_end_s,
        states,
        entry_s,
        position_dirs,
        entry_s + MAX_HOLD_S,
        execution_seconds=bar_end_s,
    )
    uncapped = armed_flip_exit_times_deadline(
        bar_end_s,
        states,
        entry_s,
        position_dirs,
        np.array([bar_end_s[-1]], dtype=np.int64),
        execution_seconds=bar_end_s,
    )

    assert capped[0] == -1
    assert uncapped[0] == bar_end_s[flip_index]
