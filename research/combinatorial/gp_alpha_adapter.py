"""Streaming adapter bridging GP-discovered expressions into ``AlphaProtocol``.

``research/combinatorial/expression_lang.py::CompiledExpression.evaluate()`` is
a *batch* interface: it consumes whole numpy arrays and returns a whole numpy
array — the calling convention the search engine used to score/select a
candidate. The platform's real signal-execution path
(``research/backtest/alpha_strategy_bridge.py`` -> ``AlphaProtocol.update()``)
is *streaming*: it calls ``update()`` tick-by-tick with one payload dict per
event and expects one ``float`` back.

``GPCompiledAlpha`` bridges the two by keeping one bounded rolling buffer
(``collections.deque``) per input variable and recomputing
``CompiledExpression.evaluate()`` over the buffer on every tick, returning the
buffer's last output element. This is provably identical, index for index, to
calling ``evaluate()`` once on the full batch array — *provided the buffer is
long enough to hold every sample each windowed operator in the expression
needs to reproduce its batch output at that index*.

Sizing that buffer correctly is the one non-obvious part of this module.

**Naive-but-wrong approach**: buffer length = the single largest window
constant found anywhere in the expression (e.g. ``max(w1, w2)`` for
``zscore(ts_delta(x, w1), w2)``). This under-sizes whenever windowed
operators nest, because required history *accumulates additively* down the
tree — the ``zscore`` needs its last ``w2`` inputs correct, and *each of
those* ``ts_delta`` outputs itself needs a value ``w1`` samples further back,
so reproducing ``zscore``'s single output value at the newest tick actually
requires ``w1 + w2`` trailing ``x`` samples, not ``max(w1, w2)``.
Verified by hand: for ``x = [0,0,0,0,100,0,0,0,10]``, ``w1=2, w2=3``, the
correct (full-batch) output at the last index is ``≈0.805``; a buffer sized
``max(2,3)=3`` produces ``≈1.414`` instead — a silent, wrong divergence from
the score the candidate was actually selected on. ``max_window_for_expression``
below instead walks the tree accumulating required history additively
(``_required_history``), which reproduces ``≈0.805`` exactly with a buffer of
``w1+w2=5`` (verified in ``tests/unit/test_combinatorial_gp_alpha_adapter.py``).

**``ts_delta`` needs one further correction.** Every other windowed operator
in ``operator_library.py`` (``ts_mean``, ``ts_std``, ``ts_sum``, ``ts_rank``,
``decay_linear``, ``ts_corr``, windowed ``zscore``) is a true rolling window:
``target[i]`` reads ``arr[max(0, i-w+1):i+1]`` — ``w`` *trailing* samples. To
reproduce ``k`` consecutive outputs ending at ``i``, that needs ``k+(w-1)``
trailing inputs. ``ts_delta`` is different: ``target[i] = arr[i] - arr[i-w]``,
a fixed *offset* difference that skips every sample in between — reproducing
``k`` outputs needs ``k+w`` trailing inputs (one more than the rolling-window
formula), because it needs the sample exactly ``w`` bars behind the oldest of
the ``k`` requested outputs, not just ``w-1`` behind it.

**``ts_corr`` and rolling ``zscore`` have a higher internal window floor.**
``operator_library.ts_corr`` and the 2-arg (rolling) form of
``operator_library.zscore`` both clamp ``w = max(2, int(window))`` internally
(a correlation/variance needs at least 2 samples), unlike every other
windowed operator's ``w = max(1, int(window))``. Buffer sizing must mirror
that per-operator floor — using a uniform ``max(1, ...)`` for these two ops
undersizes the buffer by one whenever ``window=1`` is requested, silently
diverging from the batch score (verified: ``ts_corr(x, y, 1)`` and
``zscore(x, 1)`` both stream a constant ``0.0`` instead of the correct
value under a naive ``max(1, ...)`` floor). See ``_MIN_OWN_WINDOW`` below.

**Two operators cannot be streamed via any bounded buffer at all**, and are
rejected outright (``ValueError``) rather than silently mis-adapted:

- ``rank(x)`` (bare, no window arg): ``operator_library.rank`` computes a
  whole-array cross-sectional percentile via a single ``np.argsort`` over the
  *entire* input array — ``target[i]`` depends on every other element,
  including ones after ``i``. There is no bounded trailing buffer that
  reproduces "percentile within the full selection-set array" online. This is
  not a hypothetical: the search engine's own ``_random_expression`` "volume"
  family generates bare ``rank(...)`` by default.
- ``zscore(x)`` called with only one argument (``window=None``): normalizes
  against the whole-array mean/std — same look-ahead problem. Grammar-valid
  per ``OPERATOR_ARITY["zscore"] = (1, 2)``.

Both are caught by ``_uses_whole_array_lookahead`` before any buffer-sizing
work happens, so this raises at ``GPCompiledAlpha`` construction time (and at
``promote.promote_candidate`` time, which also calls
``max_window_for_expression`` for complexity classification) rather than
producing an adapter that quietly diverges from the discovery-time score.
"""

from __future__ import annotations

from collections import deque
from typing import Any

import numpy as np

from hft_platform.contracts.alpha import AlphaManifest
from research.combinatorial.canonical_ast import (
    OPERATOR_ARG_ROLES,
    ConstNode,
    Node,
    OpNode,
    VarNode,
    to_typed_ast,
)
from research.combinatorial.expression_lang import CompiledExpression, compile_expression

# ``ts_delta`` is a fixed-offset difference (``arr[i] - arr[i-w]``), not a
# true rolling window (``arr[i-w+1:i+1]``) — see module docstring. Every other
# windowed operator follows the rolling-window shape.
_OFFSET_WINDOW_OPS = frozenset({"ts_delta"})

# Whole-array (look-ahead) operators that cannot be adapted to any bounded
# streaming buffer — see module docstring.
_WHOLE_ARRAY_LOOKAHEAD_OPS = frozenset({"rank"})

# Per-operator minimum window, mirroring internal clamps in operator_library
# (``ts_corr``/rolling ``zscore`` need >=2 samples for a variance/covariance).
# Ops not listed default to 1, matching operator_library's default floor.
_MIN_OWN_WINDOW: dict[str, int] = {"ts_corr": 2, "zscore": 2}


def max_window_for_expression(expression: str) -> int:
    """Minimum trailing-sample buffer length, per variable, to stream *expression*
    with output identical (index for index) to its batch evaluation.

    Returns the max across all variables (a single conservative buffer length
    reused for every variable — oversizing a buffer is harmless, undersizing
    is the correctness bug this sizing exists to avoid). Returns ``1`` for
    expressions with no windowed operator at all (pure pointwise, O(1) per
    tick) or no variables (constant-only expression).

    Raises ``ValueError`` if *expression* is grammar-invalid (via
    ``to_typed_ast``), uses ``rank(...)`` or a 1-arg ``zscore(...)`` (cannot
    be streamed — see module docstring), or has a non-constant value at a
    "window"-role argument position (grammar-legal per ``to_typed_ast`` but
    not evaluable — ``operator_library`` window params are consumed via
    ``int(window)`` and expect a scalar).
    """
    tree = to_typed_ast(expression)
    if _uses_whole_array_lookahead(tree):
        raise ValueError(
            "Cannot stream expression: uses rank(...) or a 1-arg zscore(...), "
            f"both whole-array look-ahead operators with no bounded-buffer "
            f"streaming equivalent: {expression!r}"
        )
    per_variable: dict[str, int] = {}
    _required_history(tree, 1, per_variable)
    if not per_variable:
        return 1
    return max(per_variable.values())


def _uses_whole_array_lookahead(node: Node) -> bool:
    if isinstance(node, OpNode):
        if node.op in _WHOLE_ARRAY_LOOKAHEAD_OPS:
            return True
        if node.op == "zscore" and len(node.args) < 2:
            return True
        return any(_uses_whole_array_lookahead(a) for a in node.args)
    return False


def _own_window(node: OpNode, arg_roles: tuple[str, ...]) -> int:
    """This operator's own window size; ``1`` if it has no window-role arg (pointwise)."""
    for child, role in zip(node.args, arg_roles[: len(node.args)], strict=True):
        if role == "window":
            if not isinstance(child, ConstNode):
                raise ValueError(
                    f"Window argument for operator '{node.op}' must be a constant, "
                    f"got {type(child).__name__} — not evaluable/streamable."
                )
            floor = _MIN_OWN_WINDOW.get(node.op, 1)
            return max(floor, int(child.value))
    return 1


def _extra_history(op: str, window: int) -> int:
    """Extra trailing input samples (beyond the ``k`` requested outputs) this op's window needs."""
    if op in _OFFSET_WINDOW_OPS:
        return window
    return max(0, window - 1)


def _required_history(node: Node, k: int, out: dict[str, int]) -> None:
    """Accumulate, per variable name, the trailing sample count needed to
    reproduce *k* consecutive batch-evaluated outputs of *node* ending at the
    newest tick. Mutates *out* in place (``max`` across all reaching paths).
    """
    if isinstance(node, VarNode):
        out[node.name] = max(out.get(node.name, 0), k)
        return
    if isinstance(node, ConstNode):
        return
    if isinstance(node, OpNode):
        arg_roles = OPERATOR_ARG_ROLES.get(node.op)
        if arg_roles is None:
            raise ValueError(f"No arg-role definition for operator: {node.op}")
        window = _own_window(node, arg_roles)
        extra = _extra_history(node.op, window)
        for child, role in zip(node.args, arg_roles[: len(node.args)], strict=True):
            if role == "signal":
                _required_history(child, k + extra, out)
        return
    raise TypeError(f"Unknown node type: {type(node).__name__}")  # pragma: no cover - exhaustive union


class GPCompiledAlpha:
    """``AlphaProtocol`` streaming adapter around a compiled GP expression.

    Structurally typed against ``hft_platform.contracts.alpha.AlphaProtocol``
    (a ``runtime_checkable`` ``Protocol``) — deliberately does not subclass
    it, matching the convention of every hand-written ``impl.py`` in
    ``research/alphas/``.
    """

    def __init__(self, expression: str, manifest: AlphaManifest) -> None:
        self._expression = expression
        self._compiled: CompiledExpression = compile_expression(expression)
        self._manifest = manifest
        window = max_window_for_expression(expression)
        self._buffers: dict[str, deque[float]] = {name: deque(maxlen=window) for name in self._compiled.variables}
        self._signal = 0.0

    @property
    def manifest(self) -> AlphaManifest:
        return self._manifest

    def update(self, *args: Any, **kwargs: Any) -> float:
        for name, buf in self._buffers.items():
            raw = kwargs.get(name, 0.0)
            try:
                buf.append(float(raw))
            except (TypeError, ValueError):
                buf.append(0.0)

        features = {name: np.asarray(buf, dtype=np.float64) for name, buf in self._buffers.items()}
        out = self._compiled.evaluate(features)
        self._signal = float(out[-1]) if out.size else 0.0
        return self._signal

    def reset(self) -> None:
        for buf in self._buffers.values():
            buf.clear()
        self._signal = 0.0

    def get_signal(self) -> float:
        return self._signal
