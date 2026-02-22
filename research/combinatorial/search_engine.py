"""Alpha combinatorial search engine — random, template-sweep, and genetic strategies.

``AlphaSearchEngine`` evaluates expression strings against a feature dict and returns
``SearchResult`` objects ranked by a composite score (Sharpe − 0.25 × pool-correlation).
"""

from __future__ import annotations

import itertools
import json
import random
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Mapping, Sequence

import numpy as np

from research.combinatorial.expression_lang import compile_expression


@dataclass(frozen=True)
class SearchResult:
    expression: str
    score: float
    sharpe_oos: float
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
    ) -> None:
        self.features = {str(k): np.asarray(v, dtype=np.float64).reshape(-1) for k, v in features.items()}
        if not self.features:
            raise ValueError("features must not be empty")
        self.returns = _as_returns(returns)
        self.pool_signals = {
            str(k): np.asarray(v, dtype=np.float64).reshape(-1) for k, v in (pool_signals or {}).items()
        }
        self._rng = random.Random(int(random_seed))

    def random_search(self, n_trials: int = 1000) -> list[SearchResult]:
        """Generate and evaluate *n_trials* random expressions; return sorted best-first."""
        out: list[SearchResult] = []
        for _ in range(max(1, int(n_trials))):
            expr = self._random_expression()
            result = self.evaluate_expression(expr)
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
            return [self.evaluate_expression(template)]

        out: list[SearchResult] = []
        for combo in itertools.product(*(param_grid[k] for k in keys)):
            params = {k: v for k, v in zip(keys, combo)}
            expr = template.format(**params)
            result = self.evaluate_expression(expr)
            out.append(result)
        return sorted(out, key=lambda x: x.score, reverse=True)

    def genetic_search(
        self,
        *,
        population: int = 100,
        generations: int = 50,
        survival_ratio: float = 0.3,
        mutation_prob: float = 0.7,
    ) -> list[SearchResult]:
        """Evolutionary search: survive top *survival_ratio* each generation, mutate rest.

        Returns the final population sorted best-first.
        """
        pop_size = max(4, int(population))
        keep_n = max(2, int(pop_size * float(survival_ratio)))
        mutate_p = float(mutation_prob)

        population_expr = [self._random_expression() for _ in range(pop_size)]
        results = [self.evaluate_expression(expr) for expr in population_expr]
        results.sort(key=lambda x: x.score, reverse=True)

        for _ in range(max(1, int(generations))):
            survivors = results[:keep_n]
            children: list[str] = []
            while len(children) + len(survivors) < pop_size:
                parent = self._rng.choice(survivors).expression
                child = self._mutate_expression(parent) if self._rng.random() < mutate_p else self._random_expression()
                children.append(child)
            results = survivors + [self.evaluate_expression(expr) for expr in children]
            results.sort(key=lambda x: x.score, reverse=True)
        return results

    def evaluate_expression(self, expression: str) -> SearchResult:
        """Compile and evaluate a single *expression* string; return its ``SearchResult``."""
        compiled = compile_expression(expression)
        signal = compiled.evaluate(self.features)
        sharpe = _signal_sharpe(signal, self.returns)
        corr = _pool_corr_max(signal, self.pool_signals)
        score = float(sharpe - (0.25 * corr))
        passed = bool(sharpe > 0.5 and corr < 0.7)
        return SearchResult(
            expression=expression,
            score=score,
            sharpe_oos=float(sharpe),
            correlation_pool_max=float(corr),
            passed=passed,
            metadata={
                "variables": list(compiled.variables),
                "depth": compiled.max_depth,
                "returns_used": self.returns is not None,
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
        payload = [item.to_dict() for item in results]
        out.write_text(json.dumps(payload, indent=2, sort_keys=True))
        return str(out)

    def _random_expression(self) -> str:
        field = self._rng.choice(sorted(self.features))
        window = self._rng.choice([3, 5, 10, 20, 50, 100])
        family = self._rng.choice(["trend", "mean_revert", "volume", "mix"])
        if family == "trend":
            return f"zscore(ts_delta({field}, {window}), {window})"
        if family == "mean_revert":
            return f"sign(ts_delta({field}, {window}))"
        if family == "volume":
            return f"rank(ts_sum({field}, {window}))"
        other = self._rng.choice(sorted(self.features))
        return f"sign(ts_corr({field}, {other}, {window}))"

    def _mutate_expression(self, expression: str) -> str:
        tokens = expression.replace("(", " ").replace(")", " ").replace(",", " ").split()
        out = list(tokens)
        for i, token in enumerate(tokens):
            if token.isdigit() and self._rng.random() < 0.5:
                out[i] = str(self._rng.choice([3, 5, 10, 20, 50, 100]))
            elif token in self.features and self._rng.random() < 0.3:
                out[i] = self._rng.choice(sorted(self.features))
        rebuilt = " ".join(out)
        rebuilt = rebuilt.replace(" ,", ",").replace("( ", "(").replace(" )", ")")
        try:
            compile_expression(rebuilt)
            return rebuilt
        except Exception:
            return self._random_expression()


def _as_returns(values: Sequence[float] | None) -> np.ndarray | None:
    if values is None:
        return None
    arr = np.asarray(values, dtype=np.float64).reshape(-1)
    if arr.size < 2:
        return None
    return np.nan_to_num(arr, nan=0.0, posinf=0.0, neginf=0.0)


def _signal_sharpe(signal: np.ndarray, returns: np.ndarray | None) -> float:
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
    return float(np.mean(pnl) / sigma * np.sqrt(252.0))


def _pool_corr_max(signal: np.ndarray, pool_signals: Mapping[str, np.ndarray]) -> float:
    if not pool_signals:
        return 0.0
    sig = np.asarray(signal, dtype=np.float64).reshape(-1)
    out = 0.0
    for pool in pool_signals.values():
        arr = np.asarray(pool, dtype=np.float64).reshape(-1)
        n = min(sig.size, arr.size)
        if n < 2:
            continue
        corr = np.corrcoef(sig[:n], arr[:n])[0, 1]
        if np.isfinite(corr):
            out = max(out, abs(float(corr)))
    return float(out)
