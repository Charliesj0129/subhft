"""Slice D T10 — DSL formula_context tests.

Verifies the two helpers used by the alpha-factory CLI to handle DSL
formulas at the manifest boundary:

* :func:`round_trip(formula)` — parse → unparse, returns canonical text.
* :func:`bind_to_manifest(formula, manifest)` — updates the manifest's
  ``dsl_formula`` field after validating identifier coverage.
"""

from __future__ import annotations

import pytest

from hft_platform.alpha.dsl import (
    DSLNameError,
    bind_to_manifest,
    round_trip,
)
from research.registry.schemas import AlphaManifest


def _make_manifest(
    *,
    data_fields: tuple[str, ...],
    dsl_formula: str | None = None,
) -> AlphaManifest:
    return AlphaManifest(
        alpha_id="r47_test_alpha",
        hypothesis="test hypothesis",
        formula="legacy formula text",
        paper_refs=("paper://test/0001",),
        data_fields=data_fields,
        complexity="O(1)",
        dsl_formula=dsl_formula,
    )


def test_round_trip_canonicalizes_spacing() -> None:
    assert round_trip("a*b") == "a * b"
    # idempotent
    assert round_trip(round_trip("a*b")) == "a * b"


def test_round_trip_preserves_addition_spacing() -> None:
    assert round_trip("a +  b") == "a + b"


def test_round_trip_drops_redundant_parens() -> None:
    # (a*b) + c -- parens redundant because * binds tighter than +.
    assert round_trip("(a*b)+c") == "a * b + c"


def test_round_trip_keeps_required_parens() -> None:
    # a*(b+c) -- parens REQUIRED to override + < * precedence.
    assert round_trip("a*(b+c)") == "a * (b + c)"


def test_round_trip_unary_minus() -> None:
    assert round_trip("-a") == "-a"


def test_round_trip_unary_minus_inside_expr() -> None:
    assert round_trip("a + -b") == "a + -b"


def test_round_trip_integer_literal_drops_trailing_zero() -> None:
    assert round_trip("2*a") == "2 * a"


def test_round_trip_float_literal_keeps_decimal() -> None:
    assert round_trip("3.14*a") == "3.14 * a"


def test_round_trip_r47_canonical_formula() -> None:
    formula = "d1_pe_entropy * d2_queue_survival * d3_mfg_inventory"
    assert round_trip(formula) == formula


def test_round_trip_idempotent_on_complex_formula() -> None:
    canonical = round_trip("(a+b)*c+d*e")
    assert round_trip(canonical) == canonical


def test_bind_updates_dsl_formula() -> None:
    manifest = _make_manifest(data_fields=("a", "b"))
    bound = bind_to_manifest("a + b", manifest)
    assert bound.dsl_formula == "a + b"


def test_bind_canonicalizes_dsl_formula() -> None:
    manifest = _make_manifest(data_fields=("a", "b"))
    bound = bind_to_manifest("a*b", manifest)
    assert bound.dsl_formula == "a * b"


def test_bind_rejects_unknown_identifier() -> None:
    manifest = _make_manifest(data_fields=("a",))
    with pytest.raises(DSLNameError):
        bind_to_manifest("a + b", manifest)


def test_bind_rejects_unknown_identifier_carries_name() -> None:
    manifest = _make_manifest(data_fields=("a",))
    with pytest.raises(DSLNameError) as exc:
        bind_to_manifest("a + missing_ident", manifest)
    assert "missing_ident" in str(exc.value)


def test_bind_preserves_other_fields() -> None:
    manifest = _make_manifest(data_fields=("a", "b"))
    bound = bind_to_manifest("a + b", manifest)
    assert bound.alpha_id == manifest.alpha_id
    assert bound.hypothesis == manifest.hypothesis
    assert bound.formula == manifest.formula
    assert bound.paper_refs == manifest.paper_refs
    assert bound.data_fields == manifest.data_fields
    assert bound.complexity == manifest.complexity


def test_bind_returns_new_instance_does_not_mutate_input() -> None:
    manifest = _make_manifest(data_fields=("a", "b"), dsl_formula=None)
    bound = bind_to_manifest("a + b", manifest)
    # AlphaManifest is frozen; bind_to_manifest must produce a new
    # instance and the input must remain unchanged.
    assert manifest.dsl_formula is None
    assert bound is not manifest
    assert bound.dsl_formula == "a + b"


def test_bind_accepts_formula_with_only_known_identifiers() -> None:
    manifest = _make_manifest(data_fields=("d1", "d2", "d3"))
    bound = bind_to_manifest("d1 * d2 * d3", manifest)
    assert bound.dsl_formula == "d1 * d2 * d3"
