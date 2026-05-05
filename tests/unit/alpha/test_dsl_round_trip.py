"""Slice-D Tasks 20a / 20b — DSL round-trip + manifest-binding contracts.

* T20a / DoD-D6 — synthetic formula round-trip. Parses ``"a * b * c"``
  and ``"(a + b) * c"``, asserts canonical equality after unparse, and
  evaluates the compiled AST against synthetic numpy arrays for
  bit-exact agreement with the equivalent numpy expression.
* T20b — manifest-binding round-trip. Reads ``dsl_formula`` from the
  ``r47_maker_pivot/manifest.yaml`` written in T2, parses + unparses it,
  asserts canonical equality + idempotence, and runs the compiled AST
  over synthetic features matching the formula's identifier set,
  asserting finite, shape-preserving output.

DoD-D6 is satisfied by T20a alone; T20b is a bonus binding check that
guarantees the DSL pipeline can consume the on-disk manifest produced
by Slice D's manifest migration step.
"""

from __future__ import annotations

from pathlib import Path

import numpy as np
import yaml

from hft_platform.alpha.dsl import (
    BinOp,
    Identifier,
    Literal,
    UnaryOp,
    compile_ast,
    parse,
    round_trip,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _repo_root() -> Path:
    """tests/unit/alpha/<file> -> repo root is three parents up."""
    return Path(__file__).resolve().parents[3]


def _collect_identifiers(node) -> set[str]:
    """Recursively collect every Identifier name in an AST.

    Used by T20b to discover which feature keys the compiled callable
    will demand — the test then constructs synthetic numpy arrays under
    those exact keys.
    """
    if isinstance(node, Identifier):
        return {node.name}
    if isinstance(node, Literal):
        return set()
    if isinstance(node, UnaryOp):
        return _collect_identifiers(node.operand)
    if isinstance(node, BinOp):
        return _collect_identifiers(node.left) | _collect_identifiers(node.right)
    raise TypeError(f"unknown DSL node type: {type(node).__name__}")


# ---------------------------------------------------------------------------
# T20a — synthetic round-trip + bit-exact compile/eval.
# ---------------------------------------------------------------------------


def test_synthetic_formula_round_trip() -> None:
    """DoD-D6: round-trip + compile + bit-exact numpy match."""
    cases: list[tuple[str, str]] = [
        ("a * b * c", "a * b * c"),
        ("(a + b) * c", "(a + b) * c"),
    ]
    rng = np.random.default_rng(42)
    a = rng.standard_normal(100)
    b = rng.standard_normal(100)
    c = rng.standard_normal(100)
    features = {"a": a, "b": b, "c": c}

    for source, expected in cases:
        canonical = round_trip(source)
        assert canonical == expected, f"round_trip({source!r}) = {canonical!r}, expected {expected!r}"
        # Idempotence: round_trip is a fixpoint.
        assert round_trip(canonical) == canonical, f"round_trip not idempotent for {canonical!r}"

        node = parse(source)
        fn = compile_ast(node)
        actual = fn(features)

        # Exact-match against the equivalent direct numpy expression.
        if "+" in source and "*" in source:
            expected_arr = (a + b) * c
        else:
            expected_arr = a * b * c
        np.testing.assert_array_equal(actual, expected_arr)
        assert actual.shape == (100,)
        assert np.all(np.isfinite(actual))


# ---------------------------------------------------------------------------
# T20b — manifest binding via r47_maker_pivot.
# ---------------------------------------------------------------------------


def test_r47_maker_pivot_round_trip() -> None:
    """T20b: read dsl_formula from the on-disk manifest, round-trip, compile.

    The manifest binding committed in T2 must survive parser + unparser
    + compiler without losing semantics. We do not compare the result
    against any historical signal — none exists at the relevant commit
    — so the contract here is structural: round-trip is idempotent, the
    AST compiles, and the callable returns finite, shape-matched output
    on synthetic input.
    """
    manifest_path = _repo_root() / "research" / "alphas" / "r47_maker_pivot" / "manifest.yaml"
    manifest = yaml.safe_load(manifest_path.read_text(encoding="utf-8"))
    formula = manifest.get("dsl_formula")
    assert isinstance(formula, str) and formula, (
        "r47_maker_pivot manifest.yaml must have a non-empty dsl_formula (populated in Slice-D T2)"
    )

    canonical = round_trip(formula)
    # Idempotence: round_trip is a fixpoint regardless of input form.
    assert round_trip(canonical) == canonical, f"round_trip not idempotent for {canonical!r}"

    node = parse(formula)
    ident_names = _collect_identifiers(node)
    assert ident_names, "manifest formula must reference at least one identifier"

    rng = np.random.default_rng(42)
    n = 100
    features = {name: rng.standard_normal(n) for name in ident_names}
    fn = compile_ast(node)
    out = fn(features)

    assert out.shape == (n,), f"compiled output shape {out.shape}, expected ({n},)"
    assert np.all(np.isfinite(out)), "compiled output must be finite on finite input"
