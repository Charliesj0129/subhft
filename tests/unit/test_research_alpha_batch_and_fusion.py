from __future__ import annotations

import numpy as np

from research.alphas.garch_vol.impl import GARCHVolAlpha
from research.alphas.kl_regime.impl import KLRegimeAlpha
from research.alphas.ofi_mc.impl import OFIMCAlpha
from research.combinatorial.expression_lang import compile_expression


def _run_row(alpha, rows, fields: list[str]) -> np.ndarray:
    out = np.zeros(len(rows), dtype=np.float64)
    alpha.reset()
    for i, row in enumerate(rows):
        payload = {k: row[k] for k in fields if k in row.dtype.names}
        out[i] = float(alpha.update(**payload))
    return out


def test_garch_vol_update_batch_matches_row():
    n = 64
    arr = np.zeros(n, dtype=[("price", "f8")])
    arr["price"] = 100 + np.linspace(0, 1, n)
    alpha = GARCHVolAlpha()
    row = _run_row(alpha, arr, ["price"])
    alpha.reset()
    batch = np.asarray(alpha.update_batch(arr), dtype=np.float64)
    assert np.allclose(batch, row, equal_nan=True)


def test_kl_regime_update_batch_matches_row():
    n = 128
    arr = np.zeros(n, dtype=[("current_return", "f8")])
    arr["current_return"] = np.sin(np.linspace(0, 10, n)) * 1e-3
    alpha = KLRegimeAlpha(window_recent=8, window_ref=24, n_bins=8)
    row = _run_row(alpha, arr, ["current_return"])
    alpha.reset()
    batch = np.asarray(alpha.update_batch(arr), dtype=np.float64)
    assert np.allclose(batch, row, equal_nan=True)


def test_ofi_mc_update_batch_matches_row():
    n = 64
    arr = np.zeros(
        n,
        dtype=[
            ("bid_px", "f8"),
            ("bid_qty", "f8"),
            ("ask_px", "f8"),
            ("ask_qty", "f8"),
            ("trade_vol", "f8"),
            ("current_mid", "f8"),
        ],
    )
    arr["bid_px"] = 100 + np.linspace(0, 0.3, n)
    arr["ask_px"] = arr["bid_px"] + 0.01
    arr["bid_qty"] = 10 + (np.arange(n) % 5)
    arr["ask_qty"] = 11 + (np.arange(n) % 3)
    arr["trade_vol"] = 1 + (np.arange(n) % 4)
    arr["current_mid"] = (arr["bid_px"] + arr["ask_px"]) / 2.0

    alpha = OFIMCAlpha()
    row = _run_row(alpha, arr, ["bid_px", "bid_qty", "ask_px", "ask_qty", "trade_vol", "current_mid"])
    alpha.reset()
    batch = np.asarray(alpha.update_batch(arr), dtype=np.float64)
    assert np.allclose(batch, row, equal_nan=True)


def test_expression_fusion_fast_paths_match_generic():
    features = {
        "ofi": np.linspace(-1, 1, 200, dtype=np.float64),
        "bid_qty": np.sin(np.linspace(0, 6, 200)).astype(np.float64),
    }
    expr1 = compile_expression("zscore(ts_delta(ofi, 3), 5)")
    expr2 = compile_expression("sign(ts_corr(ofi, bid_qty, 10))")
    assert expr1.fast_path is not None
    assert expr2.fast_path is not None

    # Compile equivalent generic path by defeating fusion shape
    generic1 = compile_expression("zscore((ts_delta(ofi, 3)), 5)")
    generic1 = generic1.__class__(generic1.expression, generic1.tree, generic1.max_depth, generic1.variables, None)
    generic2 = compile_expression("sign((ts_corr(ofi, bid_qty, 10)))")
    generic2 = generic2.__class__(generic2.expression, generic2.tree, generic2.max_depth, generic2.variables, None)

    out_fast1 = expr1.evaluate(features)
    out_generic1 = generic1.evaluate(features)
    out_fast2 = expr2.evaluate(features)
    out_generic2 = generic2.evaluate(features)
    assert np.allclose(out_fast1, out_generic1, equal_nan=True)
    assert np.allclose(out_fast2, out_generic2, equal_nan=True)
