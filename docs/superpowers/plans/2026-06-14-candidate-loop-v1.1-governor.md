# Candidate Loop v1.1 Governor Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a feedback "governor" that reads a finished run's `failure_summary.json`, derives deterministic per-family steering briefs a human approves, and then has DeepSeek generate the next round's candidate JSONL within that steering — all upstream of the unchanged frozen v1.0 loop.

**Architecture:** A new isolated sub-package `research/candidate_loop/governor/` (the frozen loop never imports it). `signals.py` turns `failure_summary.json` into typed per-family signals + a config-driven focus label; `brief.py` renders/parses human-editable Markdown briefs with an `approved:` gate; `client.py` is a thin, redacting, fail-closed DeepSeek httpx client; `runner.py` orchestrates `draft` (deterministic) and `generate` (enforces the gate, freezes the raw drop, reuses the existing `generate_drop`, writes a provenance manifest). The CLI gains `governor draft` / `governor generate`.

**Tech Stack:** Python 3.12, `httpx` (DeepSeek OpenAI-compatible HTTP), `PyYAML` (brief frontmatter + config), `pytest` with `httpx.MockTransport` (no network in tests). Reuses `research/candidate_loop/generate.py` (`generate_drop`, `build_header`), `failure_summary.py` schema, and `scoring.py` config-loading patterns verbatim.

---

## Spec

Design spec: `docs/superpowers/specs/2026-06-14-candidate-loop-v1.1-governor-design.md` (approved 2026-06-14).

## Key invariants (do not violate)

- **No change** to `evaluator.py` / `scoring.py` / gates / split definitions / ClickHouse schema / any frozen version string / the >10pt floor / the live registry.
- The governor only **reads** `failure_summary.json` and **writes** candidate JSONL + provenance sidecars; it reuses `generate_drop` unchanged.
- `DEEPSEEK_API_KEY` lives in `.env` only — never logged, committed, printed, or passed as a CLI arg. The client reads it from the environment, redacts it, and fails closed when absent. TLS verify stays on.
- The human approval gate is mandatory: `governor generate` refuses any family whose brief has `approved: false`. There is no auto-approval flag.
- `research/candidate_loop/candidates/` and `runs/` are already gitignored (`.gitignore:279-280`), so raw LLM drops (`candidates/<gen_run>/_governor_raw/`) and steering briefs (`runs/<run>/steering/`) are auto-ignored — **no `.gitignore` change is needed**.

## File Structure

Create:
- `research/candidate_loop/governor/__init__.py` — package marker + public re-exports.
- `research/candidate_loop/governor/signals.py` — `GovernorConfig`, `load_governor_config`, `SteeringSignals`, `extract_signals`, `classify_focus`, `n_target_for`, `FOCUS_LABELS`.
- `research/candidate_loop/governor/brief.py` — `ParsedBrief`, `render_brief`, `parse_brief`, `is_approved`.
- `research/candidate_loop/governor/client.py` — `DeepSeekError`, `DeepSeekClient`.
- `research/candidate_loop/governor/runner.py` — `draft_briefs`, `generate_from_briefs`.
- `config/research/candidate_loop/governor_v1.yaml` — focus thresholds, n_target map, model bounds.
- Tests (flat, matching the existing `tests/unit/research/candidate_loop/` convention — all 19 sibling test files are flat there):
  - `tests/unit/research/candidate_loop/test_governor_signals.py`
  - `tests/unit/research/candidate_loop/test_governor_brief.py`
  - `tests/unit/research/candidate_loop/test_governor_client.py`
  - `tests/unit/research/candidate_loop/test_governor_runner.py`
  - `tests/unit/research/candidate_loop/test_governor_cli.py`

Modify:
- `pyproject.toml` — add `httpx` to the `research` dependency group (currently transitive only).
- `research/candidate_loop/__main__.py` — add the `governor` subcommand (`draft` / `generate`).

---

## Task 1: Dependency + governor config + config loader

**Files:**
- Modify: `pyproject.toml` (the `research = [ ... ]` group, ~line 46-58)
- Create: `config/research/candidate_loop/governor_v1.yaml`
- Create: `research/candidate_loop/governor/__init__.py`
- Create: `research/candidate_loop/governor/signals.py`
- Test: `tests/unit/research/candidate_loop/test_governor_signals.py`

- [ ] **Step 1: Add httpx to the research dependency group**

In `pyproject.toml`, inside the `research = [` list, after the `pyarrow` line, add:

```toml
    "httpx>=0.28",  # candidate_loop v1.1 governor: DeepSeek OpenAI-compatible client
```

- [ ] **Step 2: Create the governor config file**

Create `config/research/candidate_loop/governor_v1.yaml`:

```yaml
# Alpha Candidate Loop v1.1 — governor (steering brief derivation + DeepSeek client).
# The governor lives UPSTREAM of the frozen loop: it never changes gates, scoring,
# schema, frozen versions, or the >10pt floor. These knobs only affect how many
# candidates are requested per family and the brief prose — never eligibility.
governor_version: gov_v1

model:
  name: deepseek-chat            # OpenAI-compatible; exact SKU is a config string
  base_url: https://api.deepseek.com
  max_tokens: 8192
  timeout_seconds: 120.0
  max_retries: 2
  temperature: 0.7

# Deterministic focus classification (signals.classify_focus). Priority order:
# amplify > retire > deprioritize > maintain. Labels only set n_target + prose.
focus:
  amplify_survival_min: 0.15       # survival_rate >= this → amplify
  amplify_ic_p50_min: 0.05         # survivor median IC >= this → amplify
  deprioritize_survival_max: 0.05  # survival_rate <= this (and no rescue) → deprioritize
  near_miss_margin_abs_max: 0.10   # a single-gate failure within this of passing is "cheap to flip"
  n_target:
    amplify: 30
    maintain: 20
    deprioritize: 10
    retire: 5
```

- [ ] **Step 3: Create the package marker**

Create `research/candidate_loop/governor/__init__.py`:

```python
"""Candidate loop v1.1 governor (spec 2026-06-14).

Upstream-only feedback layer: reads ``failure_summary.json``, derives
deterministic per-family steering briefs a human approves, then generates the
next round's candidates via DeepSeek. Imports nothing from the frozen scored
path; the frozen loop never imports this package.
"""
```

- [ ] **Step 4: Write the failing test for config loading + signal extraction + focus**

Create `tests/unit/research/candidate_loop/test_governor_signals.py`:

```python
"""Governor signals: config loading, failure_summary extraction, focus labels."""

from __future__ import annotations

from pathlib import Path

import pytest

from research.candidate_loop.governor.signals import (
    classify_focus,
    extract_signals,
    load_governor_config,
    n_target_for,
)

CFG_PATH = (
    Path(__file__).resolve().parents[4]
    / "config" / "research" / "candidate_loop" / "governor_v1.yaml"
)


def _summary() -> dict:
    return {
        "run_id": "smoke_001",
        "per_family": {
            "trade_flow": {  # alive → amplify
                "candidates": 20,
                "survival_rate": 0.10,
                "ic_distribution_survivors": {"p10": 0.02, "p50": 0.114, "p90": 0.20},
                "cost_failure_rate": 0.55,
                "maker_cost_failure_rate": 0.40,
                "maker_rescuable_count": 2,
                "duplicate_rate": 0.05,
                "reduced_day_coverage_count": 7,
                "near_misses": [
                    {"alpha_id": "a1", "failed_gate": "cost_proxy_taker", "margin": -0.03}
                ],
                "common_failure_patterns": [],
            },
            "microprice": {  # dead, no rescue → retire
                "candidates": 20,
                "survival_rate": 0.0,
                "ic_distribution_survivors": {"p10": 0.0, "p50": 0.0, "p90": 0.0},
                "cost_failure_rate": 1.0,
                "maker_cost_failure_rate": 1.0,
                "maker_rescuable_count": 0,
                "duplicate_rate": 0.0,
                "reduced_day_coverage_count": 0,
                "near_misses": [],
                "common_failure_patterns": [],
            },
            "depth_delta": {  # weak, no rescue → deprioritize
                "candidates": 20,
                "survival_rate": 0.03,
                "ic_distribution_survivors": {"p10": 0.0, "p50": 0.01, "p90": 0.02},
                "cost_failure_rate": 0.3,
                "maker_cost_failure_rate": 0.2,
                "maker_rescuable_count": 0,
                "duplicate_rate": 0.0,
                "reduced_day_coverage_count": 0,
                "near_misses": [],
                "common_failure_patterns": [],
            },
            "spread_regime": {  # middling, no rescue → maintain
                "candidates": 20,
                "survival_rate": 0.08,
                "ic_distribution_survivors": {"p10": 0.0, "p50": 0.02, "p90": 0.03},
                "cost_failure_rate": 0.2,
                "maker_cost_failure_rate": 0.1,
                "maker_rescuable_count": 0,
                "duplicate_rate": 0.0,
                "reduced_day_coverage_count": 0,
                "near_misses": [],
                "common_failure_patterns": [],
            },
        },
    }


def test_load_governor_config_reads_thresholds_and_model():
    cfg = load_governor_config(CFG_PATH)
    assert cfg.governor_version == "gov_v1"
    assert cfg.model_name == "deepseek-chat"
    assert cfg.base_url.startswith("https://")
    assert cfg.n_target["amplify"] == 30
    assert cfg.amplify_ic_p50_min == pytest.approx(0.05)


def test_extract_signals_maps_failure_summary_fields():
    sig = extract_signals(_summary(), "trade_flow")
    assert sig.family == "trade_flow"
    assert sig.survival_rate == pytest.approx(0.10)
    assert sig.ic_p50 == pytest.approx(0.114)
    assert sig.maker_rescuable_count == 2
    assert sig.reduced_day_coverage_count == 7
    assert sig.near_misses[0]["failed_gate"] == "cost_proxy_taker"


@pytest.mark.parametrize(
    "family,expected",
    [
        ("trade_flow", "amplify"),
        ("microprice", "retire"),
        ("depth_delta", "deprioritize"),
        ("spread_regime", "maintain"),
    ],
)
def test_classify_focus_is_deterministic_per_family(family, expected):
    cfg = load_governor_config(CFG_PATH)
    sig = extract_signals(_summary(), family)
    assert classify_focus(sig, cfg) == expected


def test_n_target_for_maps_focus_to_count():
    cfg = load_governor_config(CFG_PATH)
    assert n_target_for("amplify", cfg) == 30
    assert n_target_for("retire", cfg) == 5
```

- [ ] **Step 5: Run the test to verify it fails**

Run: `uv run pytest tests/unit/research/candidate_loop/test_governor_signals.py -q`
Expected: FAIL with `ModuleNotFoundError: No module named 'research.candidate_loop.governor.signals'`

- [ ] **Step 6: Implement `signals.py`**

Create `research/candidate_loop/governor/signals.py`:

```python
"""failure_summary.json → typed per-family steering signals + focus label.

Pure module: the only IO is reading the governor YAML config and the summary
dict the caller already loaded. No ClickHouse, no frozen-loop imports.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

FOCUS_LABELS: tuple[str, ...] = ("amplify", "maintain", "deprioritize", "retire")


@dataclass(frozen=True)
class GovernorConfig:
    governor_version: str
    model_name: str
    base_url: str
    max_tokens: int
    timeout_seconds: float
    max_retries: int
    temperature: float
    amplify_survival_min: float
    amplify_ic_p50_min: float
    deprioritize_survival_max: float
    near_miss_margin_abs_max: float
    n_target: dict[str, int]


def load_governor_config(path: Path) -> GovernorConfig:
    raw = yaml.safe_load(path.read_text())
    model = raw["model"]
    focus = raw["focus"]
    return GovernorConfig(
        governor_version=str(raw["governor_version"]),
        model_name=str(model["name"]),
        base_url=str(model["base_url"]),
        max_tokens=int(model["max_tokens"]),
        timeout_seconds=float(model["timeout_seconds"]),
        max_retries=int(model["max_retries"]),
        temperature=float(model["temperature"]),
        amplify_survival_min=float(focus["amplify_survival_min"]),
        amplify_ic_p50_min=float(focus["amplify_ic_p50_min"]),
        deprioritize_survival_max=float(focus["deprioritize_survival_max"]),
        near_miss_margin_abs_max=float(focus["near_miss_margin_abs_max"]),
        n_target={str(k): int(v) for k, v in focus["n_target"].items()},
    )


@dataclass(frozen=True)
class SteeringSignals:
    family: str
    candidates: int
    survival_rate: float
    ic_p10: float
    ic_p50: float
    ic_p90: float
    cost_failure_rate: float
    maker_cost_failure_rate: float
    maker_rescuable_count: int
    duplicate_rate: float
    reduced_day_coverage_count: int
    near_misses: list[dict[str, Any]]
    common_failure_patterns: list[str]


def extract_signals(summary: dict[str, Any], family: str) -> SteeringSignals:
    fam = summary.get("per_family", {}).get(family, {}) or {}
    ic = fam.get("ic_distribution_survivors", {}) or {}
    return SteeringSignals(
        family=family,
        candidates=int(fam.get("candidates", 0)),
        survival_rate=float(fam.get("survival_rate", 0.0)),
        ic_p10=float(ic.get("p10", 0.0)),
        ic_p50=float(ic.get("p50", 0.0)),
        ic_p90=float(ic.get("p90", 0.0)),
        cost_failure_rate=float(fam.get("cost_failure_rate", 0.0)),
        maker_cost_failure_rate=float(fam.get("maker_cost_failure_rate", 0.0)),
        maker_rescuable_count=int(fam.get("maker_rescuable_count", 0)),
        duplicate_rate=float(fam.get("duplicate_rate", 0.0)),
        reduced_day_coverage_count=int(fam.get("reduced_day_coverage_count", 0)),
        near_misses=list(fam.get("near_misses", []) or []),
        common_failure_patterns=list(fam.get("common_failure_patterns", []) or []),
    )


def _has_cheap_near_miss(signals: SteeringSignals, cfg: GovernorConfig) -> bool:
    # near_miss margins are signed distance-to-passing; a single-gate failure has
    # margin < 0. "cheap to flip" = within the band just below the threshold.
    return any(
        float(nm.get("margin", -1.0)) >= -cfg.near_miss_margin_abs_max
        for nm in signals.near_misses
    )


def classify_focus(signals: SteeringSignals, cfg: GovernorConfig) -> str:
    """Deterministic priority: amplify > retire > deprioritize > maintain."""
    rescue = signals.maker_rescuable_count > 0 or _has_cheap_near_miss(signals, cfg)
    if (
        signals.survival_rate >= cfg.amplify_survival_min
        or signals.ic_p50 >= cfg.amplify_ic_p50_min
        or rescue
    ):
        return "amplify"
    if signals.survival_rate == 0.0 and not rescue:
        return "retire"
    if signals.survival_rate <= cfg.deprioritize_survival_max:
        return "deprioritize"
    return "maintain"


def n_target_for(focus: str, cfg: GovernorConfig) -> int:
    return cfg.n_target[focus]


__all__ = [
    "FOCUS_LABELS",
    "GovernorConfig",
    "SteeringSignals",
    "classify_focus",
    "extract_signals",
    "load_governor_config",
    "n_target_for",
]
```

- [ ] **Step 7: Run the test to verify it passes**

Run: `uv run pytest tests/unit/research/candidate_loop/test_governor_signals.py -q`
Expected: PASS (7 tests: 1 config, 1 extract, 4 focus params, 1 n_target)

- [ ] **Step 8: Lint the new files**

Run: `uv run ruff check research/candidate_loop/governor/signals.py tests/unit/research/candidate_loop/test_governor_signals.py config/research/candidate_loop/governor_v1.yaml`
Expected: no errors (ruff ignores the yaml; it lints the .py files)

- [ ] **Step 9: Commit**

```bash
git add pyproject.toml config/research/candidate_loop/governor_v1.yaml \
  research/candidate_loop/governor/__init__.py \
  research/candidate_loop/governor/signals.py \
  tests/unit/research/candidate_loop/test_governor_signals.py
git commit -m "feat(governor): signals + focus classification from failure_summary

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

## Task 2: Steering brief render / parse (the human-editable artifact + gate)

**Files:**
- Create: `research/candidate_loop/governor/brief.py`
- Test: `tests/unit/research/candidate_loop/test_governor_brief.py`

- [ ] **Step 1: Write the failing test**

Create `tests/unit/research/candidate_loop/test_governor_brief.py`:

```python
"""Steering brief: deterministic render, round-trip parse, approval gate."""

from __future__ import annotations

from research.candidate_loop.governor.brief import is_approved, parse_brief, render_brief
from research.candidate_loop.governor.signals import SteeringSignals

FIXED_TS = "2026-06-14T00:00:00+00:00"


def _signals() -> SteeringSignals:
    return SteeringSignals(
        family="trade_flow",
        candidates=20,
        survival_rate=0.10,
        ic_p10=0.02,
        ic_p50=0.114,
        ic_p90=0.20,
        cost_failure_rate=0.55,
        maker_cost_failure_rate=0.40,
        maker_rescuable_count=2,
        duplicate_rate=0.05,
        reduced_day_coverage_count=7,
        near_misses=[{"alpha_id": "a1", "failed_gate": "cost_proxy_taker", "margin": -0.03}],
        common_failure_patterns=[],
    )


def _render() -> str:
    return render_brief(
        _signals(),
        focus="amplify",
        n_target=30,
        source_run_id="smoke_001",
        generated_at=FIXED_TS,
    )


def test_render_brief_defaults_to_unapproved():
    text = _render()
    assert "approved: false" in text
    assert is_approved(text) is False


def test_render_brief_is_byte_stable_for_fixed_timestamp():
    assert _render() == _render()


def test_parse_brief_round_trips_frontmatter_and_body():
    brief = parse_brief(_render())
    assert brief.family == "trade_flow"
    assert brief.source_run_id == "smoke_001"
    assert brief.focus == "amplify"
    assert brief.n_target == 30
    assert brief.approved is False
    assert "## Why" in brief.body
    assert "## Focus" in brief.body
    assert "## Avoid" in brief.body


def test_flipping_approved_true_is_detected():
    text = _render().replace("approved: false", "approved: true")
    assert is_approved(text) is True
    assert parse_brief(text).approved is True


def test_brief_body_surfaces_maker_rescue_and_near_miss():
    body = parse_brief(_render()).body
    assert "maker" in body.lower()
    assert "a1" in body  # the near-miss alpha_id
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `uv run pytest tests/unit/research/candidate_loop/test_governor_brief.py -q`
Expected: FAIL with `ModuleNotFoundError: No module named 'research.candidate_loop.governor.brief'`

- [ ] **Step 3: Implement `brief.py`**

Create `research/candidate_loop/governor/brief.py`:

```python
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
    body = text[end + 4 :].lstrip("\n")
    return ParsedBrief(
        family=str(fm.get("family", "")),
        source_run_id=str(fm.get("source_run_id", "")),
        approved=bool(fm.get("approved", False)),
        focus=str(fm.get("focus", "")),
        n_target=int(fm.get("n_target", 0)),
        signals=dict(fm.get("signals", {}) or {}),
        body=body,
    )


def is_approved(text: str) -> bool:
    return parse_brief(text).approved


__all__ = ["ParsedBrief", "is_approved", "parse_brief", "render_brief"]
```

- [ ] **Step 4: Run the test to verify it passes**

Run: `uv run pytest tests/unit/research/candidate_loop/test_governor_brief.py -q`
Expected: PASS (5 tests)

- [ ] **Step 5: Lint**

Run: `uv run ruff check research/candidate_loop/governor/brief.py tests/unit/research/candidate_loop/test_governor_brief.py`
Expected: no errors

- [ ] **Step 6: Commit**

```bash
git add research/candidate_loop/governor/brief.py \
  tests/unit/research/candidate_loop/test_governor_brief.py
git commit -m "feat(governor): steering brief render/parse with approval gate

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

## Task 3: DeepSeek client (fail-closed, redacting, no network in tests)

**Files:**
- Create: `research/candidate_loop/governor/client.py`
- Test: `tests/unit/research/candidate_loop/test_governor_client.py`

- [ ] **Step 1: Write the failing test**

Create `tests/unit/research/candidate_loop/test_governor_client.py`:

```python
"""DeepSeek client: fail-closed on missing key, JSONL extraction, redaction, retries."""

from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest

from research.candidate_loop.governor.client import DeepSeekClient, DeepSeekError
from research.candidate_loop.governor.signals import load_governor_config

CFG = load_governor_config(
    Path(__file__).resolve().parents[4]
    / "config" / "research" / "candidate_loop" / "governor_v1.yaml"
)


def _completion(content: str) -> dict:
    return {"choices": [{"message": {"content": content}}]}


def _two_candidate_content() -> str:
    return (
        '{"family":"trade_flow","name":"tf_a","formula":"x"}\n'
        '{"family":"trade_flow","name":"tf_b","formula":"y"}'
    )


def test_missing_api_key_fails_closed(monkeypatch):
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    with pytest.raises(DeepSeekError):
        DeepSeekClient(CFG)


def test_generate_candidates_extracts_jsonl_lines():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=_completion(_two_candidate_content()))

    client = DeepSeekClient(
        CFG, api_key="sk-test", transport=httpx.MockTransport(handler)
    )
    lines = client.generate_candidates(base_prompt="P", brief_body="B", n=2)
    assert len(lines) == 2
    assert json.loads(lines[0])["name"] == "tf_a"


def test_generate_candidates_strips_markdown_fences():
    content = "```json\n" + _two_candidate_content() + "\n```"

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=_completion(content))

    client = DeepSeekClient(
        CFG, api_key="sk-test", transport=httpx.MockTransport(handler)
    )
    assert len(client.generate_candidates(base_prompt="P", brief_body="B", n=2)) == 2


def test_non_json_line_fails_closed():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=_completion("not json at all"))

    client = DeepSeekClient(
        CFG, api_key="sk-test", transport=httpx.MockTransport(handler)
    )
    with pytest.raises(DeepSeekError):
        client.generate_candidates(base_prompt="P", brief_body="B", n=1)


def test_redact_removes_api_key_from_text():
    client = DeepSeekClient(
        CFG, api_key="sk-supersecret", transport=httpx.MockTransport(lambda r: httpx.Response(200))
    )
    assert "sk-supersecret" not in client.redact("boom sk-supersecret here")


def test_retries_then_succeeds_within_bound():
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] == 1:
            return httpx.Response(500, json={"error": "transient"})
        return httpx.Response(200, json=_completion(_two_candidate_content()))

    client = DeepSeekClient(
        CFG, api_key="sk-test", transport=httpx.MockTransport(handler)
    )
    lines = client.generate_candidates(base_prompt="P", brief_body="B", n=2)
    assert len(lines) == 2
    assert calls["n"] == 2  # one retry consumed
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `uv run pytest tests/unit/research/candidate_loop/test_governor_client.py -q`
Expected: FAIL with `ModuleNotFoundError: No module named 'research.candidate_loop.governor.client'`

- [ ] **Step 3: Implement `client.py`**

Create `research/candidate_loop/governor/client.py`:

```python
"""Thin DeepSeek client (OpenAI-compatible chat/completions).

Security: ``DEEPSEEK_API_KEY`` from the environment only; never logged or stored
beyond the in-memory client; redacted from any raised message; TLS verify on
(httpx default). Fails closed when the key is missing. Content validation of
candidates is deferred to the runner — the client only enforces the JSONL SHAPE
(one JSON object per line), matching ``generate.ingest_jsonl``.
"""

from __future__ import annotations

import json
import os
from typing import Any

import httpx

from research.candidate_loop.governor.signals import GovernorConfig

API_KEY_ENV = "DEEPSEEK_API_KEY"


class DeepSeekError(RuntimeError):
    """A DeepSeek call or its response violated the client contract."""


def _extract_jsonl(content: str) -> list[str]:
    lines: list[str] = []
    for raw in content.splitlines():
        raw = raw.strip()
        if not raw or raw.startswith("```"):
            continue
        try:
            obj = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise DeepSeekError(f"model returned a non-JSON line: {raw[:80]!r}") from exc
        if not isinstance(obj, dict):
            raise DeepSeekError(f"model line is not a JSON object: {raw[:80]!r}")
        lines.append(json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False))
    return lines


class DeepSeekClient:
    def __init__(
        self,
        cfg: GovernorConfig,
        *,
        api_key: str | None = None,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self._cfg = cfg
        self._api_key = api_key or os.environ.get(API_KEY_ENV, "")
        if not self._api_key:
            raise DeepSeekError(
                f"{API_KEY_ENV} not set; refusing to call DeepSeek unauthenticated"
            )
        self._client = httpx.Client(
            base_url=cfg.base_url,
            timeout=cfg.timeout_seconds,
            transport=transport,  # None → real network with TLS verify on
        )

    def redact(self, text: str) -> str:
        if self._api_key and self._api_key in text:
            return text.replace(self._api_key, "***")
        return text

    def _chat(self, base_prompt: str, brief_body: str, n: int) -> str:
        payload: dict[str, Any] = {
            "model": self._cfg.model_name,
            "messages": [
                {"role": "system", "content": base_prompt},
                {
                    "role": "user",
                    "content": f"{brief_body}\n\nEmit exactly {n} JSONL candidate lines now.",
                },
            ],
            "max_tokens": self._cfg.max_tokens,
            "temperature": self._cfg.temperature,
        }
        headers = {"Authorization": f"Bearer {self._api_key}"}
        last_exc: Exception | None = None
        for _ in range(self._cfg.max_retries + 1):
            try:
                resp = self._client.post("/chat/completions", json=payload, headers=headers)
                resp.raise_for_status()
                data = resp.json()
                return str(data["choices"][0]["message"]["content"])
            except (httpx.HTTPError, KeyError, IndexError) as exc:
                last_exc = exc
        raise DeepSeekError(self.redact(f"DeepSeek request failed: {last_exc}"))

    def generate_candidates(self, *, base_prompt: str, brief_body: str, n: int) -> list[str]:
        return _extract_jsonl(self._chat(base_prompt, brief_body, n))

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> "DeepSeekClient":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()


__all__ = ["API_KEY_ENV", "DeepSeekClient", "DeepSeekError"]
```

- [ ] **Step 4: Run the test to verify it passes**

Run: `uv run pytest tests/unit/research/candidate_loop/test_governor_client.py -q`
Expected: PASS (6 tests)

- [ ] **Step 5: Lint**

Run: `uv run ruff check research/candidate_loop/governor/client.py tests/unit/research/candidate_loop/test_governor_client.py`
Expected: no errors

- [ ] **Step 6: Commit**

```bash
git add research/candidate_loop/governor/client.py \
  tests/unit/research/candidate_loop/test_governor_client.py
git commit -m "feat(governor): fail-closed redacting DeepSeek client

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

## Task 4: Runner — draft (deterministic) + generate (gate + freeze + manifest)

**Files:**
- Create: `research/candidate_loop/governor/runner.py`
- Test: `tests/unit/research/candidate_loop/test_governor_runner.py`

- [ ] **Step 1: Write the failing test**

Create `tests/unit/research/candidate_loop/test_governor_runner.py`:

```python
"""Governor runner: draft briefs, enforce approval gate, freeze raw drop, manifest."""

from __future__ import annotations

import json
from pathlib import Path

from research.candidate_loop.governor.runner import draft_briefs, generate_from_briefs
from research.candidate_loop.governor.signals import load_governor_config

REPO = Path(__file__).resolve().parents[4]
CFG = load_governor_config(REPO / "config" / "research" / "candidate_loop" / "governor_v1.yaml")
PROMPTS = REPO / "research" / "candidate_loop" / "prompts" / "v1"
FIXED_TS = "2026-06-14T00:00:00+00:00"


def _summary() -> dict:
    return {
        "run_id": "smoke_001",
        "per_family": {
            "trade_flow": {
                "candidates": 20,
                "survival_rate": 0.10,
                "ic_distribution_survivors": {"p10": 0.0, "p50": 0.114, "p90": 0.2},
                "cost_failure_rate": 0.55,
                "maker_cost_failure_rate": 0.40,
                "maker_rescuable_count": 2,
                "duplicate_rate": 0.05,
                "reduced_day_coverage_count": 7,
                "near_misses": [],
                "common_failure_patterns": [],
            }
        },
    }


class _FakeClient:
    """Stand-in DeepSeekClient: records calls, returns canned JSONL."""

    def __init__(self) -> None:
        self.calls = 0

    def generate_candidates(self, *, base_prompt: str, brief_body: str, n: int) -> list[str]:
        self.calls += 1
        return [
            json.dumps({"family": "trade_flow", "name": f"tf_{i}", "formula": "x"}, sort_keys=True)
            for i in range(n)
        ]


def test_draft_briefs_writes_unapproved_per_family(tmp_path):
    summary_path = tmp_path / "failure_summary.json"
    summary_path.write_text(json.dumps(_summary()))
    out_dir = tmp_path / "steering"
    paths = draft_briefs(summary_path=summary_path, out_dir=out_dir, cfg=CFG, generated_at=FIXED_TS)
    assert [p.name for p in paths] == ["trade_flow.md"]
    assert "approved: false" in (out_dir / "trade_flow.md").read_text()


def test_generate_refuses_unapproved_brief(tmp_path):
    steering = tmp_path / "steering"
    steering.mkdir()
    summary_path = tmp_path / "fs.json"
    summary_path.write_text(json.dumps(_summary()))
    draft_briefs(summary_path=summary_path, out_dir=steering, cfg=CFG, generated_at=FIXED_TS)
    client = _FakeClient()
    candidates_root = tmp_path / "candidates"
    manifest = generate_from_briefs(
        steering_dir=steering,
        gen_run_id="gen_001",
        cfg=CFG,
        client=client,
        prompts_dir=PROMPTS,
        candidates_root=candidates_root,
        generated_at=FIXED_TS,
    )
    assert client.calls == 0
    assert manifest["skipped_unapproved"] == ["trade_flow"]
    assert manifest["families"] == {}


def test_generate_from_approved_brief_freezes_drop_and_writes_manifest(tmp_path):
    steering = tmp_path / "steering"
    steering.mkdir()
    summary_path = tmp_path / "fs.json"
    summary_path.write_text(json.dumps(_summary()))
    draft_briefs(summary_path=summary_path, out_dir=steering, cfg=CFG, generated_at=FIXED_TS)
    brief_path = steering / "trade_flow.md"
    brief_path.write_text(brief_path.read_text().replace("approved: false", "approved: true"))

    client = _FakeClient()
    candidates_root = tmp_path / "candidates"
    manifest = generate_from_briefs(
        steering_dir=steering,
        gen_run_id="gen_001",
        cfg=CFG,
        client=client,
        prompts_dir=PROMPTS,
        candidates_root=candidates_root,
        generated_at=FIXED_TS,
    )
    assert client.calls == 1
    fam = manifest["families"]["trade_flow"]
    assert fam["focus"] == "amplify"
    assert fam["model"] == "deepseek-chat"
    assert len(fam["steering_sha256"]) == 64
    # raw drop frozen + family file written through the existing generate path
    assert (candidates_root / "gen_001" / "_governor_raw" / "trade_flow.jsonl").exists()
    family_file = candidates_root / "gen_001" / "family=trade_flow.jsonl"
    assert family_file.exists()
    header = json.loads(family_file.read_text().splitlines()[0])
    assert header["generation_model"] == "deepseek-chat"
    assert (candidates_root / "gen_001" / "governor_manifest.json").exists()


def test_generate_is_idempotent_reuses_frozen_drop(tmp_path):
    steering = tmp_path / "steering"
    steering.mkdir()
    summary_path = tmp_path / "fs.json"
    summary_path.write_text(json.dumps(_summary()))
    draft_briefs(summary_path=summary_path, out_dir=steering, cfg=CFG, generated_at=FIXED_TS)
    brief_path = steering / "trade_flow.md"
    brief_path.write_text(brief_path.read_text().replace("approved: false", "approved: true"))

    candidates_root = tmp_path / "candidates"
    kwargs = dict(
        steering_dir=steering,
        gen_run_id="gen_001",
        cfg=CFG,
        prompts_dir=PROMPTS,
        candidates_root=candidates_root,
        generated_at=FIXED_TS,
    )
    first = _FakeClient()
    generate_from_briefs(client=first, **kwargs)
    second = _FakeClient()
    manifest = generate_from_briefs(client=second, **kwargs)
    assert first.calls == 1
    assert second.calls == 0  # reused frozen drop
    assert manifest["families"]["trade_flow"]["reused"] is True
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `uv run pytest tests/unit/research/candidate_loop/test_governor_runner.py -q`
Expected: FAIL with `ModuleNotFoundError: No module named 'research.candidate_loop.governor.runner'`

- [ ] **Step 3: Implement `runner.py`**

Create `research/candidate_loop/governor/runner.py`:

```python
"""Governor orchestration: deterministic `draft`, gated `generate`.

`draft_briefs` is pure-deterministic (summary → briefs). `generate_from_briefs`
enforces the per-family approval gate, freezes the non-deterministic LLM drop as
an artifact (so re-runs reuse it instead of re-calling DeepSeek), hands it to the
existing `generate_drop`, and records a `governor_manifest.json` provenance
sidecar. Nothing here touches the frozen scored path.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any, Protocol

from research.candidate_loop.generate import generate_drop
from research.candidate_loop.governor.brief import parse_brief, render_brief
from research.candidate_loop.governor.signals import (
    GovernorConfig,
    classify_focus,
    extract_signals,
    n_target_for,
)


class _Client(Protocol):
    def generate_candidates(self, *, base_prompt: str, brief_body: str, n: int) -> list[str]: ...


def draft_briefs(
    *,
    summary_path: Path,
    out_dir: Path,
    cfg: GovernorConfig,
    generated_at: str,
) -> list[Path]:
    summary = json.loads(summary_path.read_text())
    source_run_id = str(summary.get("run_id", ""))
    out_dir.mkdir(parents=True, exist_ok=True)
    written: list[Path] = []
    for family in sorted(summary.get("per_family", {})):
        signals = extract_signals(summary, family)
        focus = classify_focus(signals, cfg)
        text = render_brief(
            signals,
            focus=focus,
            n_target=n_target_for(focus, cfg),
            source_run_id=source_run_id,
            generated_at=generated_at,
        )
        path = out_dir / f"{family}.md"
        path.write_text(text, encoding="utf-8")
        written.append(path)
    return written


def generate_from_briefs(
    *,
    steering_dir: Path,
    gen_run_id: str,
    cfg: GovernorConfig,
    client: _Client,
    prompts_dir: Path,
    candidates_root: Path,
    generated_at: str,
) -> dict[str, Any]:
    manifest: dict[str, Any] = {
        "gen_run_id": gen_run_id,
        "governor_version": cfg.governor_version,
        "model": cfg.model_name,
        "generated_at": generated_at,
        "families": {},
        "skipped_unapproved": [],
    }
    raw_root = candidates_root / gen_run_id / "_governor_raw"
    for brief_path in sorted(steering_dir.glob("*.md")):
        text = brief_path.read_text(encoding="utf-8")
        brief = parse_brief(text)
        if not brief.approved:
            manifest["skipped_unapproved"].append(brief.family)
            continue
        base_prompt = (prompts_dir / f"{brief.family}.md").read_text(encoding="utf-8")
        raw_path = raw_root / f"{brief.family}.jsonl"
        reused = raw_path.exists()
        if reused:
            lines = [ln for ln in raw_path.read_text(encoding="utf-8").splitlines() if ln.strip()]
        else:
            lines = client.generate_candidates(
                base_prompt=base_prompt, brief_body=brief.body, n=brief.n_target
            )
            raw_path.parent.mkdir(parents=True, exist_ok=True)
            raw_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        family_file = generate_drop(
            gen_run_id=gen_run_id,
            family=brief.family,
            prompt_path=prompts_dir / f"{brief.family}.md",
            from_jsonl=raw_path,
            generation_model=cfg.model_name,
            generated_at=generated_at,
            candidates_root=candidates_root,
        )
        manifest["families"][brief.family] = {
            "source_run_id": brief.source_run_id,
            "steering_path": str(brief_path),
            "steering_sha256": hashlib.sha256(text.encode("utf-8")).hexdigest(),
            "focus": brief.focus,
            "n_target": brief.n_target,
            "model": cfg.model_name,
            "raw_drop": str(raw_path),
            "family_file": str(family_file),
            "reused": reused,
            "generated_at": generated_at,
        }
    manifest["skipped_unapproved"] = sorted(manifest["skipped_unapproved"])
    manifest_path = candidates_root / gen_run_id / "governor_manifest.json"
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    manifest_path.write_text(json.dumps(manifest, indent=2, sort_keys=True))
    return manifest


__all__ = ["draft_briefs", "generate_from_briefs"]
```

- [ ] **Step 4: Run the test to verify it passes**

Run: `uv run pytest tests/unit/research/candidate_loop/test_governor_runner.py -q`
Expected: PASS (4 tests)

- [ ] **Step 5: Lint**

Run: `uv run ruff check research/candidate_loop/governor/runner.py tests/unit/research/candidate_loop/test_governor_runner.py`
Expected: no errors

- [ ] **Step 6: Commit**

```bash
git add research/candidate_loop/governor/runner.py \
  tests/unit/research/candidate_loop/test_governor_runner.py
git commit -m "feat(governor): draft + gated generate with frozen drop + manifest

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

## Task 5: CLI — `governor draft` / `governor generate`

**Files:**
- Modify: `research/candidate_loop/__main__.py`
- Test: `tests/unit/research/candidate_loop/test_governor_cli.py`

- [ ] **Step 1: Write the failing test**

Create `tests/unit/research/candidate_loop/test_governor_cli.py`:

```python
"""Governor CLI: `draft` writes unapproved briefs; `generate` is wired + fail-closed."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from research.candidate_loop.__main__ import main


def _summary() -> dict:
    return {
        "run_id": "smoke_001",
        "per_family": {
            "trade_flow": {
                "candidates": 20,
                "survival_rate": 0.10,
                "ic_distribution_survivors": {"p10": 0.0, "p50": 0.114, "p90": 0.2},
                "cost_failure_rate": 0.55,
                "maker_cost_failure_rate": 0.40,
                "maker_rescuable_count": 2,
                "duplicate_rate": 0.05,
                "reduced_day_coverage_count": 7,
                "near_misses": [],
                "common_failure_patterns": [],
            }
        },
    }


def test_governor_draft_writes_unapproved_briefs(tmp_path, capsys):
    runs_root = tmp_path / "runs"
    (runs_root / "smoke_001").mkdir(parents=True)
    (runs_root / "smoke_001" / "failure_summary.json").write_text(json.dumps(_summary()))
    rc = main(
        [
            "governor",
            "draft",
            "--from-run",
            "smoke_001",
            "--runs-root",
            str(runs_root),
        ]
    )
    assert rc == 0
    brief = runs_root / "smoke_001" / "steering" / "trade_flow.md"
    assert "approved: false" in brief.read_text()


def test_governor_draft_missing_summary_returns_error(tmp_path):
    rc = main(["governor", "draft", "--from-run", "nope", "--runs-root", str(tmp_path)])
    assert rc == 2


def test_governor_generate_fails_closed_without_api_key(tmp_path, monkeypatch):
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    steering = tmp_path / "steering"
    steering.mkdir()
    with pytest.raises(SystemExit):
        main(
            [
                "governor",
                "generate",
                "--steering",
                str(steering),
                "--gen-run",
                "gen_001",
                "--candidates-root",
                str(tmp_path / "candidates"),
            ]
        )
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `uv run pytest tests/unit/research/candidate_loop/test_governor_cli.py -q`
Expected: FAIL — `argument command: invalid choice: 'governor'`

- [ ] **Step 3: Add the governor subcommand to `__main__.py`**

In `research/candidate_loop/__main__.py`, update the imports near the top so the prompts/config defaults are available:

```python
from research.candidate_loop.generate import (
    DEFAULT_CANDIDATES_ROOT,
    DEFAULT_PROMPTS_DIR,
    generate_drop,
)
from research.candidate_loop.runner import DEFAULT_CONFIG_DIR, DEFAULT_RUNS_ROOT, RunConfig, run_batch
```

Add these two handlers (place them after `_cmd_replay_fallback`):

```python
def _cmd_governor_draft(args: argparse.Namespace) -> int:
    from research.candidate_loop.governor.runner import draft_briefs
    from research.candidate_loop.governor.signals import load_governor_config

    summary_path = Path(args.runs_root) / args.from_run / "failure_summary.json"
    if not summary_path.exists():
        print(f"ERROR: {summary_path} not found (run the prior batch first)", file=sys.stderr)
        return 2
    cfg = load_governor_config(Path(args.governor_config))
    out_dir = Path(args.out) if args.out else Path(args.runs_root) / args.from_run / "steering"
    paths = draft_briefs(
        summary_path=summary_path,
        out_dir=out_dir,
        cfg=cfg,
        generated_at=datetime.now(timezone.utc).isoformat(),
    )
    for path in paths:
        print(f"drafted {path}  (approved: false — edit, then flip to true to authorize)")
    return 0


def _cmd_governor_generate(args: argparse.Namespace) -> int:
    from research.candidate_loop.governor.client import DeepSeekClient, DeepSeekError
    from research.candidate_loop.governor.runner import generate_from_briefs
    from research.candidate_loop.governor.signals import load_governor_config

    cfg = load_governor_config(Path(args.governor_config))
    try:
        client = DeepSeekClient(cfg)  # reads DEEPSEEK_API_KEY; fail-closed
    except DeepSeekError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        raise SystemExit(2) from exc
    try:
        manifest = generate_from_briefs(
            steering_dir=Path(args.steering),
            gen_run_id=args.gen_run,
            cfg=cfg,
            client=client,
            prompts_dir=Path(args.prompts_dir),
            candidates_root=Path(args.candidates_root),
            generated_at=datetime.now(timezone.utc).isoformat(),
        )
    finally:
        client.close()
    print(
        json.dumps(
            {
                "generated": sorted(manifest["families"]),
                "skipped_unapproved": manifest["skipped_unapproved"],
            },
            indent=2,
        )
    )
    return 0
```

In `build_parser()`, after the `replay` subparser, add the nested `governor` subcommand:

```python
    governor = sub.add_parser("governor", help="v1.1 governor: steer next-round generation")
    gov_sub = governor.add_subparsers(dest="gov_command", required=True)

    gov_draft = gov_sub.add_parser("draft", help="draft per-family steering briefs from a prior run")
    gov_draft.add_argument("--from-run", required=True, help="prior run id under runs-root")
    gov_draft.add_argument("--out", default=None, help="output dir (default runs/<from-run>/steering)")
    gov_draft.add_argument("--runs-root", default=str(DEFAULT_RUNS_ROOT))
    gov_draft.add_argument(
        "--governor-config", default=str(DEFAULT_CONFIG_DIR / "governor_v1.yaml")
    )

    gov_gen = gov_sub.add_parser("generate", help="generate candidates from APPROVED briefs (DeepSeek)")
    gov_gen.add_argument("--steering", required=True, help="dir of approved <family>.md briefs")
    gov_gen.add_argument("--gen-run", required=True, help="generation run id (candidates/<gen-run>/)")
    gov_gen.add_argument("--prompts-dir", default=str(DEFAULT_PROMPTS_DIR))
    gov_gen.add_argument("--candidates-root", default=str(DEFAULT_CANDIDATES_ROOT))
    gov_gen.add_argument(
        "--governor-config", default=str(DEFAULT_CONFIG_DIR / "governor_v1.yaml")
    )
```

Update `main()` to dispatch the nested subcommand:

```python
def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.command == "governor":
        gov_handlers = {
            "draft": _cmd_governor_draft,
            "generate": _cmd_governor_generate,
        }
        return gov_handlers[args.gov_command](args)
    handlers = {
        "generate": _cmd_generate,
        "run": _cmd_run,
        "summarize": _cmd_summarize,
        "promote": _cmd_promote,
        "replay-fallback": _cmd_replay_fallback,
    }
    return handlers[args.command](args)
```

- [ ] **Step 4: Run the test to verify it passes**

Run: `uv run pytest tests/unit/research/candidate_loop/test_governor_cli.py -q`
Expected: PASS (3 tests)

- [ ] **Step 5: Confirm the existing CLI test still passes (no regression in the parser)**

Run: `uv run pytest tests/unit/research/candidate_loop/test_cli.py -q`
Expected: PASS (unchanged)

- [ ] **Step 6: Lint**

Run: `uv run ruff check research/candidate_loop/__main__.py tests/unit/research/candidate_loop/test_governor_cli.py`
Expected: no errors

- [ ] **Step 7: Commit**

```bash
git add research/candidate_loop/__main__.py \
  tests/unit/research/candidate_loop/test_governor_cli.py
git commit -m "feat(governor): CLI governor draft/generate subcommands

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

## Task 6: Package re-exports + full-suite verification

**Files:**
- Modify: `research/candidate_loop/governor/__init__.py`

- [ ] **Step 1: Add public re-exports to the package init**

Append to `research/candidate_loop/governor/__init__.py`:

```python
from research.candidate_loop.governor.brief import (
    ParsedBrief,
    is_approved,
    parse_brief,
    render_brief,
)
from research.candidate_loop.governor.client import DeepSeekClient, DeepSeekError
from research.candidate_loop.governor.runner import draft_briefs, generate_from_briefs
from research.candidate_loop.governor.signals import (
    GovernorConfig,
    SteeringSignals,
    classify_focus,
    extract_signals,
    load_governor_config,
    n_target_for,
)

__all__ = [
    "DeepSeekClient",
    "DeepSeekError",
    "GovernorConfig",
    "ParsedBrief",
    "SteeringSignals",
    "classify_focus",
    "draft_briefs",
    "extract_signals",
    "generate_from_briefs",
    "is_approved",
    "load_governor_config",
    "n_target_for",
    "parse_brief",
    "render_brief",
]
```

- [ ] **Step 2: Run the full governor + candidate-loop suite**

Run: `uv run pytest tests/unit/research/candidate_loop/ -q`
Expected: PASS — all pre-existing candidate-loop tests plus the 5 new governor test files (25 new tests: 7 signals + 5 brief + 6 client + 4 runner + 3 cli).

- [ ] **Step 3: Lint the whole governor package + touched files**

Run: `uv run ruff check research/candidate_loop/ tests/unit/research/candidate_loop/`
Expected: no errors

- [ ] **Step 4: Verify import surface + CLI help end-to-end (no network)**

Run:
```bash
uv run python -c "import research.candidate_loop.governor as g; print(sorted(g.__all__))"
uv run python -m research.candidate_loop governor --help
uv run python -m research.candidate_loop governor draft --help
```
Expected: the `__all__` list prints; both `--help` outputs render the new subcommands without error.

- [ ] **Step 5: Commit**

```bash
git add research/candidate_loop/governor/__init__.py
git commit -m "feat(governor): public re-exports + v1.1 governor complete

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

## Verification Summary

After all tasks:
- `uv run pytest tests/unit/research/candidate_loop/ -q` — green (25 new governor tests + all pre-existing).
- `uv run ruff check research/candidate_loop/ tests/unit/research/candidate_loop/` — clean.
- `git log --oneline` shows 6 focused commits, each test-backed.
- No `src/` import of the governor; no frozen-version/gate/schema change; live registry untouched.
- `DEEPSEEK_API_KEY` referenced only from the environment, redacted, fail-closed — never in code, args, or logs.

## Out of scope (tracked, not silently dropped)

- **FDR / OOS confirmation** — steered generation is gentler on multiple-testing than brute force, but no false-discovery or holdout layer is added here. Remains the known v1.2 gap before any promotion is trustworthy.
- **LLM-drafted briefs** — briefs are deterministic in v1.1; LLM prose is a future toggle.
- **Auto-approval** — the human gate is mandatory; no flag bypasses it.
- **Live `.env` integration test against the real DeepSeek API** — the suite uses `httpx.MockTransport`; a one-off authenticated smoke run (`governor generate` against a real approved brief) is a manual post-merge step the operator runs with the key in `.env`.

## Manual post-merge smoke (operator-run, requires `DEEPSEEK_API_KEY` in `.env`)

```bash
# from a finished run that produced runs/<run>/failure_summary.json:
uv run python -m research.candidate_loop governor draft --from-run <run>
#   → edit runs/<run>/steering/<family>.md: refine prose, set approved: true
uv run python -m research.candidate_loop governor generate \
  --steering research/candidate_loop/runs/<run>/steering --gen-run <gen_run>
uv run python -m research.candidate_loop run --batch <gen_run>
```
