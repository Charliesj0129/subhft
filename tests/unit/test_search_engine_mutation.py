"""Mutation must edit an expression, not shred it.

``_mutate_expression`` used to split the expression on ``(``, ``)`` and ``,`` and rejoin
the tokens with spaces, producing strings like ``zscore ts_delta f0 5 10`` that
``compile_expression`` rejects. Every mutation then fell back to a fresh random
expression, so ``genetic_search`` was a random search with extra steps: it logged
``alpha_mutation_failed`` on every child and selection pressure never reached the
expressions it selected.
"""

from __future__ import annotations

import ast

import numpy as np

from research.combinatorial.expression_lang import compile_expression
from research.combinatorial.search_engine import AlphaSearchEngine

PARENTS = (
    "zscore(ts_delta(f0, 5), 5)",
    "sign(ts_delta(f1, 20))",
    "rank(ts_sum(f2, 50))",
    "sign(ts_corr(f0, f1, 20))",
)
TRIALS = 200


def _engine(seed: int = 7) -> AlphaSearchEngine:
    rng = np.random.default_rng(seed)
    features = {f"f{i}": rng.normal(size=256) for i in range(3)}
    return AlphaSearchEngine(features=features, returns=rng.normal(size=256), random_seed=seed)


def _shape(expression: str) -> str:
    """The expression tree with every feature and number blanked: operators and nesting only."""
    tree = ast.parse(expression, mode="eval")

    class Blank(ast.NodeTransformer):
        def visit_Name(self, node: ast.Name) -> ast.AST:  # noqa: N802 - ast visitor API
            return node if isinstance(node.ctx, ast.Load) and node.id in _OPERATORS else ast.Name(id="F", ctx=node.ctx)

        def visit_Constant(self, node: ast.Constant) -> ast.AST:  # noqa: N802
            return ast.Constant(value=0)

    return ast.dump(Blank().visit(tree))


_OPERATORS = frozenset({"zscore", "ts_delta", "sign", "rank", "ts_sum", "ts_corr"})


def _mutants(engine: AlphaSearchEngine) -> list[tuple[str, str]]:
    return [(parent, engine._mutate_expression(parent)) for _ in range(TRIALS // len(PARENTS)) for parent in PARENTS]


def test_mutation_yields_compilable_expressions_of_the_same_shape() -> None:
    engine = _engine()

    pairs = _mutants(engine)

    compiled = 0
    same_shape = 0
    for parent, child in pairs:
        try:
            compile_expression(child)
            compiled += 1
        except Exception:  # noqa: BLE001 - counting failures is the assertion
            continue
        same_shape += _shape(child) == _shape(parent)
    assert len(pairs) == TRIALS
    assert compiled >= 0.95 * TRIALS, f"only {compiled}/{TRIALS} mutants compile"
    assert same_shape >= 0.95 * TRIALS, f"only {same_shape}/{TRIALS} mutants keep the parent's tree"


def test_mutation_rarely_falls_back_to_a_random_expression() -> None:
    engine = _engine()

    _mutants(engine)

    assert engine._mutation_failures <= 0.05 * TRIALS


def test_mutation_actually_changes_the_expression() -> None:
    engine = _engine()

    changed = sum(parent != child for parent, child in _mutants(engine))

    assert changed >= 0.5 * TRIALS


def test_mutation_without_a_draw_below_threshold_returns_the_parent_unchanged() -> None:
    engine = _engine()
    engine._rng.random = lambda: 0.99  # type: ignore[method-assign] - no token is selected for mutation

    assert [engine._mutate_expression(parent) for parent in PARENTS] == list(PARENTS)


def test_mutation_never_swaps_in_a_feature_name_the_engine_does_not_have() -> None:
    engine = _engine()

    names = {
        node.id
        for _, child in _mutants(engine)
        for node in ast.walk(ast.parse(child, mode="eval"))
        if isinstance(node, ast.Name)
    }

    assert names - _OPERATORS <= set(engine.features)
