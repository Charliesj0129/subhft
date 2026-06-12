"""Deterministic 6x20 smoke batch expander (plan step 13, no-LLM fallback).

Expands fixed parameter grids into 20 candidates per family (120 total),
asserts every one validates under the frozen validator (and that all formula
hashes are unique batch-wide) BEFORE writing, then writes headered family
files via the §11 generate path with ``generation_model="template_v1"``.

Grids deliberately avoid the exact (signal, regime, horizon) combos in
``fixtures/validator_matrix_12.jsonl`` so running the fixture batch into the
same ClickHouse first cannot DUPLICATE_ALPHA-kill smoke candidates
(regression-tested in test_make_smoke_batch.py).

Run: ``uv run python -m research.candidate_loop.tools.make_smoke_batch --run-id smoke_001``
"""

from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from research.candidate_loop.generate import (
    DEFAULT_CANDIDATES_ROOT,
    DEFAULT_PROMPTS_DIR,
    build_header,
    family_jsonl_path,
    write_family_jsonl,
)
from research.candidate_loop.validator import ValidCandidate, validate_batch

GENERATION_MODEL = "template_v1"
PER_FAMILY = 20


def _cand(
    name: str,
    family: str,
    hypothesis: str,
    features: list[dict[str, str]],
    signal: str,
    horizon: str,
    expected_sign: str = "positive",
    regime_filter: str = "",
) -> dict[str, Any]:
    return {
        "name": name,
        "family": family,
        "hypothesis": hypothesis,
        "features": features,
        "signal_formula": signal,
        "label": f"future_mid_return(horizon='{horizon}')",
        "horizon": horizon,
        "expected_sign": expected_sign,
        "regime_filter": regime_filter,
    }


def _order_book_imbalance() -> list[dict[str, Any]]:
    out = []
    for levels in (1, 2, 3, 4, 5):
        for window in (500, 2000):
            for horizon in ("500ms", "2s"):
                out.append(
                    _cand(
                        name=f"obi_l{levels}_w{window}_h{horizon}",
                        family="order_book_imbalance",
                        hypothesis=(
                            f"{levels}-level book imbalance normalized over {window} events "
                            f"predicts the mid move over {horizon}."
                        ),
                        features=[{"name": "obi", "formula": f"book_imbalance({levels})"}],
                        signal=f"zscore(obi, '{window}_events')",
                        horizon=horizon,
                    )
                )
    return out


def _microprice() -> list[dict[str, Any]]:
    norms = {
        "raw": "microprice() - mid_price()",
        "spr": "(microprice() - mid_price()) / (spread_ticks() + 1)",
    }
    out = []
    # raw norm at window 2000 would collide with the fixture's
    # microprice_dev_pull; 2000 is excluded from the grid entirely.
    for norm_key, formula in norms.items():
        for window in (500, 1000, 2500, 5000, 10000):
            for horizon in ("500ms", "1s"):
                out.append(
                    _cand(
                        name=f"mp_{norm_key}_w{window}_h{horizon}",
                        family="microprice",
                        hypothesis=(
                            f"Microprice displacement ({norm_key}) z-scored over {window} "
                            f"events resolves into the mid within {horizon}."
                        ),
                        features=[{"name": "mp_disp", "formula": formula}],
                        signal=f"zscore(mp_disp, '{window}_events')",
                        horizon=horizon,
                    )
                )
    return out


def _depth_delta() -> list[dict[str, Any]]:
    out = []
    for levels in (1, 2, 3, 4, 5):
        for dwin in ("200ms", "500ms"):
            for horizon in ("1s", "2s"):
                out.append(
                    _cand(
                        name=f"dd_l{levels}_d{dwin}_h{horizon}",
                        family="depth_delta",
                        hypothesis=(
                            f"Bid-vs-ask depth change at {levels} levels over {dwin} "
                            f"marks one-sided passive pressure resolving within {horizon}."
                        ),
                        features=[
                            {
                                "name": "dd_diff",
                                "formula": (
                                    f"depth_delta('bid', {levels}, '{dwin}') - "
                                    f"depth_delta('ask', {levels}, '{dwin}')"
                                ),
                            }
                        ],
                        signal="ema(dd_diff, '2s')",
                        horizon=horizon,
                    )
                )
    return out


def _trade_flow() -> list[dict[str, Any]]:
    out = []
    for tw in ("500ms", "1s", "2s", "5s", "10s"):
        for zwin in (2000, 8000):
            for horizon in ("1s", "2s"):
                out.append(
                    _cand(
                        name=f"tf_t{tw}_w{zwin}_h{horizon}",
                        family="trade_flow",
                        hypothesis=(
                            f"Signed trade flow over {tw} normalized across {zwin} events "
                            f"continues into the mid over {horizon}."
                        ),
                        features=[{"name": "tf_imb", "formula": f"trade_imbalance('{tw}')"}],
                        signal=f"zscore(tf_imb, '{zwin}_events')",
                        horizon=horizon,
                    )
                )
    return out


def _spread_regime() -> list[dict[str, Any]]:
    out = []
    # zscore window 3000 avoids the fixture's tight_spread_obi_regime (2000).
    for levels in (1, 2, 3, 4, 5):
        for threshold in (1, 2):
            for horizon in ("500ms", "1s"):
                out.append(
                    _cand(
                        name=f"sr_l{levels}_t{threshold}_h{horizon}",
                        family="spread_regime",
                        hypothesis=(
                            f"{levels}-level imbalance carries edge only when the spread is "
                            f"at most {threshold} ticks, resolving within {horizon}."
                        ),
                        features=[{"name": "obi", "formula": f"book_imbalance({levels})"}],
                        signal="zscore(obi, '3000_events')",
                        horizon=horizon,
                        regime_filter=f"spread_ticks() <= {threshold}",
                    )
                )
    return out


def _replenishment() -> list[dict[str, Any]]:
    out = []
    for side, sign in (("bid", "positive"), ("ask", "negative")):
        for levels in (1, 2, 3, 4, 5):
            for horizon in ("1s", "2s"):
                out.append(
                    _cand(
                        name=f"rep_{side}_l{levels}_h{horizon}",
                        family="replenishment",
                        hypothesis=(
                            f"Fast {side}-side replenishment at {levels} levels signals "
                            f"committed passive interest pushing the mid within {horizon}."
                        ),
                        features=[
                            {
                                "name": "rep",
                                "formula": f"ema(depth_delta('{side}', {levels}, '200ms'), '2s')",
                            }
                        ],
                        signal="zscore(rep, '4000_events')",
                        horizon=horizon,
                        expected_sign=sign,
                    )
                )
    return out


def build_candidates() -> dict[str, list[dict[str, Any]]]:
    families = {
        "order_book_imbalance": _order_book_imbalance(),
        "microprice": _microprice(),
        "depth_delta": _depth_delta(),
        "trade_flow": _trade_flow(),
        "spread_regime": _spread_regime(),
        "replenishment": _replenishment(),
    }
    for family, cands in families.items():
        if len(cands) != PER_FAMILY:
            raise AssertionError(f"{family}: expected {PER_FAMILY} candidates, got {len(cands)}")
    return families


def _assert_all_valid(families: dict[str, list[dict[str, Any]]]) -> None:
    lines = [
        json.dumps(c, sort_keys=True, separators=(",", ":"))
        for family in sorted(families)
        for c in families[family]
    ]
    results = validate_batch(lines)
    bad = [r for r in results if not isinstance(r, ValidCandidate)]
    if bad:
        details = "; ".join(f"{b.death_reason.value}: {b.detail}" for b in bad[:5])
        raise AssertionError(f"{len(bad)} smoke candidates failed validation: {details}")
    hashes = {r.formula_hash for r in results if isinstance(r, ValidCandidate)}
    if len(hashes) != len(results):
        raise AssertionError("smoke batch contains duplicate formula hashes")


def write_smoke_batch(
    run_id: str,
    candidates_root: Path = DEFAULT_CANDIDATES_ROOT,
    prompts_dir: Path = DEFAULT_PROMPTS_DIR,
    generated_at: str | None = None,
) -> list[Path]:
    families = build_candidates()
    _assert_all_valid(families)
    stamp = generated_at or datetime.now(timezone.utc).isoformat()
    written: list[Path] = []
    for family in sorted(families):
        header = build_header(
            prompt_path=prompts_dir / f"{family}.md",
            generation_model=GENERATION_MODEL,
            generation_run_id=run_id,
            generated_at=stamp,
        )
        lines = [
            json.dumps(c, sort_keys=True, separators=(",", ":")) for c in families[family]
        ]
        written.append(
            write_family_jsonl(family_jsonl_path(candidates_root, run_id, family), header, lines)
        )
    return written


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="make_smoke_batch")
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--candidates-root", default=str(DEFAULT_CANDIDATES_ROOT))
    parser.add_argument("--prompts-dir", default=str(DEFAULT_PROMPTS_DIR))
    args = parser.parse_args(argv)
    paths = write_smoke_batch(
        args.run_id,
        candidates_root=Path(args.candidates_root),
        prompts_dir=Path(args.prompts_dir),
    )
    for path in paths:
        print(f"wrote {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
