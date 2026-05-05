"""Slice-D Tasks 17-19 — alpha-factory MVP integration tests.

End-to-end checks that exercise the public alpha-factory entry points
(``cheap_screen``, ``cluster_alphas``, ``promote_alpha``) against real
on-disk artifacts and assert the DoD evidence required by the plan:

* T17 / DoD-D1 — the cheap screener completes for every manifest-bearing
  alpha within 60 s wall-clock, with the 95th-percentile under budget.
* T18 / DoD-D3 — clustering on the deterministic Slice-D fixture corpus
  groups ``r47_maker_pivot`` with at least one of its latent-factor
  siblings, and rejects an empty corpus with ``EmptyCorpusError``.
* T19 / DoD-D4 — ``promote_alpha`` writes a kill-ledger row when Gate C
  raises and when Gate D rejects, idempotently on retry.

These tests intentionally drive ``promote_alpha`` end-to-end (no Gate
function monkeypatching) so the integration boundary is validated. The
deeper unit-level contract tests live under
``tests/unit/test_alpha_promotion.py::TestSliceDAutoKill`` (T14).
"""
from __future__ import annotations

import json
import time
from pathlib import Path

import pytest
import yaml

from hft_platform.alpha.cluster import EmptyCorpusError, cluster_alphas
from hft_platform.alpha.promotion import PromotionConfig, promote_alpha
from hft_platform.alpha.screener import BUDGET_S, cheap_screen


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _repo_root() -> Path:
    """tests/integration/<file> -> repo root is two parents up."""
    return Path(__file__).resolve().parents[2]


def _enumerate_manifest_bearing_alphas(repo_root: Path) -> list[str]:
    """Lex-sorted alpha ids under ``research/alphas/`` that carry a manifest.yaml.

    Hidden / underscore-prefixed entries (``_templates``, ``__init__.py``,
    ``_kill_ledger.jsonl``) are filtered out so we only enumerate the real
    alphas the factory promotes.
    """
    alphas_dir = repo_root / "research" / "alphas"
    return sorted(
        d.name
        for d in alphas_dir.iterdir()
        if d.is_dir() and (d / "manifest.yaml").exists() and not d.name.startswith("_")
    )


def _strict_profile():
    """Minimal strict ValidationProfile for promote_alpha entry."""
    from hft_platform.alpha._validation_profile import ValidationProfile

    return ValidationProfile(
        name="t19_integration",
        is_strict=True,
        thresholds={},
        blocking_sub_gates=("sharpe_threshold",),
    )


def _write_scorecard(
    path: Path,
    *,
    sharpe: float,
    max_drawdown: float = -0.05,
    turnover: float = 0.5,
    corr: float = 0.3,
    latency_profile: str = "sim_p95_v2026-02-26",
    replay_parity_match_pct: float = 100.0,
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload: dict = {
        "sharpe_oos": sharpe,
        "max_drawdown": max_drawdown,
        "turnover": turnover,
        "correlation_pool_max": corr,
        "latency_profile": latency_profile,
        "replay_parity": {"match_pct": replay_parity_match_pct},
    }
    path.write_text(json.dumps(payload, indent=2, sort_keys=True))


def _setup_alpha_fixture(
    tmp_path: Path,
    alpha_id: str,
    *,
    gate_c_passed: bool,
    sharpe: float = 1.6,
) -> tuple[Path, Path]:
    """Lay down a synthetic ``research/alphas/<alpha_id>/`` fixture.

    Mirrors the layout used by ``TestSliceDAutoKill`` (T14 unit suite) so
    promote_alpha can run with a real on-disk scorecard + meta + manifest.

    Returns
    -------
    (project_root, scorecard_path)
    """
    alpha_dir = tmp_path / "research" / "alphas" / alpha_id
    alpha_dir.mkdir(parents=True, exist_ok=True)

    scorecard_path = alpha_dir / "scorecard.json"
    _write_scorecard(scorecard_path, sharpe=sharpe)

    (alpha_dir / "meta.json").write_text(
        json.dumps({"gate_status": {"gate_c": gate_c_passed}})
    )

    (alpha_dir / "manifest.yaml").write_text(
        yaml.safe_dump(
            {
                "alpha_id": alpha_id,
                "hypothesis": "synthetic integration-test alpha",
                "formula": "x",
                "paper_refs": ["1234.5678"],
                "data_fields": ["feature[0]"],
                "complexity": "O(1)",
                "status": "draft",
            }
        )
    )
    return tmp_path, scorecard_path


def _read_ledger(path: Path) -> list[dict]:
    if not path.exists():
        return []
    rows: list[dict] = []
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line:
            continue
        rows.append(json.loads(line))
    return rows


# ---------------------------------------------------------------------------
# Shared isolation fixture for the T19 (Gate C/D auto-kill) tests.
# ---------------------------------------------------------------------------


@pytest.fixture
def isolated_kill_ledger(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> Path:
    """Redirect kill-ledger writes into ``tmp_path`` and reset cache.

    Mirrors ``TestSliceDAutoKill._isolate_kill_ledger`` from the T14 unit
    suite. Audit is forced off so writes land in the jsonl sink we read
    back from disk.
    """
    from hft_platform.alpha import audit, kill_ledger

    jsonl = tmp_path / "_kill_ledger.jsonl"
    monkeypatch.setenv("HFT_ALPHA_KILL_LEDGER_PATH", str(jsonl))
    monkeypatch.setenv("HFT_ALPHA_AUDIT_ENABLED", "0")
    monkeypatch.setenv("HFT_KILL_LEDGER_ENABLED", "1")
    audit._ENABLED = None  # noqa: SLF001 -- re-read env on next call
    kill_ledger._reset_cache_for_tests()
    return jsonl


# ---------------------------------------------------------------------------
# T17 — DoD-D1: cheap screener finishes for all 15 alphas within 60 s.
# ---------------------------------------------------------------------------


def test_dod_d1_screen_15_alphas() -> None:
    """DoD-D1: every manifest-bearing alpha screens in < 60 s wall-clock.

    Most alphas have no committed signals at the production path, so they
    return ``verdict='unknown'`` quickly via the screener's fast-fail
    branch. The DoD is the timing contract — the verdict mix is incidental.
    """
    repo_root = _repo_root()
    alpha_ids = _enumerate_manifest_bearing_alphas(repo_root)
    assert len(alpha_ids) >= 15, (
        f"DoD-D1 requires >= 15 manifest-bearing alphas, found {len(alpha_ids)}"
    )

    durations: list[tuple[str, float, str]] = []
    for alpha_id in alpha_ids:
        t0 = time.monotonic()
        result = cheap_screen(alpha_id, project_root=repo_root)
        dt = time.monotonic() - t0
        durations.append((alpha_id, dt, result.verdict))
        # Per-alpha hard budget.
        assert dt < BUDGET_S, (
            f"alpha {alpha_id} took {dt:.2f}s (>= {BUDGET_S:.0f}s budget)"
        )
        # Closed verdict domain.
        assert result.verdict in {"pass", "kill", "unknown"}, (
            f"alpha {alpha_id} returned unexpected verdict {result.verdict!r}"
        )

    # 95th-percentile sanity check. With no committed signals the screener
    # short-circuits in well under a millisecond; this guards against a
    # regression where one alpha silently drags the budget close to the
    # ceiling.
    times = sorted(dt for _, dt, _ in durations)
    p95_idx = max(0, int(0.95 * len(times)) - 1)
    p95 = times[p95_idx]
    max_dur = max(dt for _, dt, _ in durations)
    assert p95 < BUDGET_S, (
        f"P95 {p95:.2f}s exceeds {BUDGET_S:.0f}s budget; "
        f"max={max_dur:.4f}s; details={durations}"
    )


# ---------------------------------------------------------------------------
# T18 — DoD-D3: cluster pair detection finds the R47 family.
# ---------------------------------------------------------------------------


def test_dod_d3_cluster_finds_r47_family() -> None:
    """DoD-D3: the deterministic T7b corpus groups r47_maker_pivot with
    at least one of its latent-factor siblings under rho >= 0.7."""
    assignments = cluster_alphas(
        base_dir="research/experiments/_slice_d_fixtures",
        threshold=0.7,
        metric="pearson",
        write_artifact=False,
    )
    by_cluster: dict[str, set[str]] = {}
    for a in assignments:
        by_cluster.setdefault(a.cluster_id, set()).add(a.alpha_id)

    siblings = {
        "c60_tmfd6_r47_minimal_inst_rt",
        "c63_txfd6_r47_tight_spread",
        "c72_tmfd6_queue_position_aware",
    }
    found = False
    matched_siblings: set[str] = set()
    for members in by_cluster.values():
        if "r47_maker_pivot" in members and members & siblings:
            found = True
            matched_siblings = members & siblings
            break
    assert found, (
        f"R47 cluster not detected with r47_maker_pivot + any of {sorted(siblings)}; "
        f"clusters: {by_cluster}"
    )
    # Surface which siblings landed in the cluster — useful when this
    # assertion drifts after corpus regeneration.
    assert matched_siblings, "matched siblings unexpectedly empty"


def test_dod_d3_empty_corpus_raises(tmp_path: Path) -> None:
    """DoD-D3 negative: empty base_dir raises EmptyCorpusError."""
    with pytest.raises(EmptyCorpusError):
        cluster_alphas(
            base_dir=str(tmp_path),
            threshold=0.7,
            metric="pearson",
            write_artifact=False,
        )


# ---------------------------------------------------------------------------
# T19 — DoD-D4: ledger row from Gate-C raise / Gate-D reject + idempotency.
# ---------------------------------------------------------------------------


def test_dod_d4_kill_ledger_row_on_gate_c_raise(
    tmp_path: Path,
    isolated_kill_ledger: Path,
) -> None:
    """Gate-C verification raises ValueError; ledger records gate='C'."""
    root, sc_path = _setup_alpha_fixture(
        tmp_path, "alpha_d4_c", gate_c_passed=False
    )
    cfg = PromotionConfig(
        alpha_id="alpha_d4_c",
        owner="charlie",
        project_root=str(root),
        scorecard_path=str(sc_path),
        validation_profile=_strict_profile(),
    )
    with pytest.raises(ValueError, match="Gate C has not passed"):
        promote_alpha(cfg)

    rows = _read_ledger(isolated_kill_ledger)
    assert len(rows) == 1, f"expected exactly 1 ledger row, got {rows}"
    assert rows[0]["alpha_id"] == "alpha_d4_c"
    assert rows[0]["gate"] == "C"
    assert "Gate C has not passed" in rows[0]["reason"]
    assert rows[0]["killed_by"] == "promote_alpha:auto"


def test_dod_d4_kill_ledger_row_on_gate_d_reject(
    tmp_path: Path,
    isolated_kill_ledger: Path,
) -> None:
    """Gate-D rejection (sharpe < threshold) records gate='D' without raising."""
    # sharpe=0.2 < min_sharpe_oos=1.0 -> Gate D fails, promote_alpha
    # returns approved=False (no exception).
    root, sc_path = _setup_alpha_fixture(
        tmp_path, "alpha_d4_d", gate_c_passed=True, sharpe=0.2
    )
    cfg = PromotionConfig(
        alpha_id="alpha_d4_d",
        owner="charlie",
        project_root=str(root),
        scorecard_path=str(sc_path),
        validation_profile=_strict_profile(),
    )
    result = promote_alpha(cfg)
    assert result.approved is False, "Gate-D fail must not auto-approve"
    assert result.gate_d_passed is False

    rows = _read_ledger(isolated_kill_ledger)
    assert len(rows) == 1, f"expected exactly 1 ledger row, got {rows}"
    assert rows[0]["alpha_id"] == "alpha_d4_d"
    assert rows[0]["gate"] == "D"
    # The reason includes the failed sharpe_oos sub-check.
    assert "sharpe_oos" in rows[0]["reason"]
    assert rows[0]["killed_by"] == "promote_alpha:auto"


def test_dod_d4_kill_ledger_idempotent_under_retry(
    tmp_path: Path,
    isolated_kill_ledger: Path,
) -> None:
    """Running promote_alpha twice on the same Gate-D failure yields one row."""
    root, sc_path = _setup_alpha_fixture(
        tmp_path, "alpha_d4_d_idem", gate_c_passed=True, sharpe=0.2
    )
    cfg = PromotionConfig(
        alpha_id="alpha_d4_d_idem",
        owner="charlie",
        project_root=str(root),
        scorecard_path=str(sc_path),
        validation_profile=_strict_profile(),
    )
    for _ in range(2):
        result = promote_alpha(cfg)
        assert result.approved is False

    rows = _read_ledger(isolated_kill_ledger)
    assert len(rows) == 1, (
        f"expected idempotent single ledger row, got {len(rows)}: {rows}"
    )
    # kill_id is deterministic; both retries collapse onto the same one.
    assert rows[0]["kill_id"], "kill_id must be populated"
