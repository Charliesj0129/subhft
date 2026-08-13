"""Alpha combinatorial search engine — random, template-sweep, and genetic-programming search.

``AlphaSearchEngine`` evaluates expression strings against a feature dict and returns
``SearchResult`` objects ranked by a composite score (information ratio − 0.25 × pool-correlation).

All results are scored on the same selection-set data passed to the engine — there is no
train/test partitioning in this module, so ``SearchResult.selection_sharpe`` must never be
read as an out-of-sample metric. Real OOS evaluation belongs to the Gate C/D backtest
pipeline (``research/backtest``), not this module.
"""

from __future__ import annotations

import itertools
import json
import random
import re
import uuid
from collections import defaultdict
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Mapping, Sequence

import numpy as np
from structlog import get_logger

from research.combinatorial.canonical_ast import canonical_hash, crossover
from research.combinatorial.expression_lang import compile_expression
from research.combinatorial.ledger import TrialLedger

logger = get_logger("search_engine")

# `(`, `)`, `,` each match as their own token; any other non-whitespace,
# non-paren, non-comma run (identifiers, int/float literals, signs) is one token.
_MUTATION_TOKEN_RE = re.compile(r"\(|\)|,|[^\s(),]+")


@dataclass(frozen=True)
class SearchResult:
    expression: str
    score: float
    selection_sharpe: float
    correlation_pool_max: float
    passed: bool
    metadata: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class AlphaSearchEngine:
    def __init__(
        self,
        *,
        features: Mapping[str, Sequence[float]],
        returns: Sequence[float] | None = None,
        pool_signals: Mapping[str, Sequence[float]] | None = None,
        random_seed: int = 42,
        dataset_fingerprint: str | None = None,
        search_run_id: str | None = None,
        trial_ledger: TrialLedger | None = None,
        partition_manifest_hash: str = "",
    ) -> None:
        self.features = {str(k): np.asarray(v, dtype=np.float64).reshape(-1) for k, v in features.items()}
        if not self.features:
            raise ValueError("features must not be empty")
        self.returns = _as_returns(returns)
        self.pool_signals = {
            str(k): np.asarray(v, dtype=np.float64).reshape(-1) for k, v in (pool_signals or {}).items()
        }
        self._rng = random.Random(int(random_seed))
        self._feature_keys = tuple(sorted(self.features))
        self._window_choices = (3, 5, 10, 20, 50, 100)
        self._pool_corr_groups = _build_pool_corr_groups(self.pool_signals)
        self._mutation_failures = 0
        self._crossover_failures = 0
        self.dataset_fingerprint = dataset_fingerprint
        self.search_run_id = search_run_id or uuid.uuid4().hex
        self.trial_ledger = trial_ledger
        self.partition_manifest_hash = partition_manifest_hash
        self._result_cache: dict[str, SearchResult] = {}

    def random_search(self, n_trials: int = 1000) -> list[SearchResult]:
        """Generate and evaluate *n_trials* random expressions; return sorted best-first."""
        out: list[SearchResult] = []
        for _ in range(max(1, int(n_trials))):
            expr = self._random_expression()
            result = self.evaluate_expression(expr, algorithm="random_search")
            out.append(result)
        return sorted(out, key=lambda x: x.score, reverse=True)

    def template_sweep(
        self,
        template: str,
        param_grid: Mapping[str, Sequence[Any]],
    ) -> list[SearchResult]:
        """Evaluate all combinations of *param_grid* substituted into *template*.

        The template uses Python ``str.format`` syntax, e.g. ``"ts_mean(x, {window})"``.
        Returns results sorted best-first.
        """
        keys = sorted(param_grid)
        if not keys:
            return [self.evaluate_expression(template, algorithm="template_sweep")]

        out: list[SearchResult] = []
        for combo in itertools.product(*(param_grid[k] for k in keys)):
            params = {k: v for k, v in zip(keys, combo)}
            expr = template.format(**params)
            result = self.evaluate_expression(expr, algorithm="template_sweep")
            out.append(result)
        return sorted(out, key=lambda x: x.score, reverse=True)

    def genetic_search(
        self,
        *,
        population: int = 100,
        generations: int = 50,
        survival_ratio: float = 0.3,
        mutation_prob: float = 0.7,
        crossover_prob: float = 0.6,
        tournament_size: int = 3,
    ) -> list[SearchResult]:
        """Genetic-programming search: tournament selection, typed role-aware subtree
        crossover, and elitism.

        Each generation keeps the top *survival_ratio* of the population unchanged
        (elitism). Each remaining slot is filled by, per slot, up to two independent
        rolls: first against *crossover_prob* — if it hits, two tournament-selected
        parents (sample size *tournament_size*, drawn from the *whole* current-
        generation population, not just the elite subset) breed via typed subtree
        crossover (``canonical_ast.crossover``); otherwise a second roll against
        *mutation_prob* — if it hits, one tournament-selected parent is mutated via
        ``_mutate_expression`` (unchanged token-level mutation); otherwise a fresh
        random expression is generated. Parent lineage (candidate ids) is threaded
        into the trial ledger's ``parent_ids`` field via ``evaluate_expression``.

        Returns the final population sorted best-first.
        """
        pop_size = max(4, int(population))
        keep_n = max(2, int(pop_size * float(survival_ratio)))
        cross_p = float(crossover_prob)
        mutate_p = float(mutation_prob)
        k = max(2, int(tournament_size))

        population_expr = [self._random_expression() for _ in range(pop_size)]
        results = [self.evaluate_expression(expr, algorithm="genetic_search") for expr in population_expr]
        results.sort(key=lambda x: x.score, reverse=True)

        for gen in range(max(1, int(generations))):
            survivors = results[:keep_n]
            children: list[SearchResult] = []
            while len(children) + len(survivors) < pop_size:
                if self._rng.random() < cross_p:
                    parent_a = self._tournament_select(results, k)
                    parent_b = self._tournament_select(results, k)
                    child_expr = self._crossover_expression(parent_a.expression, parent_b.expression)
                    parent_ids = (
                        TrialLedger.candidate_id_for(parent_a.expression),
                        TrialLedger.candidate_id_for(parent_b.expression),
                    )
                elif self._rng.random() < mutate_p:
                    parent = self._tournament_select(results, k)
                    child_expr = self._mutate_expression(parent.expression)
                    parent_ids = (TrialLedger.candidate_id_for(parent.expression),)
                else:
                    child_expr = self._random_expression()
                    parent_ids = ()
                children.append(self.evaluate_expression(child_expr, algorithm="genetic_search", parent_ids=parent_ids))
            results = survivors + children
            results.sort(key=lambda x: x.score, reverse=True)
            logger.debug("genetic_search_generation_complete", generation=gen, best_score=results[0].score)
        return results

    def _tournament_select(self, population: Sequence[SearchResult], k: int) -> SearchResult:
        """Real tournament selection: sample *k* individuals (without replacement,
        clamped to population size) from the *whole* current-generation population
        and return the best of the sample — distinct from the legacy behavior of
        picking uniformly from only the already-elite survivors subset.
        """
        sample_size = min(k, len(population))
        contestants = self._rng.sample(list(population), sample_size)
        return max(contestants, key=lambda r: r.score)

    def _crossover_expression(self, expr_a: str, expr_b: str) -> str:
        """Typed, role-aware subtree crossover of two parent expressions.

        Falls back to a fresh random expression on any failure (no common
        crossover role, resulting depth exceeds ``compile_expression``'s
        ``max_depth``, or any other compile-time rejection) — same established
        fallback posture as ``_mutate_expression``.
        """
        try:
            child = crossover(expr_a, expr_b, self._rng)
            compile_expression(child)
            return child
        except Exception as exc:
            self._crossover_failures += 1
            logger.warning("alpha_crossover_failed", expr_a=expr_a[:120], expr_b=expr_b[:120], reason=str(exc))
            return self._random_expression()

    def evaluate_expression(
        self,
        expression: str,
        *,
        algorithm: str = "manual",
        parent_ids: Sequence[str] = (),
    ) -> SearchResult:
        """Compile and evaluate a single *expression* string; return its ``SearchResult``.

        *algorithm* is a free-form label (e.g. ``"random_search"``, ``"template_sweep"``,
        ``"genetic_search"``) recorded in ``metadata["search_algorithm"]`` for
        provenance; it has no effect on scoring. *parent_ids* records lineage (parent
        candidate ids) in the trial ledger, when attached — populated by
        ``genetic_search`` for crossover (2 parents) and mutation (1 parent) children;
        empty for random/manual/template-sweep expressions with no parent.

        Semantically-equivalent expressions (commutative reordering, ``BinOp``/``Call``
        equivalence, numeric-literal formatting — see ``canonical_ast``) within this
        engine instance reuse a cached ``SearchResult`` instead of recompiling and
        rescoring. The ledger's own trial identity is likewise semantic, so a repeat
        call with an equivalent expression (same dataset/algorithm) dedupes to the
        same ledger row rather than double-counting — it is the same hypothesis, not
        a new one.

        When ``self.trial_ledger`` is set, every call is recorded — compile *or*
        evaluation failures as a ``stage="compile"`` row (then re-raised; ledger
        recording is additive and never swallows errors), successful evaluations as a
        ``stage="search"`` row.
        """
        canonical_key = _safe_canonical_hash(expression)

        if canonical_key is not None and canonical_key in self._result_cache:
            cached = self._result_cache[canonical_key]
            if self.trial_ledger is not None:
                self._record_trial(self.trial_ledger, expression, algorithm, cached, parent_ids=parent_ids)
            return cached

        if self.trial_ledger is not None:
            try:
                compiled = compile_expression(expression)
                signal = compiled.evaluate(self.features)
            except Exception as exc:
                self.trial_ledger.record_compile_failure(
                    expression=expression,
                    dataset_fingerprint=self.dataset_fingerprint or "",
                    partition_manifest_hash=self.partition_manifest_hash,
                    algorithm=algorithm,
                    search_run_id=self.search_run_id,
                    reason=str(exc),
                )
                raise
        else:
            compiled = compile_expression(expression)
            signal = compiled.evaluate(self.features)

        ratio = _signal_information_ratio(signal, self.returns)
        corr = self._pool_corr_max(signal)
        score = float(ratio - (0.25 * corr))
        passed = bool(ratio > 0.5 and corr < 0.7)
        result = SearchResult(
            expression=expression,
            score=score,
            selection_sharpe=float(ratio),
            correlation_pool_max=float(corr),
            passed=passed,
            metadata={
                "variables": list(compiled.variables),
                "depth": compiled.max_depth,
                "returns_used": self.returns is not None,
                "search_algorithm": algorithm,
            },
        )

        if canonical_key is not None:
            self._result_cache[canonical_key] = result

        if self.trial_ledger is not None:
            self._record_trial(self.trial_ledger, expression, algorithm, result, parent_ids=parent_ids)

        return result

    def _record_trial(
        self,
        ledger: TrialLedger,
        expression: str,
        algorithm: str,
        result: SearchResult,
        *,
        parent_ids: Sequence[str] = (),
    ) -> None:
        candidate_id = ledger.candidate_id_for(expression)
        trial_id = ledger.trial_id_for(
            expression=expression,
            dataset_fingerprint=self.dataset_fingerprint or "",
            partition_manifest_hash=self.partition_manifest_hash,
            algorithm=algorithm,
        )
        ledger.record_trial(
            trial_id=trial_id,
            search_run_id=self.search_run_id,
            candidate_id=candidate_id,
            dataset_fingerprint=self.dataset_fingerprint or "",
            stage="search",
            status="passed" if result.passed else "failed",
            parent_ids=parent_ids,
            metrics={
                "score": result.score,
                "selection_sharpe": result.selection_sharpe,
                "correlation_pool_max": result.correlation_pool_max,
            },
        )

    def save_results(
        self,
        results: Sequence[SearchResult],
        *,
        path: str = "research/combinatorial/results/latest.json",
    ) -> str:
        """Serialise *results* to JSON at *path*; returns the path written."""
        out = Path(path)
        out.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "search_run_id": self.search_run_id,
            "dataset_fingerprint": self.dataset_fingerprint,
            "results": [item.to_dict() for item in results],
        }
        out.write_text(json.dumps(payload, indent=2, sort_keys=True))
        return str(out)

    def _random_expression(self) -> str:
        field = self._rng.choice(self._feature_keys)
        window = self._rng.choice(self._window_choices)
        family = self._rng.choice(["trend", "mean_revert", "volume", "mix"])
        if family == "trend":
            return f"zscore(ts_delta({field}, {window}), {window})"
        if family == "mean_revert":
            return f"sign(ts_delta({field}, {window}))"
        if family == "volume":
            return f"rank(ts_sum({field}, {window}))"
        other = self._rng.choice(self._feature_keys)
        return f"sign(ts_corr({field}, {other}, {window}))"

    def _mutate_expression(self, expression: str) -> str:
        # `(`, `)`, `,` are their own tokens (not stripped) so the rebuilt string
        # keeps valid call syntax; Python's tokenizer doesn't care about the extra
        # whitespace `" ".join` introduces around them.
        tokens = _MUTATION_TOKEN_RE.findall(expression)
        out = list(tokens)
        for i, token in enumerate(tokens):
            if token.isdigit() and self._rng.random() < 0.5:
                out[i] = str(self._rng.choice(self._window_choices))
            elif token in self.features and self._rng.random() < 0.3:
                out[i] = self._rng.choice(self._feature_keys)
        rebuilt = " ".join(out)
        try:
            compile_expression(rebuilt)
            return rebuilt
        except Exception as exc:
            self._mutation_failures += 1
            logger.warning("alpha_mutation_failed", expr=rebuilt[:120], reason=str(exc))
            return self._random_expression()

    def _pool_corr_max(self, signal: np.ndarray) -> float:
        if not self._pool_corr_groups:
            return 0.0
        sig = np.asarray(signal, dtype=np.float64).reshape(-1)
        if sig.size < 2:
            return 0.0

        out = 0.0
        used_fast_path = False
        for n, group in self._pool_corr_groups.items():
            if n < 2 or sig.size < n:
                continue
            mat = group["centered"]
            norms = group["norms"]
            if mat.size == 0 or norms.size == 0:
                continue
            view = np.nan_to_num(sig[:n], nan=0.0, posinf=0.0, neginf=0.0)
            view_centered = view - float(np.mean(view))
            sig_norm = float(np.linalg.norm(view_centered))
            if sig_norm <= 1e-12:
                continue
            denom = norms * sig_norm
            dots = mat @ view_centered
            corrs = np.zeros_like(dots, dtype=np.float64)
            np.divide(dots, denom, out=corrs, where=denom > 1e-12)
            if corrs.size:
                local_max = float(np.max(np.abs(corrs)))
                if np.isfinite(local_max):
                    out = max(out, local_max)
                    used_fast_path = True

        # Rare path: pool arrays longer than signal (mismatched lengths). Fall back to exact correlation.
        if not used_fast_path or any(n > sig.size for n in self._pool_corr_groups):
            for pool in self.pool_signals.values():
                n = min(sig.size, pool.size)
                if n < 2:
                    continue
                corr = np.corrcoef(sig[:n], pool[:n])[0, 1]
                if np.isfinite(corr):
                    out = max(out, abs(float(corr)))
        return float(out)


def _safe_canonical_hash(expression: str) -> str | None:
    """``canonical_hash``, or ``None`` if *expression* isn't grammar-valid.

    Malformed expressions skip the within-run cache entirely (never blocks
    evaluation) and fall through to the normal compile/evaluate path, which
    records the failure via ``TrialLedger.record_compile_failure`` as before.
    """
    try:
        return canonical_hash(expression)
    except (SyntaxError, ValueError):
        return None


def _as_returns(values: Sequence[float] | None) -> np.ndarray | None:
    if values is None:
        return None
    arr = np.asarray(values, dtype=np.float64).reshape(-1)
    if arr.size < 2:
        return None
    return np.nan_to_num(arr, nan=0.0, posinf=0.0, neginf=0.0)


def _signal_information_ratio(signal: np.ndarray, returns: np.ndarray | None) -> float:
    """Raw (non-annualized) information ratio: mean(pnl) / std(pnl).

    Deliberately not annualized: *signal*/*returns* here are per-tick/event samples,
    not daily-aggregated returns, so a fixed sqrt(252) factor has no valid meaning at
    this granularity and would misrepresent the true annualized risk-adjusted return.
    Annualization is only correct once PnL has been aggregated to trading-day
    granularity, which this module does not do.
    """
    sig = np.asarray(signal, dtype=np.float64).reshape(-1)
    if sig.size < 3:
        return 0.0
    if returns is None:
        shifted = np.roll(sig, -1)
        shifted[-1] = sig[-1]
        pnl = sig * (shifted - sig)
    else:
        n = min(sig.size, returns.size)
        pnl = sig[:n] * returns[:n]
    sigma = float(np.std(pnl))
    if sigma <= 1e-12:
        return 0.0
    return float(np.mean(pnl) / sigma)


def _build_pool_corr_groups(pool_signals: Mapping[str, np.ndarray]) -> dict[int, dict[str, np.ndarray]]:
    if not pool_signals:
        return {}
    grouped_centered: dict[int, list[np.ndarray]] = defaultdict(list)
    grouped_norms: dict[int, list[float]] = defaultdict(list)

    for pool in pool_signals.values():
        arr = np.asarray(pool, dtype=np.float64).reshape(-1)
        n = int(arr.size)
        if n < 2:
            continue
        clean = np.nan_to_num(arr, nan=0.0, posinf=0.0, neginf=0.0)
        centered = clean - float(np.mean(clean))
        norm = float(np.linalg.norm(centered))
        if norm <= 1e-12:
            continue
        grouped_centered[n].append(centered)
        grouped_norms[n].append(norm)

    out: dict[int, dict[str, np.ndarray]] = {}
    for n, rows in grouped_centered.items():
        if not rows:
            continue
        out[n] = {
            "centered": np.ascontiguousarray(np.vstack(rows), dtype=np.float64),
            "norms": np.asarray(grouped_norms[n], dtype=np.float64),
        }
    return out
