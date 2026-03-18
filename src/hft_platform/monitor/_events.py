"""Event detection, opportunity scoring, and dominant-alpha labelling.

Pure functions — no side effects, fully testable.
"""

from __future__ import annotations

import math

from hft_platform.monitor._types import EventFlag, SymbolState

# Short display labels for alpha IDs
_ALPHA_SHORT: dict[str, str] = {
    "queue_imbalance": "QI",
    "flow_mode_decomp": "FMD",
    "microprice_momentum": "MM",
    "ofi_regime": "OFI_R",
    "sqrt_ofi": "sOFI",
    "depth_depletion_asym": "DA",
}


def snapshot_prev(ss: SymbolState) -> None:
    """Copy current values to prev_* fields. Called at cycle start."""
    ss.prev_composite = ss.composite
    ss.prev_spread_bps = ss.spread_bps
    ss.prev_is_stale = ss.is_stale
    # Derive agree direction from current alpha_states
    pos = neg = 0
    for astate in ss.alpha_states.values():
        if astate.disabled or math.isnan(astate.signal):
            continue
        if astate.signal > 0:
            pos += 1
        elif astate.signal < 0:
            neg += 1
    if pos > neg:
        ss.prev_agree_direction = 1
    elif neg > pos:
        ss.prev_agree_direction = -1
    else:
        ss.prev_agree_direction = 0


def detect_events(ss: SymbolState, now_ns: int) -> None:
    """Compare current vs prev, set event_flags bitmask + last_event_ns."""
    flags = EventFlag.NONE

    # Composite sign flip
    if ss.prev_composite != 0.0 and ss.composite != 0.0:
        if (ss.prev_composite > 0) != (ss.composite > 0):
            flags |= EventFlag.COMPOSITE_CROSS

    # Sigma breaks
    prev_sigma = abs(ss.prev_composite)
    curr_sigma = abs(ss.composite)
    if prev_sigma < 1.0 <= curr_sigma or prev_sigma < 2.0 <= curr_sigma:
        flags |= EventFlag.SIGMA_BREAK_UP
    if curr_sigma < 1.0 <= prev_sigma or curr_sigma < 2.0 <= prev_sigma:
        flags |= EventFlag.SIGMA_BREAK_DOWN

    # Agree direction flip
    pos = neg = 0
    for astate in ss.alpha_states.values():
        if astate.disabled or math.isnan(astate.signal):
            continue
        if astate.signal > 0:
            pos += 1
        elif astate.signal < 0:
            neg += 1
    curr_dir = 1 if pos > neg else (-1 if neg > pos else 0)
    if ss.prev_agree_direction != 0 and curr_dir != 0 and curr_dir != ss.prev_agree_direction:
        flags |= EventFlag.AGREE_FLIP

    # Spread convergence / widening
    if ss.prev_spread_bps > 0 and ss.spread_bps > 0:
        ratio = ss.spread_bps / ss.prev_spread_bps
        if ratio < 0.7:
            flags |= EventFlag.SPREAD_CONVERGE
        elif ratio > 1.5:
            flags |= EventFlag.SPREAD_WIDEN

    # Stale transitions
    if ss.is_stale and not ss.prev_is_stale:
        flags |= EventFlag.STALE_ENTER
    elif not ss.is_stale and ss.prev_is_stale:
        flags |= EventFlag.STALE_RESOLVE

    ss.event_flags = flags
    ss.composite_delta = ss.composite - ss.prev_composite
    ss.composite_delta_abs = abs(ss.composite_delta)
    if flags != EventFlag.NONE:
        ss.last_event_ns = now_ns


def dominant_alpha_label(ss: SymbolState) -> str:
    """Return compact label of alphas driving composite direction.

    E.g. "QI+MM", "FMD", "all". Top 2 by |z_score|, aligned with composite direction.
    """
    if not ss.alpha_states:
        return ""
    comp_dir = 1 if ss.composite > 0 else (-1 if ss.composite < 0 else 0)
    if comp_dir == 0:
        return ""

    aligned: list[tuple[float, str]] = []
    for astate in ss.alpha_states.values():
        if astate.disabled or math.isnan(astate.signal):
            continue
        sig_dir = 1 if astate.signal > 0 else (-1 if astate.signal < 0 else 0)
        if sig_dir == comp_dir:
            aligned.append((abs(astate.z_score), astate.alpha_id))

    if not aligned:
        return ""

    total_active = sum(
        1 for a in ss.alpha_states.values() if not a.disabled and not math.isnan(a.signal)
    )
    if len(aligned) == total_active and total_active > 2:
        return "all"

    aligned.sort(key=lambda t: t[0], reverse=True)
    top = aligned[:2]
    return "+".join(_ALPHA_SHORT.get(aid, aid[:3].upper()) for _, aid in top)


def compute_opportunity_score(ss: SymbolState, warmup_ticks: int) -> float:
    """Compute opportunity score for ranking symbols.

    Higher = more actionable. Closed/stale get large negatives.
    """
    if ss.is_closed:
        return -1000.0
    if ss.is_stale:
        return -500.0
    if ss.tick_count < warmup_ticks:
        return -100.0

    # |composite| strength
    comp_abs = abs(ss.composite)

    # Agreement ratio
    total = 0
    dominant = 0
    pos = neg = 0
    for astate in ss.alpha_states.values():
        if astate.disabled or math.isnan(astate.signal):
            continue
        total += 1
        if astate.signal > 0:
            pos += 1
        elif astate.signal < 0:
            neg += 1
    dominant = max(pos, neg)
    agree_ratio = dominant / max(total, 1)

    # Spread penalty (higher spread = less actionable)
    spread_penalty = min(ss.spread_bps / 100.0, 1.0) if ss.spread_bps > 0 else 0.0

    return comp_abs * agree_ratio * (1.0 - spread_penalty * 0.5)


def format_event_label(flag: EventFlag, ss: SymbolState) -> str:
    """Return human-readable label for the most significant event flag."""
    if flag & EventFlag.COMPOSITE_CROSS:
        arrow = "▲" if ss.composite > 0 else "▼"
        return f"crossed 0 {arrow}"
    if flag & EventFlag.SIGMA_BREAK_UP:
        sigma = abs(ss.composite)
        level = "2σ" if sigma >= 2.0 else "1σ"
        arrow = "▲" if ss.composite > 0 else "▼"
        return f"broke {level}{arrow}"
    if flag & EventFlag.SIGMA_BREAK_DOWN:
        return "fell below σ"
    if flag & EventFlag.AGREE_FLIP:
        pos = neg = 0
        for a in ss.alpha_states.values():
            if not a.disabled and not math.isnan(a.signal):
                if a.signal > 0:
                    pos += 1
                elif a.signal < 0:
                    neg += 1
        arrow = "▲" if pos > neg else "▼"
        return f"agree flip {arrow}"
    if flag & EventFlag.SPREAD_CONVERGE:
        return "spread↓"
    if flag & EventFlag.SPREAD_WIDEN:
        return "spread↑"
    if flag & EventFlag.STALE_ENTER:
        return "went stale"
    if flag & EventFlag.STALE_RESOLVE:
        return "back live"
    return ""
