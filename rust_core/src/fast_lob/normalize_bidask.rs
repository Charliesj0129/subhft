#![allow(clippy::too_many_arguments)]

use numpy::{PyArray2, PyArrayMethods, PyReadonlyArray1};
use pyo3::prelude::*;
use pyo3::types::PyList;
use pyo3::types::PyTuple;

use super::scale::{compute_l1_stats, scale_book_pair_stats, scale_side_with_stats};
use super::stats::{extract_ts, get_optional};

#[pyfunction]
pub fn normalize_bidask_tuple(
    py: Python<'_>,
    payload: &Bound<'_, PyAny>,
    symbol: &str,
    scale: i64,
) -> PyResult<PyObject> {
    if symbol.is_empty() {
        return Ok(py.None());
    }

    let ts_obj = get_optional(payload, &["ts", "datetime"]);
    let exch_ts = extract_ts(ts_obj, py)?;

    let bid_prices_obj = get_optional(payload, &["bid_price", "bidPrice"])
        .unwrap_or_else(|| PyList::empty_bound(py).into_py(py));
    let bid_vols_obj = get_optional(payload, &["bid_volume", "bidVolume"])
        .unwrap_or_else(|| PyList::empty_bound(py).into_py(py));
    let ask_prices_obj = get_optional(payload, &["ask_price", "askPrice"])
        .unwrap_or_else(|| PyList::empty_bound(py).into_py(py));
    let ask_vols_obj = get_optional(payload, &["ask_volume", "askVolume"])
        .unwrap_or_else(|| PyList::empty_bound(py).into_py(py));

    let (bids, asks, stats) = scale_book_pair_stats(
        py,
        bid_prices_obj.bind(py),
        bid_vols_obj.bind(py),
        ask_prices_obj.bind(py),
        ask_vols_obj.bind(py),
        scale,
    )?;

    let result = PyTuple::new_bound(
        py,
        [
            "bidask".into_py(py),
            symbol.into_py(py),
            bids.into_py(py),
            asks.into_py(py),
            exch_ts.into_py(py),
            false.into_py(py),
            stats.0.into_py(py),
            stats.1.into_py(py),
            stats.2.into_py(py),
            stats.3.into_py(py),
            stats.4.into_py(py),
            stats.5.into_py(py),
            stats.6.into_py(py),
        ],
    );

    Ok(result.into_py(py))
}

#[pyfunction]
pub fn normalize_bidask_tuple_np(
    py: Python<'_>,
    symbol: &str,
    exch_ts: i64,
    bid_prices: PyReadonlyArray1<f64>,
    bid_vols: PyReadonlyArray1<i64>,
    ask_prices: PyReadonlyArray1<f64>,
    ask_vols: PyReadonlyArray1<i64>,
    scale: i64,
) -> PyResult<PyObject> {
    if symbol.is_empty() {
        return Ok(py.None());
    }

    let (bids, best_bid, bid_top_vol, bid_depth_total) =
        scale_side_with_stats(py, bid_prices, bid_vols, scale)?;
    let (asks, best_ask, ask_top_vol, ask_depth_total) =
        scale_side_with_stats(py, ask_prices, ask_vols, scale)?;

    let (mid_price, spread, imbalance) =
        compute_l1_stats(best_bid, best_ask, bid_top_vol, ask_top_vol);

    let result = PyTuple::new_bound(
        py,
        [
            "bidask".into_py(py),
            symbol.into_py(py),
            bids.into_py(py),
            asks.into_py(py),
            exch_ts.into_py(py),
            false.into_py(py),
            best_bid.into_py(py),
            best_ask.into_py(py),
            bid_depth_total.into_py(py),
            ask_depth_total.into_py(py),
            mid_price.into_py(py),
            spread.into_py(py),
            imbalance.into_py(py),
        ],
    );

    Ok(result.into_py(py))
}

/// Like `normalize_bidask_tuple_np` but with built-in synthetic side synthesis.
///
/// If one side has no valid levels (all prices <= 0), a 1-lot level is
/// synthesized at `best +/- tick_size_scaled * synthetic_ticks`.
///
/// Returns the same 13-element tuple as `normalize_bidask_tuple_np` with
/// a 14th `synthesized: bool` element appended.
#[pyfunction]
pub fn normalize_bidask_tuple_with_synth(
    py: Python<'_>,
    symbol: &str,
    exch_ts: i64,
    bid_prices: PyReadonlyArray1<f64>,
    bid_vols: PyReadonlyArray1<i64>,
    ask_prices: PyReadonlyArray1<f64>,
    ask_vols: PyReadonlyArray1<i64>,
    scale: i64,
    tick_size_scaled: i64,
    synthetic_ticks: i64,
) -> PyResult<PyObject> {
    if symbol.is_empty() {
        return Ok(py.None());
    }

    let (bids, best_bid, bid_top_vol, bid_depth_total) =
        scale_side_with_stats(py, bid_prices, bid_vols, scale)?;
    let (asks, best_ask, ask_top_vol, ask_depth_total) =
        scale_side_with_stats(py, ask_prices, ask_vols, scale)?;

    let has_bids = bid_depth_total > 0;
    let has_asks = ask_depth_total > 0;

    let tick_offset = tick_size_scaled.max(1) * synthetic_ticks.max(1);

    // Determine effective bids/asks after potential synthesis
    let (eff_bids, eff_best_bid, eff_bid_top_vol, eff_bid_depth) = if !has_bids && has_asks {
        let synth_price = (best_ask - tick_offset).max(1);
        let synth = PyArray2::<i64>::zeros_bound(py, [1, 2], false);
        {
            let mut v = unsafe { synth.as_array_mut() };
            v[(0, 0)] = synth_price;
            v[(0, 1)] = 1;
        }
        (synth.into(), synth_price, 1i64, 1i64)
    } else {
        (bids, best_bid, bid_top_vol, bid_depth_total)
    };

    let (eff_asks, eff_best_ask, eff_ask_top_vol, eff_ask_depth) = if !has_asks && has_bids {
        let synth_price = (best_bid + tick_offset).max(1);
        let synth = PyArray2::<i64>::zeros_bound(py, [1, 2], false);
        {
            let mut v = unsafe { synth.as_array_mut() };
            v[(0, 0)] = synth_price;
            v[(0, 1)] = 1;
        }
        (synth.into(), synth_price, 1i64, 1i64)
    } else {
        (asks, best_ask, ask_top_vol, ask_depth_total)
    };

    let synthesized = (!has_bids && has_asks) || (!has_asks && has_bids);

    let (mid_price, spread, imbalance) =
        compute_l1_stats(eff_best_bid, eff_best_ask, eff_bid_top_vol, eff_ask_top_vol);

    let result = PyTuple::new_bound(
        py,
        [
            "bidask".into_py(py),
            symbol.into_py(py),
            eff_bids.into_py(py),
            eff_asks.into_py(py),
            exch_ts.into_py(py),
            false.into_py(py),
            eff_best_bid.into_py(py),
            eff_best_ask.into_py(py),
            eff_bid_depth.into_py(py),
            eff_ask_depth.into_py(py),
            mid_price.into_py(py),
            spread.into_py(py),
            imbalance.into_py(py),
            synthesized.into_py(py),
        ],
    );

    Ok(result.into_py(py))
}
