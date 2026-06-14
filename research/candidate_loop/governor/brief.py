"""Render / parse the human-editable steering brief (the approval gate).

Frontmatter is YAML (``approved: false`` by default); the body is deterministic
Markdown derived from the signals. ``generated_at`` is the only non-deterministic
field and is passed in by the caller so the body is independently byte-stable.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import yaml

from research.candidate_loop.governor.signals import SteeringSignals


@dataclass(frozen=True)
class ParsedBrief:
    family: str
    source_run_id: str
    approved: bool
    focus: str
    n_target: int
    signals: dict[str, Any]
    body: str


def _brief_body(signals: SteeringSignals, focus: str) -> str:
    why = {
        "amplify": (
            f"Survivor IC median {signals.ic_p50:.3f}; "
            f"{signals.maker_rescuable_count} maker-rescuable near-miss(es) — "
            "the live-est the family looked this round; keep pushing it."
        ),
        "maintain": (
            f"Mixed: survival_rate {signals.survival_rate:.2f}, "
            f"IC median {signals.ic_p50:.3f}. Hold allocation."
        ),
        "deprioritize": (
            f"Weak: survival_rate {signals.survival_rate:.2f} with no cheap rescue. "
            "Small probe only."
        ),
        "retire": (
            "No survivors and no maker-rescuable candidates — family looks dead; "
            "minimal probe."
        ),
    }[focus]

    focus_bullets: list[str] = []
    if signals.maker_rescuable_count > 0:
        focus_bullets.append(
            f"- {signals.maker_rescuable_count} candidate(s) failed taker cost but passed "
            "maker — favour signed-flow formulas a maker-execution variant could harvest."
        )
    for nm in signals.near_misses[:3]:
        focus_bullets.append(
            f"- Near-miss {nm.get('alpha_id', '?')} failed `{nm.get('failed_gate', '?')}` "
            f"by margin {float(nm.get('margin', 0.0)):.3f} — vary parameters around it."
        )
    if not focus_bullets:
        focus_bullets.append(
            "- No live signal this round; explore new parameter regions of the family."
        )

    avoid_bullets: list[str] = []
    if signals.duplicate_rate > 0:
        avoid_bullets.append(
            f"- Re-sampling already-tried formulas (duplicate_rate {signals.duplicate_rate:.2f})."
        )
    if signals.reduced_day_coverage_count > 0:
        avoid_bullets.append(
            "- Formulas needing days masked by the dir-clean filter "
            f"(reduced_day_coverage_count {signals.reduced_day_coverage_count})."
        )
    if signals.cost_failure_rate >= 0.5:
        avoid_bullets.append(
            f"- High-turnover taker formulas (cost_failure_rate {signals.cost_failure_rate:.2f})."
        )
    if not avoid_bullets:
        avoid_bullets.append("- No specific anti-patterns flagged this round.")

    return "\n".join(
        [
            f"# Steering brief — {signals.family} (focus: {focus})",
            "",
            "## Why",
            why,
            "",
            "## Focus",
            *focus_bullets,
            "",
            "## Avoid",
            *avoid_bullets,
        ]
    )


def render_brief(
    signals: SteeringSignals,
    *,
    focus: str,
    n_target: int,
    source_run_id: str,
    generated_at: str,
) -> str:
    frontmatter = {
        "family": signals.family,
        "source_run_id": source_run_id,
        "approved": False,
        "focus": focus,
        "n_target": n_target,
        "signals": {
            "candidates": signals.candidates,
            "survival_rate": signals.survival_rate,
            "ic_p50": signals.ic_p50,
            "cost_failure_rate": signals.cost_failure_rate,
            "maker_cost_failure_rate": signals.maker_cost_failure_rate,
            "maker_rescuable_count": signals.maker_rescuable_count,
            "duplicate_rate": signals.duplicate_rate,
            "reduced_day_coverage_count": signals.reduced_day_coverage_count,
        },
        "generated_at": generated_at,
    }
    front = yaml.safe_dump(frontmatter, sort_keys=True, default_flow_style=False).strip()
    body = _brief_body(signals, focus)
    return f"---\n{front}\n---\n\n{body}\n"


def parse_brief(text: str) -> ParsedBrief:
    if not text.startswith("---\n"):
        raise ValueError("brief missing YAML frontmatter")
    end = text.find("\n---", 4)
    if end < 0:
        raise ValueError("brief frontmatter unterminated")
    fm = yaml.safe_load(text[4:end]) or {}
    if not isinstance(fm, dict):
        raise ValueError("brief frontmatter must be a YAML mapping")
    body = text[end + 4 :].lstrip("\n")
    raw_approved = fm.get("approved", False)
    if not isinstance(raw_approved, bool):
        raise ValueError(
            f"brief 'approved' must be a YAML boolean (true/false), got {raw_approved!r}"
        )
    return ParsedBrief(
        family=str(fm.get("family", "")),
        source_run_id=str(fm.get("source_run_id", "")),
        approved=raw_approved,
        focus=str(fm.get("focus", "")),
        n_target=int(fm.get("n_target", 0)),
        signals=dict(fm.get("signals", {}) or {}),
        body=body,
    )


def is_approved(text: str) -> bool:
    return parse_brief(text).approved


__all__ = ["ParsedBrief", "is_approved", "parse_brief", "render_brief"]
