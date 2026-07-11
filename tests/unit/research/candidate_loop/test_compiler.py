"""Compiler tree-walk over validator-canonical ASTs (spec §7/§10)."""

from __future__ import annotations

import json

import numpy as np
import pytest

from research.candidate_loop.compiler import (
    CompileError,
    evaluate_label,
    evaluate_regime,
    evaluate_signal,
)
from research.candidate_loop.grammar import Call, Compare, Identifier, Number, String
from research.candidate_loop.validator import ValidCandidate, validate_line


def _signal_ast(signal_formula: str, features: list[dict] | None = None, **overrides: object):
    """Run a candidate through the real validator and return its canonical AST."""
    base: dict = {
        "name": "compiler_probe",
        "family": "order_book_imbalance",
        "hypothesis": "Synthetic-panel probe candidate for compiler unit tests.",
        "features": features or [{"name": "imb_l1", "formula": "book_imbalance(1)"}],
        "signal_formula": signal_formula,
        "label": "future_mid_return(horizon='1s')",
        "horizon": "1s",
        "expected_sign": "positive",
    }
    base.update(overrides)
    result = validate_line(json.dumps(base), seen_hashes=set())
    assert isinstance(result, ValidCandidate), getattr(result, "detail", "")
    return result


def _cols(n: int = 4) -> dict[str, np.ndarray]:
    sec = 1_000_000_000
    local_ts = np.arange(n, dtype=np.int64) * sec
    cols: dict[str, np.ndarray] = {
        "exch_ts": local_ts.copy(),
        "local_ts": local_ts,
        "mid": np.linspace(100.0, 100.0 + n - 1, n),
        "microprice": np.full(n, 100.25),
        "spread_ticks": np.array([1.0, 2.0, 3.0, np.nan][:n]),
        "trade_buy_qty": np.cumsum(np.full(n, 2.0)),
        "trade_sell_qty": np.cumsum(np.full(n, 1.0)),
    }
    for lvl in range(1, 6):
        cols[f"bid_qty_{lvl}"] = np.full(n, float(lvl))
        cols[f"ask_qty_{lvl}"] = np.full(n, 2.0 * lvl)
        cols[f"bid_px_{lvl}"] = np.full(n, 100.0 - lvl)
        cols[f"ask_px_{lvl}"] = np.full(n, 100.0 + lvl)
    return cols


class TestSignalEvaluation:
    def test_book_imbalance_through_validated_ast(self) -> None:
        valid = _signal_ast("imb_l1")
        out = evaluate_signal(valid.signal_ast, _cols())
        # bid 1 vs ask 2 -> (1-2)/(1+2)
        np.testing.assert_allclose(out, np.full(4, -1.0 / 3.0))

    def test_arithmetic_over_primitives(self) -> None:
        valid = _signal_ast("mid_price() - microprice()", features=[{"name": "unused", "formula": "mid_price()"}])
        out = evaluate_signal(valid.signal_ast, _cols())
        np.testing.assert_allclose(out, np.linspace(100.0, 103.0, 4) - 100.25)

    def test_safe_divide_in_formula(self) -> None:
        cols = _cols()
        cols["bid_qty_1"] = np.array([0.0, 4.0, 4.0, 4.0])
        valid = _signal_ast("spread_ticks() / depth_sum('bid', 1)")
        out = evaluate_signal(valid.signal_ast, cols)
        assert out[0] == 0.0  # zero depth -> safe divide
        assert out[1] == pytest.approx(0.5)

    def test_unary_minus(self) -> None:
        valid = _signal_ast("-imb_l1")
        out = evaluate_signal(valid.signal_ast, _cols())
        np.testing.assert_allclose(out, np.full(4, 1.0 / 3.0))

    def test_negative_zscore_is_negated_zscore(self) -> None:
        cols = _cols()
        cols["bid_qty_1"] = np.array([1.0, 2.0, 4.0, 8.0])
        pos = _signal_ast("zscore(imb_l1, window='10_events')")
        neg = _signal_ast("negative_zscore(imb_l1, window='10_events')")
        z = evaluate_signal(pos.signal_ast, cols)
        nz = evaluate_signal(neg.signal_ast, cols)
        np.testing.assert_allclose(nz, -z)

    def test_time_window_survives_canonical_ns_format(self) -> None:
        # Validator canonicalizes '1s' -> '1000000000ns'; compiler must parse it.
        valid = _signal_ast(
            "dd_bid",
            features=[{"name": "dd_bid", "formula": "depth_delta('bid', 1, '1s')"}],
        )
        out = evaluate_signal(valid.signal_ast, _cols())
        assert np.isnan(out[0])
        np.testing.assert_allclose(out[1:], np.zeros(3))  # constant book

    def test_clip_in_compiled_formula(self) -> None:
        valid = _signal_ast("clip(imb_l1 * 9, -1, 1)")
        out = evaluate_signal(valid.signal_ast, _cols())
        np.testing.assert_allclose(out, np.full(4, -1.0))

    def test_scalar_formula_broadcasts_to_panel_length(self) -> None:
        out = evaluate_signal(Number(2.5), _cols())
        np.testing.assert_allclose(out, np.full(4, 2.5))

    def test_trade_imbalance_via_compiler(self) -> None:
        valid = _signal_ast(
            "tf_imb",
            features=[{"name": "tf_imb", "formula": "trade_imbalance('1s')"}],
            family="trade_flow",
        )
        out = evaluate_signal(valid.signal_ast, _cols())
        assert np.isnan(out[0])
        np.testing.assert_allclose(out[1:], np.full(3, 1.0 / 3.0))

    def test_ema_via_compiler(self) -> None:
        valid = _signal_ast("ema(imb_l1, '10_events')")
        out = evaluate_signal(valid.signal_ast, _cols())
        np.testing.assert_allclose(out, np.full(4, -1.0 / 3.0))


class TestRegimeEvaluation:
    def test_regime_mask_with_nan_false(self) -> None:
        valid = _signal_ast("imb_l1", regime_filter="spread_ticks() <= 2")
        assert valid.regime_ast is not None
        mask = evaluate_regime(valid.regime_ast, _cols())
        # spreads 1,2,3,NaN -> True,True,False,False
        np.testing.assert_array_equal(mask, [True, True, False, False])

    @pytest.mark.parametrize(
        ("formula", "expected"),
        [
            ("spread_ticks() >= 2", [False, True, True, False]),
            ("spread_ticks() < 2", [True, False, False, False]),
            ("spread_ticks() > 2", [False, False, True, False]),
            ("spread_ticks() == 2", [False, True, False, False]),
        ],
    )
    def test_all_comparison_operators(self, formula: str, expected: list[bool]) -> None:
        valid = _signal_ast("imb_l1", regime_filter=formula)
        mask = evaluate_regime(valid.regime_ast, _cols())
        np.testing.assert_array_equal(mask, expected)

    def test_non_compare_root_raises(self) -> None:
        with pytest.raises(CompileError, match="Compare"):
            evaluate_regime(Number(1.0), _cols())


class TestLabel:
    def test_label_matches_future_mid_return_semantics(self) -> None:
        cols = _cols()
        out = evaluate_label(cols, "1s")
        np.testing.assert_allclose(out[:3], [101.0 / 100.0 - 1, 102.0 / 101.0 - 1, 103.0 / 102.0 - 1])
        assert np.isnan(out[3])

    def test_event_horizon_label(self) -> None:
        out = evaluate_label(_cols(), "2_events")
        assert np.isnan(out[2]) and np.isnan(out[3])


class TestDefensiveErrors:
    def test_unresolved_identifier_raises(self) -> None:
        with pytest.raises(CompileError, match="unresolved identifier"):
            evaluate_signal(Identifier("ghost"), _cols())

    def test_future_mid_return_in_signal_raises(self) -> None:
        node = Call("future_mid_return", (String("1s"),), ())
        with pytest.raises(CompileError, match="label-only"):
            evaluate_signal(node, _cols())

    def test_compare_in_signal_position_raises(self) -> None:
        node = Compare("<", Number(1.0), Number(2.0))
        with pytest.raises(CompileError, match="regime"):
            evaluate_signal(node, _cols())

    def test_string_in_expression_position_raises(self) -> None:
        with pytest.raises(CompileError, match="string literal"):
            evaluate_signal(String("1s"), _cols())

    def test_unknown_call_raises(self) -> None:
        with pytest.raises(CompileError, match="not a prim_v1"):
            evaluate_signal(Call("vwap", (), ()), _cols())
