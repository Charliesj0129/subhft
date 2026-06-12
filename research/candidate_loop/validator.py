"""Staged candidate validation with first-failure-wins death reasons (spec §6/§13).

Stages, in order; the first failing stage assigns the primary
``death_reason``:

1. ``SCHEMA_INVALID``       — msgspec decode / field / enum / label-format
   (feature count > 6 maps to ``OVER_COMPLEX`` here, per §13)
2. ``FORMULA_PARSE_ERROR``  — grammar failure in any feature/signal/regime
   formula, or a non-comparison regime_filter
3. ``PRIMITIVE_INVALID`` / ``UNSUPPORTED_NEW_PRIMITIVE`` — non-whitelisted
   call or unknown reference; ``future_mid_return`` outside the label
4. ``ARGUMENT_INVALID``     — side/levels/window/horizon domain violation
5. ``OVER_COMPLEX``         — inlined signal AST > 64 nodes or call depth > 3
6. ``DUPLICATE_ALPHA``      — formula_hash collision (within batch or vs
   prior runs at the same primitive_version)

``formula_hash`` covers the feature-inlined, argument-canonicalized signal
AST plus the canonical regime_filter and horizon (renamed-but-identical
formulas collide; same signal at a different horizon or regime does NOT —
those are distinct candidates under eval_v1).
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass

import msgspec

from research.candidate_loop import grammar
from research.candidate_loop.grammar import (
    BinOp,
    Call,
    Compare,
    FormulaSyntaxError,
    Identifier,
    Node,
    Number,
    String,
    UnaryOp,
)
from research.candidate_loop.schema import (
    CANDIDATE_NAME_RE,
    EVENT_WINDOW_MAX,
    EVENT_WINDOW_MIN,
    FAMILIES,
    FEATURE_NAME_RE,
    HORIZON_EVENT_MAX,
    HORIZON_EVENT_MIN,
    HORIZON_TIME_MAX_NS,
    HORIZON_TIME_MIN_NS,
    HYPOTHESIS_MAX_CHARS,
    HYPOTHESIS_MIN_CHARS,
    LABEL_PRIMITIVE,
    MAX_FEATURES,
    PRIMITIVE_SIGNATURES,
    PRIMITIVE_VERSION,
    SIDES,
    TIME_WINDOW_MAX_NS,
    TIME_WINDOW_MIN_NS,
    TRANSFORM_DEFAULTS,
    TRANSFORM_SIGNATURES,
    Candidate,
    DeathReason,
    compute_alpha_id,
    parse_window_spec,
)

MAX_SIGNAL_NODES = 64
MAX_CALL_DEPTH = 3

_ALL_SIGNATURES = {**PRIMITIVE_SIGNATURES, **TRANSFORM_SIGNATURES}


class _Violation(Exception):
    """Internal control flow: carries (death_reason, detail) up the stack."""

    def __init__(self, reason: DeathReason, detail: str) -> None:
        super().__init__(detail)
        self.reason = reason
        self.detail = detail


@dataclass(frozen=True)
class ValidCandidate:
    candidate: Candidate
    alpha_id: str
    formula_hash: str
    signal_ast: Node  # feature-inlined, argument-canonicalized
    regime_ast: Node | None  # canonicalized Compare, or None when always-on
    uses_trade_imbalance: bool
    signal_node_count: int
    raw_json: str


@dataclass(frozen=True)
class InvalidCandidate:
    death_reason: DeathReason
    detail: str
    raw_json: str
    alpha_id: str = ""  # set when the line at least decoded
    candidate: Candidate | None = None


ValidationResult = ValidCandidate | InvalidCandidate


# ---------------------------------------------------------------------------
# Stage 1: schema / field validation.
# ---------------------------------------------------------------------------


def _decode(raw: str) -> Candidate:
    try:
        return msgspec.json.decode(raw, type=Candidate)
    except msgspec.ValidationError as exc:
        raise _Violation(DeathReason.SCHEMA_INVALID, f"decode: {exc}") from exc
    except msgspec.DecodeError as exc:
        raise _Violation(DeathReason.SCHEMA_INVALID, f"not valid JSON: {exc}") from exc


def _validate_fields(cand: Candidate) -> None:
    if not CANDIDATE_NAME_RE.match(cand.name):
        raise _Violation(DeathReason.SCHEMA_INVALID, f"name {cand.name!r} fails {CANDIDATE_NAME_RE.pattern}")
    if cand.family not in FAMILIES:
        raise _Violation(DeathReason.SCHEMA_INVALID, f"family {cand.family!r} not in {sorted(FAMILIES)}")
    if not (HYPOTHESIS_MIN_CHARS <= len(cand.hypothesis) <= HYPOTHESIS_MAX_CHARS):
        raise _Violation(
            DeathReason.SCHEMA_INVALID,
            f"hypothesis length {len(cand.hypothesis)} outside "
            f"[{HYPOTHESIS_MIN_CHARS},{HYPOTHESIS_MAX_CHARS}]",
        )
    if not cand.features:
        raise _Violation(DeathReason.SCHEMA_INVALID, "features must have at least 1 entry")
    if len(cand.features) > MAX_FEATURES:
        raise _Violation(
            DeathReason.OVER_COMPLEX,
            f"{len(cand.features)} features > {MAX_FEATURES}",
        )
    seen_names: set[str] = set()
    for feat in cand.features:
        if not FEATURE_NAME_RE.match(feat.name):
            raise _Violation(
                DeathReason.SCHEMA_INVALID, f"feature name {feat.name!r} fails {FEATURE_NAME_RE.pattern}"
            )
        if feat.name in seen_names:
            raise _Violation(DeathReason.SCHEMA_INVALID, f"duplicate feature name {feat.name!r}")
        seen_names.add(feat.name)
    if cand.expected_sign not in ("positive", "negative"):
        raise _Violation(
            DeathReason.SCHEMA_INVALID, f"expected_sign {cand.expected_sign!r} not positive|negative"
        )
    try:
        parse_window_spec(cand.horizon)
    except ValueError as exc:
        raise _Violation(DeathReason.SCHEMA_INVALID, f"horizon: {exc}") from exc
    _validate_label_format(cand)
    for prop in cand.proposed_new_primitives:
        if not prop.not_executable_in_v1:
            raise _Violation(
                DeathReason.SCHEMA_INVALID,
                f"proposed_new_primitives[{prop.name!r}].not_executable_in_v1 must be true",
            )


def _validate_label_format(cand: Candidate) -> None:
    """label must be exactly ``future_mid_return(horizon=<candidate.horizon>)``."""
    try:
        node = grammar.parse(cand.label)
    except FormulaSyntaxError as exc:
        raise _Violation(DeathReason.SCHEMA_INVALID, f"label does not parse: {exc}") from exc
    bad = _Violation(
        DeathReason.SCHEMA_INVALID,
        f"label must be exactly {LABEL_PRIMITIVE}(horizon='<horizon>'), got {cand.label!r}",
    )
    if not isinstance(node, Call) or node.name != LABEL_PRIMITIVE:
        raise bad
    if node.args and node.kwargs:
        raise bad
    if len(node.args) == 1 and not node.kwargs:
        horizon_node = node.args[0]
    elif not node.args and len(node.kwargs) == 1 and node.kwargs[0][0] == "horizon":
        horizon_node = node.kwargs[0][1]
    else:
        raise bad
    if not isinstance(horizon_node, String):
        raise bad
    if horizon_node.value != cand.horizon:
        raise _Violation(
            DeathReason.SCHEMA_INVALID,
            f"label horizon {horizon_node.value!r} != candidate horizon {cand.horizon!r}",
        )


# ---------------------------------------------------------------------------
# Stage 2: formula parsing.
# ---------------------------------------------------------------------------


def _parse_formulas(cand: Candidate) -> tuple[dict[str, Node], Node, Node | None]:
    feature_asts: dict[str, Node] = {}
    for feat in cand.features:
        try:
            feature_asts[feat.name] = grammar.parse(feat.formula)
        except FormulaSyntaxError as exc:
            raise _Violation(
                DeathReason.FORMULA_PARSE_ERROR, f"feature {feat.name!r}: {exc}"
            ) from exc
    try:
        signal_ast = grammar.parse(cand.signal_formula)
    except FormulaSyntaxError as exc:
        raise _Violation(DeathReason.FORMULA_PARSE_ERROR, f"signal_formula: {exc}") from exc
    regime_ast: Node | None = None
    if cand.regime_filter:
        try:
            regime_ast = grammar.parse(cand.regime_filter, allow_compare=True)
        except FormulaSyntaxError as exc:
            raise _Violation(DeathReason.FORMULA_PARSE_ERROR, f"regime_filter: {exc}") from exc
        if not isinstance(regime_ast, Compare):
            raise _Violation(
                DeathReason.FORMULA_PARSE_ERROR,
                "regime_filter must be exactly one top-level comparison",
            )
    return feature_asts, signal_ast, regime_ast


# ---------------------------------------------------------------------------
# Stage 3: call names + references.
# ---------------------------------------------------------------------------


def _check_names(node: Node, allowed_idents: frozenset[str], proposed: frozenset[str], ctx: str) -> None:
    """Whitelist Call names and expression-position identifiers.

    Call argument values are NOT treated as references (bare ``bid``/``ask``
    live there); only transform ``x`` arguments re-enter expression context.
    """
    if isinstance(node, Identifier):
        if node.name not in allowed_idents:
            raise _Violation(
                DeathReason.PRIMITIVE_INVALID, f"{ctx}: unknown reference {node.name!r}"
            )
        return
    if isinstance(node, (Number, String)):
        return
    if isinstance(node, UnaryOp):
        _check_names(node.operand, allowed_idents, proposed, ctx)
        return
    if isinstance(node, (BinOp, Compare)):
        _check_names(node.left, allowed_idents, proposed, ctx)
        _check_names(node.right, allowed_idents, proposed, ctx)
        return
    if isinstance(node, Call):
        if node.name not in _ALL_SIGNATURES:
            if node.name in proposed:
                raise _Violation(
                    DeathReason.UNSUPPORTED_NEW_PRIMITIVE,
                    f"{ctx}: proposed new primitive {node.name!r} is used in a formula",
                )
            raise _Violation(
                DeathReason.PRIMITIVE_INVALID, f"{ctx}: {node.name!r} is not a prim_v1 primitive/transform"
            )
        if node.name == LABEL_PRIMITIVE:
            raise _Violation(
                DeathReason.PRIMITIVE_INVALID,
                f"{ctx}: {LABEL_PRIMITIVE} is label-only and may not appear in formulas",
            )
        if node.name in TRANSFORM_SIGNATURES:
            # x is expression-position: first positional or kwarg 'x'.
            if node.args:
                _check_names(node.args[0], allowed_idents, proposed, ctx)
            for key, val in node.kwargs:
                if key == "x":
                    _check_names(val, allowed_idents, proposed, ctx)
                for call in grammar.iter_calls(val):
                    _check_call_name_only(call, proposed, ctx)
            for arg in node.args[1:]:
                for call in grammar.iter_calls(arg):
                    _check_call_name_only(call, proposed, ctx)
        else:
            for arg in node.args:
                for call in grammar.iter_calls(arg):
                    _check_call_name_only(call, proposed, ctx)
            for _, val in node.kwargs:
                for call in grammar.iter_calls(val):
                    _check_call_name_only(call, proposed, ctx)
        return
    raise TypeError(f"Unknown node type {type(node).__name__}")  # pragma: no cover


def _check_call_name_only(call: Call, proposed: frozenset[str], ctx: str) -> None:
    if call.name not in _ALL_SIGNATURES:
        if call.name in proposed:
            raise _Violation(
                DeathReason.UNSUPPORTED_NEW_PRIMITIVE,
                f"{ctx}: proposed new primitive {call.name!r} is used in a formula",
            )
        raise _Violation(
            DeathReason.PRIMITIVE_INVALID, f"{ctx}: {call.name!r} is not a prim_v1 primitive/transform"
        )
    if call.name == LABEL_PRIMITIVE:
        raise _Violation(
            DeathReason.PRIMITIVE_INVALID,
            f"{ctx}: {LABEL_PRIMITIVE} is label-only and may not appear in formulas",
        )


# ---------------------------------------------------------------------------
# Stage 4: argument domains + canonicalization.
# ---------------------------------------------------------------------------


def _canonicalize(node: Node, ctx: str) -> Node:
    """Validate argument domains and return a canonical AST.

    Canonical form: kwargs resolved into signature order, defaults filled,
    sides lowercased to String, windows normalized to ``'N_events'`` /
    ``'<ns>ns'``, unary +/- folded into Numbers.
    """
    if isinstance(node, (Identifier, Number, String)):
        return node
    if isinstance(node, UnaryOp):
        operand = _canonicalize(node.operand, ctx)
        if node.op == "+":
            return operand
        if isinstance(operand, Number):
            return Number(-operand.value)
        return UnaryOp("-", operand)
    if isinstance(node, BinOp):
        return BinOp(node.op, _canonicalize(node.left, ctx), _canonicalize(node.right, ctx))
    if isinstance(node, Compare):
        return Compare(node.op, _canonicalize(node.left, ctx), _canonicalize(node.right, ctx))
    if isinstance(node, Call):
        return _canonicalize_call(node, ctx)
    raise TypeError(f"Unknown node type {type(node).__name__}")  # pragma: no cover


def _canonicalize_call(node: Call, ctx: str) -> Call:
    sig = _ALL_SIGNATURES[node.name]
    bound: dict[str, Node] = {}
    if len(node.args) > len(sig):
        raise _Violation(
            DeathReason.ARGUMENT_INVALID,
            f"{ctx}: {node.name} takes {len(sig)} args, got {len(node.args)} positional",
        )
    for param, arg in zip(sig, node.args):
        bound[param] = arg
    for key, val in node.kwargs:
        if key not in sig:
            raise _Violation(DeathReason.ARGUMENT_INVALID, f"{ctx}: {node.name} has no parameter {key!r}")
        if key in bound:
            raise _Violation(DeathReason.ARGUMENT_INVALID, f"{ctx}: {node.name} duplicate argument {key!r}")
        bound[key] = val
    for param, default in TRANSFORM_DEFAULTS.get(node.name, {}).items():
        bound.setdefault(param, String(default))
    missing = [p for p in sig if p not in bound]
    if missing:
        raise _Violation(
            DeathReason.ARGUMENT_INVALID, f"{ctx}: {node.name} missing argument(s) {missing}"
        )

    canon: list[Node] = []
    for param in sig:
        canon.append(_canonicalize_arg(node.name, param, bound[param], ctx))
    return Call(node.name, tuple(canon), ())


def _canonicalize_arg(fn: str, param: str, value: Node, ctx: str) -> Node:
    where = f"{ctx}: {fn}({param}=...)"
    if param == "x":
        return _canonicalize(value, ctx)
    if param == "side":
        if isinstance(value, Identifier):
            value = String(value.name)
        if not isinstance(value, String) or value.value.lower() not in SIDES:
            raise _Violation(DeathReason.ARGUMENT_INVALID, f"{where} side must be 'bid'|'ask'")
        return String(value.value.lower())
    if param == "levels":
        value = _fold_number(value)
        if not isinstance(value, Number) or value.value != int(value.value):
            raise _Violation(DeathReason.ARGUMENT_INVALID, f"{where} levels must be an integer literal")
        levels = int(value.value)
        if not (1 <= levels <= 5):
            raise _Violation(DeathReason.ARGUMENT_INVALID, f"{where} levels {levels} outside 1..5")
        return Number(float(levels))
    if param == "window":
        return String(_canonical_window(value, where, is_horizon=False))
    if param == "horizon":
        return String(_canonical_window(value, where, is_horizon=True))
    if param in ("lo", "hi"):
        value = _fold_number(value)
        if not isinstance(value, Number):
            raise _Violation(DeathReason.ARGUMENT_INVALID, f"{where} must be a numeric literal")
        return value
    raise TypeError(f"Unknown parameter {param!r}")  # pragma: no cover


def _fold_number(value: Node) -> Node:
    if isinstance(value, UnaryOp) and isinstance(value.operand, Number):
        return Number(-value.operand.value) if value.op == "-" else value.operand
    return value


def _canonical_window(value: Node, where: str, *, is_horizon: bool) -> str:
    if not isinstance(value, String):
        raise _Violation(
            DeathReason.ARGUMENT_INVALID, f"{where} must be a string like '2000_events'|'500ms'|'5s'"
        )
    try:
        win = parse_window_spec(value.value)
    except ValueError as exc:
        raise _Violation(DeathReason.ARGUMENT_INVALID, f"{where}: {exc}") from exc
    if win.kind == "events":
        lo, hi = (
            (HORIZON_EVENT_MIN, HORIZON_EVENT_MAX)
            if is_horizon
            else (EVENT_WINDOW_MIN, EVENT_WINDOW_MAX)
        )
        if not (lo <= win.count <= hi):
            raise _Violation(
                DeathReason.ARGUMENT_INVALID,
                f"{where} event count {win.count} outside [{lo},{hi}]",
            )
        return f"{win.count}_events"
    lo_ns, hi_ns = (
        (HORIZON_TIME_MIN_NS, HORIZON_TIME_MAX_NS)
        if is_horizon
        else (TIME_WINDOW_MIN_NS, TIME_WINDOW_MAX_NS)
    )
    if not (lo_ns <= win.duration_ns <= hi_ns):
        raise _Violation(
            DeathReason.ARGUMENT_INVALID,
            f"{where} duration {win.duration_ns}ns outside [{lo_ns},{hi_ns}]ns",
        )
    return f"{win.duration_ns}ns"


def _validate_clip_bounds(node: Node, ctx: str) -> None:
    for call in grammar.iter_calls(node):
        if call.name == "clip" and len(call.args) == 3:
            lo, hi = call.args[1], call.args[2]
            if isinstance(lo, Number) and isinstance(hi, Number) and not (lo.value < hi.value):
                raise _Violation(
                    DeathReason.ARGUMENT_INVALID, f"{ctx}: clip lo {lo.value} must be < hi {hi.value}"
                )


# ---------------------------------------------------------------------------
# Stage 5: feature inlining + complexity.
# ---------------------------------------------------------------------------


def _inline(node: Node, features: dict[str, Node]) -> Node:
    if isinstance(node, Identifier):
        return features.get(node.name, node)
    if isinstance(node, (Number, String)):
        return node
    if isinstance(node, UnaryOp):
        return UnaryOp(node.op, _inline(node.operand, features))
    if isinstance(node, BinOp):
        return BinOp(node.op, _inline(node.left, features), _inline(node.right, features))
    if isinstance(node, Compare):
        return Compare(node.op, _inline(node.left, features), _inline(node.right, features))
    if isinstance(node, Call):
        return Call(
            node.name,
            tuple(_inline(a, features) for a in node.args),
            tuple((k, _inline(v, features)) for k, v in node.kwargs),
        )
    raise TypeError(f"Unknown node type {type(node).__name__}")  # pragma: no cover


# ---------------------------------------------------------------------------
# Stage 6: formula hash.
# ---------------------------------------------------------------------------


def canonical_ast_str(node: Node) -> str:
    """Deterministic S-expression of a canonicalized AST."""
    if isinstance(node, Identifier):
        return node.name
    if isinstance(node, Number):
        v = node.value
        return str(int(v)) if v == int(v) else repr(v)
    if isinstance(node, String):
        return f"'{node.value}'"
    if isinstance(node, UnaryOp):
        return f"(neg {canonical_ast_str(node.operand)})"
    if isinstance(node, (BinOp, Compare)):
        return f"({node.op} {canonical_ast_str(node.left)} {canonical_ast_str(node.right)})"
    if isinstance(node, Call):
        parts = " ".join(canonical_ast_str(a) for a in node.args)
        return f"({node.name} {parts})" if parts else f"({node.name})"
    raise TypeError(f"Unknown node type {type(node).__name__}")  # pragma: no cover


def compute_formula_hash(signal_ast: Node, regime_ast: Node | None, horizon: str) -> str:
    """Dedupe key over (prim_v1, inlined signal, regime, horizon)."""
    canon_horizon = _canonical_window(String(horizon), "horizon", is_horizon=True)
    payload = (
        f"{PRIMITIVE_VERSION}|signal={canonical_ast_str(signal_ast)}"
        f"|regime={canonical_ast_str(regime_ast) if regime_ast is not None else ''}"
        f"|horizon={canon_horizon}"
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]


# ---------------------------------------------------------------------------
# Entry points.
# ---------------------------------------------------------------------------


def validate_line(
    raw: str,
    seen_hashes: set[str],
    prior_hashes: frozenset[str] = frozenset(),
) -> ValidationResult:
    """Validate one JSONL line. Mutates ``seen_hashes`` on success."""
    alpha_id = ""
    cand: Candidate | None = None
    try:
        cand = _decode(raw)
        alpha_id = compute_alpha_id(cand)
        _validate_fields(cand)
        feature_asts, signal_ast, regime_ast = _parse_formulas(cand)

        proposed = frozenset(p.name for p in cand.proposed_new_primitives)
        feature_names = frozenset(feature_asts)
        for feat in cand.features:
            _check_names(feature_asts[feat.name], frozenset(), proposed, f"feature {feat.name!r}")
        _check_names(signal_ast, feature_names, proposed, "signal_formula")
        if regime_ast is not None:
            _check_names(regime_ast, feature_names, proposed, "regime_filter")

        canon_features = {
            name: _canonicalize(ast, f"feature {name!r}") for name, ast in feature_asts.items()
        }
        canon_signal = _canonicalize(signal_ast, "signal_formula")
        canon_regime = _canonicalize(regime_ast, "regime_filter") if regime_ast is not None else None
        for name, ast in canon_features.items():
            _validate_clip_bounds(ast, f"feature {name!r}")
        _validate_clip_bounds(canon_signal, "signal_formula")
        # Horizon range check (format already passed in stage 1).
        _canonical_window(String(cand.horizon), "horizon", is_horizon=True)

        inlined_signal = _inline(canon_signal, canon_features)
        inlined_regime = _inline(canon_regime, canon_features) if canon_regime is not None else None
        n_nodes = grammar.node_count(inlined_signal)
        if n_nodes > MAX_SIGNAL_NODES:
            raise _Violation(
                DeathReason.OVER_COMPLEX, f"inlined signal AST has {n_nodes} nodes > {MAX_SIGNAL_NODES}"
            )
        depth = grammar.call_depth(inlined_signal)
        if depth > MAX_CALL_DEPTH:
            raise _Violation(DeathReason.OVER_COMPLEX, f"call depth {depth} > {MAX_CALL_DEPTH}")

        formula_hash = compute_formula_hash(inlined_signal, inlined_regime, cand.horizon)
        if formula_hash in seen_hashes or formula_hash in prior_hashes:
            raise _Violation(
                DeathReason.DUPLICATE_ALPHA,
                f"formula_hash {formula_hash} already seen at {PRIMITIVE_VERSION}",
            )
        seen_hashes.add(formula_hash)

        uses_ti = any(
            c.name == "trade_imbalance"
            for ast in (inlined_signal, inlined_regime)
            if ast is not None
            for c in grammar.iter_calls(ast)
        )
        return ValidCandidate(
            candidate=cand,
            alpha_id=alpha_id,
            formula_hash=formula_hash,
            signal_ast=inlined_signal,
            regime_ast=inlined_regime,
            uses_trade_imbalance=uses_ti,
            signal_node_count=n_nodes,
            raw_json=raw,
        )
    except _Violation as v:
        return InvalidCandidate(
            death_reason=v.reason, detail=v.detail, raw_json=raw, alpha_id=alpha_id, candidate=cand
        )


def validate_batch(
    lines: list[str], prior_hashes: frozenset[str] = frozenset()
) -> list[ValidationResult]:
    """Validate a batch in order; within-batch dedupe is first-come-wins."""
    seen: set[str] = set()
    return [validate_line(line, seen, prior_hashes) for line in lines]


__all__ = [
    "MAX_CALL_DEPTH",
    "MAX_SIGNAL_NODES",
    "InvalidCandidate",
    "ValidCandidate",
    "ValidationResult",
    "canonical_ast_str",
    "compute_formula_hash",
    "validate_batch",
    "validate_line",
]
