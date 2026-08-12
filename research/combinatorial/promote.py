"""Promote a GP-discovered expression into a scaffolded ``research/alphas/<id>/``
package -- the intake path for ``research/combinatorial``'s search output into
the existing, unmodified Gate A/B/C alpha-governance pipeline.

Mirrors ``research/tools/alpha_scaffold.py``'s plain-f-string-generator /
``write_file(force=...)`` / ``ALPHA_CLASS`` convention, with differences
driven by what a GP-discovered candidate actually is:

  - ``formula`` carries the raw GP expression text (``research/combinatorial``'s
    function-call grammar); ``dsl_formula`` stays unset (``None``). The two
    grammars are incompatible (``src/hft_platform/alpha/dsl/`` supports only
    linear ``+ - *`` weighted sums) -- do not try to unify them.
  - Unlike ``alpha_scaffold.py`` (which never writes one), this also writes
    ``manifest.yaml`` -- consumed independently by
    ``research/tools/lifecycle_audit.py`` (status source of truth) and
    ``hft_platform.alpha.kill_ledger``/``promotion`` -- and the generated
    ``impl.py`` loads its ``AlphaManifest`` from that file at construction
    time (single source of truth; editing ``manifest.yaml`` is enough to
    update the alpha's declared metadata, no ``impl.py`` edit needed). This
    mirrors real hand-authored alphas in ``research/alphas/`` (e.g.
    ``c75_tmf_mw_ofi_taker``), which all carry a ``manifest.yaml``.
  - The generated ``impl.py`` wraps ``gp_alpha_adapter.GPCompiledAlpha``, a
    streaming ``AlphaProtocol`` adapter, rather than a hand-written state
    machine.

Every promoted package stops at ``status=DRAFT`` -- exactly the boundary
``alpha_scaffold.py`` already has ("no live registry entry yet"). No gate
runs automatically; a human runs ``hft alpha validate`` next.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from hft_platform.contracts.alpha import AlphaManifest, AlphaStatus
from research.combinatorial.expression_lang import compile_expression
from research.combinatorial.gp_alpha_adapter import max_window_for_expression
from research.combinatorial.ledger import TrialLedger
from research.combinatorial.search_engine import SearchResult


@dataclass(frozen=True)
class Lineage:
    candidate_id: str
    search_run_id: str | None
    parent_ids: tuple[str, ...]
    score: float | None
    selection_sharpe: float | None
    correlation_pool_max: float | None
    search_algorithm: str | None


def class_name_for(alpha_id: str) -> str:
    return "".join(part.capitalize() for part in alpha_id.split("_")) + "Alpha"


def promote_candidate(
    expression: str,
    *,
    alpha_id: str,
    owner: str,
    strategy_type: str = "taker",
    instrument: str,
    out_dir: Path | str = Path("research/alphas"),
    force: bool = False,
    search_result: SearchResult | None = None,
    trial_ledger: TrialLedger | None = None,
) -> Path:
    """Scaffold ``<out_dir>/<alpha_id>/`` from a GP-discovered *expression*.

    Raises ``ValueError``/``SyntaxError`` (via ``compile_expression`` and
    ``max_window_for_expression``) if *expression* is grammar-invalid, uses
    an operator that cannot be streamed (``rank``, 1-arg ``zscore`` -- see
    ``gp_alpha_adapter``'s module docstring), or has a non-constant window
    argument. Raises ``FileExistsError`` if the target directory already
    exists and ``force`` is not set.

    When *search_result* is supplied (the ``hft alpha mine promote
    --from-results`` path), its exact score/lineage metadata is used
    directly. Otherwise (the ``--expression`` path), lineage is recovered
    best-effort from *trial_ledger* (default: the default-path
    ``TrialLedger``) by looking up the candidate's semantic-identity hash --
    empty/``None`` fields if the expression was never run through the search
    engine (e.g. a hand-typed expression).
    """
    compiled = compile_expression(expression)
    window = max_window_for_expression(expression)  # raises on non-streamable expressions
    data_fields = tuple(sorted(compiled.variables))
    complexity = "O(N)" if window > 1 else "O(1)"

    lineage = (
        _lineage_from_search_result(expression, search_result)
        if search_result is not None
        else _lineage_from_ledger(expression, trial_ledger or TrialLedger())
    )

    alpha_dir = Path(out_dir) / alpha_id
    if alpha_dir.exists() and not force:
        raise FileExistsError(f"Alpha directory already exists: {alpha_dir} (use force=True to overwrite)")
    alpha_dir.mkdir(parents=True, exist_ok=True)

    manifest = AlphaManifest(
        alpha_id=alpha_id,
        hypothesis=(
            "GP-discovered via research/combinatorial genetic_search; see README.md "
            "for lineage and discovery-time score (selection-set, not out-of-sample)."
        ),
        formula=expression,
        paper_refs=(),
        data_fields=data_fields,
        complexity=complexity,
        status=AlphaStatus.DRAFT,
        tier=None,
        rust_module=None,
        latency_profile=None,
        roles_used=(),
        skills_used=(),
        feature_set_version=None,
        strategy_type=strategy_type,
        instrument=instrument,
        dsl_formula=None,
        parent_alpha_id=None,
        cost_profile_refs=(),
    )

    _write_file(alpha_dir / "__init__.py", f'"""Alpha package: {alpha_id} (GP-discovered)."""\n', force=force)
    _write_file(alpha_dir / "manifest.yaml", _render_manifest_yaml(manifest, lineage), force=force)
    _write_file(alpha_dir / "impl.py", _render_impl(alpha_id, expression), force=force)
    _write_file(alpha_dir / "README.md", _render_readme(alpha_id, expression, owner, manifest, lineage), force=force)
    return alpha_dir


def promote_from_results(results_path: Path | str, rank: int, **kwargs: Any) -> Path:
    """Read an ``AlphaSearchEngine.save_results()`` JSON and promote ``results[rank]``."""
    payload = json.loads(Path(results_path).read_text())
    results = payload.get("results", [])
    if not (0 <= rank < len(results)):
        raise IndexError(f"rank {rank} out of range for {len(results)} result(s) in {results_path}")
    row = results[rank]
    result = SearchResult(
        expression=str(row["expression"]),
        score=float(row["score"]),
        selection_sharpe=float(row["selection_sharpe"]),
        correlation_pool_max=float(row["correlation_pool_max"]),
        passed=bool(row["passed"]),
        metadata=dict(row.get("metadata", {})),
    )
    return promote_candidate(result.expression, search_result=result, **kwargs)


def promote_smma_candidate(
    expression: str,
    *,
    alpha_id: str,
    owner: str,
    instrument: str,
    candidate_spec: dict[str, Any],
    out_dir: Path | str = Path("research/alphas"),
    force: bool = False,
) -> Path:
    """Scaffold a DRAFT package backed by :class:`SMMACompiledAlpha`.

    This is deliberately separate from live promotion. It only makes a
    governed research package so ``hft alpha screen`` can produce
    ``screen_only=true`` Gate A-C evidence.
    """
    compiled = compile_expression(expression)
    window = max_window_for_expression(expression)
    alpha_dir = Path(out_dir) / alpha_id
    if alpha_dir.exists() and not force:
        raise FileExistsError(f"Alpha directory already exists: {alpha_dir} (use force=True to overwrite)")
    alpha_dir.mkdir(parents=True, exist_ok=True)
    candidate_id = str(candidate_spec.get("candidate_id", TrialLedger.candidate_id_for(expression)))
    lineage = Lineage(
        candidate_id=candidate_id,
        search_run_id=None,
        parent_ids=(),
        score=None,
        selection_sharpe=None,
        correlation_pool_max=None,
        search_algorithm="smma_family_search",
    )
    manifest = AlphaManifest(
        alpha_id=alpha_id,
        hypothesis=(
            "SMMA-family research survivor; recursive Pine-compatible SMMA is "
            "computed causally before the stationary GP expression."
        ),
        formula=expression,
        paper_refs=(),
        data_fields=tuple(sorted(compiled.variables)),
        complexity="O(N)" if window > 1 else "O(1)",
        status=AlphaStatus.DRAFT,
        tier=None,
        rust_module=None,
        latency_profile=None,
        roles_used=("planner", "architect", "code-reviewer"),
        skills_used=("validation-gate", "hft-backtest-engine", "hft-backtest-validation"),
        feature_set_version=None,
        strategy_type="taker",
        instrument=instrument,
        dsl_formula=None,
        parent_alpha_id=None,
        cost_profile_refs=(instrument,),
    )
    _write_file(alpha_dir / "__init__.py", f'"""Alpha package: {alpha_id} (SMMA family)."""\n', force=force)
    manifest_text = _render_manifest_yaml(manifest, lineage)
    manifest_payload = yaml.safe_load(manifest_text) or {}
    manifest_payload["experiment_metadata"].update(
        {
            "origin": "research.combinatorial.smma",
            "family": "smma",
            "candidate_spec": dict(candidate_spec),
            "screen_only_required": True,
            "promotion_eligible": False,
        }
    )
    _write_file(
        alpha_dir / "manifest.yaml",
        yaml.safe_dump(manifest_payload, sort_keys=False),
        force=force,
    )
    _write_file(
        alpha_dir / "impl.py",
        _render_smma_impl(alpha_id, expression),
        force=force,
    )
    readme = _render_readme(alpha_id, expression, owner, manifest, lineage)
    readme += (
        "\n## SMMA Governance\n"
        "- This package is a research DRAFT.\n"
        "- Gate A-C must be run through `hft alpha screen`; resulting scorecards "
        "must retain `screen_only=true`.\n"
        "- This package does not write or enable the live registry.\n"
    )
    _write_file(alpha_dir / "README.md", readme, force=force)
    return alpha_dir


def _lineage_from_search_result(expression: str, result: SearchResult) -> Lineage:
    return Lineage(
        candidate_id=TrialLedger.candidate_id_for(expression),
        search_run_id=None,
        parent_ids=(),
        score=result.score,
        selection_sharpe=result.selection_sharpe,
        correlation_pool_max=result.correlation_pool_max,
        search_algorithm=(
            str(result.metadata["search_algorithm"]) if result.metadata.get("search_algorithm") else None
        ),
    )


def _lineage_from_ledger(expression: str, ledger: TrialLedger) -> Lineage:
    candidate_id = TrialLedger.candidate_id_for(expression)
    matching = [row for row in ledger.read_trials() if row.get("candidate_id") == candidate_id]
    if not matching:
        return Lineage(
            candidate_id=candidate_id,
            search_run_id=None,
            parent_ids=(),
            score=None,
            selection_sharpe=None,
            correlation_pool_max=None,
            search_algorithm=None,
        )
    latest = matching[-1]
    metrics = latest.get("metrics") or {}
    return Lineage(
        candidate_id=candidate_id,
        search_run_id=(str(latest["search_run_id"]) if latest.get("search_run_id") else None),
        parent_ids=tuple(str(p) for p in latest.get("parent_ids", ())),
        score=_to_optional_float(metrics.get("score")),
        selection_sharpe=_to_optional_float(metrics.get("selection_sharpe")),
        correlation_pool_max=_to_optional_float(metrics.get("correlation_pool_max")),
        search_algorithm=None,
    )


def _to_optional_float(value: Any) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _write_file(path: Path, content: str, *, force: bool) -> None:
    if path.exists() and not force:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content)


def _render_manifest_yaml(manifest: AlphaManifest, lineage: Lineage) -> str:
    payload = manifest.to_dict()
    payload["experiment_metadata"] = {
        "origin": "research.combinatorial",
        "candidate_id": lineage.candidate_id,
        "search_run_id": lineage.search_run_id,
        "parent_ids": list(lineage.parent_ids),
        "search_algorithm": lineage.search_algorithm,
        "discovery_score": lineage.score,
        "discovery_selection_sharpe": lineage.selection_sharpe,
        "discovery_correlation_pool_max": lineage.correlation_pool_max,
        "discovery_metric_note": (
            "score/selection_sharpe/correlation_pool_max are selection-set metrics from "
            "research/combinatorial/search_engine.py, never out-of-sample -- see that "
            "module's docstring."
        ),
    }
    return yaml.safe_dump(payload, sort_keys=False)


def _render_impl(alpha_id: str, expression: str) -> str:
    class_name = class_name_for(alpha_id)
    return f"""from __future__ import annotations

from pathlib import Path

import yaml

from hft_platform.contracts.alpha import AlphaManifest
from research.combinatorial.gp_alpha_adapter import GPCompiledAlpha

_EXPRESSION = {expression!r}
_MANIFEST_PATH = Path(__file__).resolve().parent / "manifest.yaml"


class {class_name}(GPCompiledAlpha):
    def __init__(self) -> None:
        manifest_data = yaml.safe_load(_MANIFEST_PATH.read_text(encoding="utf-8")) or {{}}
        super().__init__(expression=_EXPRESSION, manifest=AlphaManifest.from_dict(manifest_data))


ALPHA_CLASS = {class_name}
"""


def _render_smma_impl(alpha_id: str, expression: str) -> str:
    class_name = class_name_for(alpha_id)
    return f"""from __future__ import annotations

from pathlib import Path

import yaml

from hft_platform.contracts.alpha import AlphaManifest
from research.combinatorial.smma_alpha_adapter import SMMACompiledAlpha

_EXPRESSION = {expression!r}
_MANIFEST_PATH = Path(__file__).resolve().parent / "manifest.yaml"


class {class_name}(SMMACompiledAlpha):
    def __init__(self) -> None:
        manifest_data = yaml.safe_load(_MANIFEST_PATH.read_text(encoding="utf-8")) or {{}}
        super().__init__(expression=_EXPRESSION, manifest=AlphaManifest.from_dict(manifest_data))


ALPHA_CLASS = {class_name}
"""


def _render_readme(
    alpha_id: str,
    expression: str,
    owner: str,
    manifest: AlphaManifest,
    lineage: Lineage,
) -> str:
    parent_lines = "".join(f"- `{p}`\n" for p in lineage.parent_ids) or (
        "- (none recorded -- not a crossover/mutation child)\n"
    )
    field_lines = "".join(f"- `{f}`\n" for f in manifest.data_fields) or "- (none -- constant-only expression)\n"
    return (
        f"# {alpha_id}\n\n"
        "## Origin\n"
        "GP-discovered via `research/combinatorial` (genetic/random/template search) -- "
        "not hand-authored. Promoted via `hft alpha mine promote`.\n\n"
        "## Expression\n"
        f"- `{expression}`\n\n"
        "## Data Fields\n"
        f"{field_lines}\n"
        "## Lineage\n"
        f"- `candidate_id`: `{lineage.candidate_id}`\n"
        f"- `search_run_id`: `{lineage.search_run_id or '-'}`\n"
        f"- `search_algorithm`: `{lineage.search_algorithm or '-'}`\n"
        f"- Parent candidate ids:\n{parent_lines}\n"
        "## Discovery-Time Score (selection-set only -- NOT out-of-sample)\n"
        f"- `score`: {lineage.score if lineage.score is not None else '-'}\n"
        f"- `selection_sharpe`: {lineage.selection_sharpe if lineage.selection_sharpe is not None else '-'}\n"
        "- `correlation_pool_max`: "
        f"{lineage.correlation_pool_max if lineage.correlation_pool_max is not None else '-'}\n\n"
        "## Metadata\n"
        f"- `alpha_id`: `{alpha_id}`\n"
        f"- `owner`: {owner}\n"
        f"- `complexity`: `{manifest.complexity}`\n"
        f"- `strategy_type`: `{manifest.strategy_type}`\n"
        f"- `instrument`: `{manifest.instrument}`\n\n"
        "## Status\n"
        "`DRAFT` -- no live registry entry yet. Next: human review, then "
        "`hft alpha validate --profile <strict-profile> --data <path>` to run Gate A.\n"
    )
