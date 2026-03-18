#![allow(clippy::too_many_arguments)]

use numpy::{PyArray2, PyArrayMethods, PyReadonlyArray1};
use pyo3::prelude::*;

/// Pure-Rust L1 stats computation (no Python dependency).
pub(super) fn compute_l1_stats(
    best_bid: i64,
    best_ask: i64,
    bid_top_vol: i64,
    ask_top_vol: i64,
) -> (f64, f64, f64) {
    let mut mid_price = 0.0f64;
    let mut spread = 0.0f64;
    let mut imbalance = 0.0f64;

    if best_bid > 0 && best_ask > 0 {
        mid_price = (best_bid + best_ask) as f64 / 2.0;
        spread = (best_ask - best_bid) as f64;
        let total_top = bid_top_vol + ask_top_vol;
        if total_top > 0 {
            imbalance = (bid_top_vol - ask_top_vol) as f64 / total_top as f64;
        }
    }

    (mid_price, spread, imbalance)
}

#[pyfunction]
pub fn scale_book(
    py: Python<'_>,
    prices: PyReadonlyArray1<f64>,
    vols: PyReadonlyArray1<i64>,
    scale: i64,
) -> PyResult<Py<PyArray2<i64>>> {
    let prices = prices.as_array();
    let vols = vols.as_array();

    if prices.len() != vols.len() {
        return Err(pyo3::exceptions::PyValueError::new_err(
            "prices/vols length mismatch",
        ));
    }

    let mut rows = 0usize;
    for (&p, _) in prices.iter().zip(vols.iter()) {
        if p > 0.0 {
            rows += 1;
        }
    }

    let out = PyArray2::<i64>::zeros_bound(py, [rows, 2], false);
    let mut out_view = unsafe { out.as_array_mut() };
    let mut idx = 0usize;
    for (&p, &v) in prices.iter().zip(vols.iter()) {
        if p > 0.0 {
            out_view[(idx, 0)] = (p * scale as f64) as i64;
            out_view[(idx, 1)] = v;
            idx += 1;
        }
    }

    Ok(out.into())
}

#[pyfunction]
pub fn scale_book_seq(
    py: Python<'_>,
    prices: &Bound<'_, PyAny>,
    vols: &Bound<'_, PyAny>,
    scale: i64,
) -> PyResult<Py<PyArray2<i64>>> {
    scale_book_seq_inner(py, prices, vols, scale)
}

#[pyfunction]
#[allow(clippy::type_complexity)]
pub fn scale_book_pair(
    py: Python<'_>,
    bid_prices: &Bound<'_, PyAny>,
    bid_vols: &Bound<'_, PyAny>,
    ask_prices: &Bound<'_, PyAny>,
    ask_vols: &Bound<'_, PyAny>,
    scale: i64,
) -> PyResult<(Py<PyArray2<i64>>, Py<PyArray2<i64>>)> {
    let bids = scale_book_seq_inner(py, bid_prices, bid_vols, scale)?;
    let asks = scale_book_seq_inner(py, ask_prices, ask_vols, scale)?;
    Ok((bids, asks))
}

#[pyfunction]
#[allow(clippy::type_complexity)]
pub fn scale_book_pair_stats(
    py: Python<'_>,
    bid_prices: &Bound<'_, PyAny>,
    bid_vols: &Bound<'_, PyAny>,
    ask_prices: &Bound<'_, PyAny>,
    ask_vols: &Bound<'_, PyAny>,
    scale: i64,
) -> PyResult<(
    Py<PyArray2<i64>>,
    Py<PyArray2<i64>>,
    (i64, i64, i64, i64, f64, f64, f64),
)> {
    let bids = scale_book_seq_inner(py, bid_prices, bid_vols, scale)?;
    let asks = scale_book_seq_inner(py, ask_prices, ask_vols, scale)?;

    let bids_view = bids.bind(py).readonly();
    let asks_view = asks.bind(py).readonly();
    let stats = super::stats::compute_book_stats(bids_view, asks_view)?;

    Ok((bids, asks, stats))
}

#[pyfunction]
#[allow(clippy::type_complexity)]
pub fn scale_book_pair_stats_np(
    py: Python<'_>,
    bid_prices: PyReadonlyArray1<f64>,
    bid_vols: PyReadonlyArray1<i64>,
    ask_prices: PyReadonlyArray1<f64>,
    ask_vols: PyReadonlyArray1<i64>,
    scale: i64,
) -> PyResult<(
    Py<PyArray2<i64>>,
    Py<PyArray2<i64>>,
    (i64, i64, i64, i64, f64, f64, f64),
)> {
    let (bids, best_bid, bid_top_vol, bid_depth_total) =
        scale_side_with_stats(py, bid_prices, bid_vols, scale)?;
    let (asks, best_ask, ask_top_vol, ask_depth_total) =
        scale_side_with_stats(py, ask_prices, ask_vols, scale)?;

    let (mid_price, spread, imbalance) =
        compute_l1_stats(best_bid, best_ask, bid_top_vol, ask_top_vol);

    Ok((
        bids,
        asks,
        (
            best_bid,
            best_ask,
            bid_depth_total,
            ask_depth_total,
            mid_price,
            spread,
            imbalance,
        ),
    ))
}

pub(super) fn scale_side_with_stats(
    py: Python<'_>,
    prices: PyReadonlyArray1<f64>,
    vols: PyReadonlyArray1<i64>,
    scale: i64,
) -> PyResult<(Py<PyArray2<i64>>, i64, i64, i64)> {
    let prices = prices.as_array();
    let vols = vols.as_array();

    if prices.len() != vols.len() {
        return Err(pyo3::exceptions::PyValueError::new_err(
            "prices/vols length mismatch",
        ));
    }

    let mut rows = 0usize;
    for (&p, _) in prices.iter().zip(vols.iter()) {
        if p > 0.0 {
            rows += 1;
        }
    }

    let out = PyArray2::<i64>::zeros_bound(py, [rows, 2], false);
    let mut out_view = unsafe { out.as_array_mut() };

    let mut idx = 0usize;
    let mut best_price = 0i64;
    let mut top_vol = 0i64;
    let mut depth_total = 0i64;

    for (&p, &v) in prices.iter().zip(vols.iter()) {
        if p > 0.0 {
            let scaled = (p * scale as f64) as i64;
            out_view[(idx, 0)] = scaled;
            out_view[(idx, 1)] = v;
            if idx == 0 {
                best_price = scaled;
                top_vol = v;
            }
            depth_total += v;
            idx += 1;
        }
    }

    Ok((out.into(), best_price, top_vol, depth_total))
}

pub(super) fn scale_book_seq_inner(
    py: Python<'_>,
    prices: &Bound<'_, PyAny>,
    vols: &Bound<'_, PyAny>,
    scale: i64,
) -> PyResult<Py<PyArray2<i64>>> {
    let mut price_iter = prices.iter()?;
    let mut vol_iter = vols.iter()?;
    let mut flat: Vec<i64> = Vec::new();

    loop {
        let p_next = price_iter.next();
        let v_next = vol_iter.next();
        match (p_next, v_next) {
            (None, None) => break,
            (Some(Ok(p_obj)), Some(Ok(v_obj))) => {
                let p: f64 = p_obj.extract()?;
                let v: i64 = v_obj.extract()?;
                if p > 0.0 {
                    flat.push((p * scale as f64) as i64);
                    flat.push(v);
                }
            }
            _ => {
                return Err(pyo3::exceptions::PyValueError::new_err(
                    "prices/vols length mismatch",
                ));
            }
        }
    }

    let rows = flat.len() / 2;
    let out = PyArray2::<i64>::zeros_bound(py, [rows, 2], false);
    let mut out_view = unsafe { out.as_array_mut() };
    for i in 0..rows {
        out_view[(i, 0)] = flat[i * 2];
        out_view[(i, 1)] = flat[i * 2 + 1];
    }

    Ok(out.into())
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn test_compute_l1_stats_normal() {
        let (mid, spread, imbalance) = compute_l1_stats(1000, 1010, 50, 50);
        assert_eq!(mid, 1005.0);
        assert_eq!(spread, 10.0);
        assert_eq!(imbalance, 0.0); // equal volumes -> 0 imbalance
    }

    #[test]
    fn test_compute_l1_stats_asymmetric_volume() {
        let (mid, spread, imbalance) = compute_l1_stats(1000, 1010, 80, 20);
        assert_eq!(mid, 1005.0);
        assert_eq!(spread, 10.0);
        // imbalance = (80 - 20) / 100 = 0.6
        assert!((imbalance - 0.6).abs() < 1e-12);
    }

    #[test]
    fn test_compute_l1_stats_zero_bid() {
        let (mid, spread, imbalance) = compute_l1_stats(0, 1010, 0, 50);
        assert_eq!(mid, 0.0);
        assert_eq!(spread, 0.0);
        assert_eq!(imbalance, 0.0);
    }

    #[test]
    fn test_compute_l1_stats_zero_ask() {
        let (mid, spread, imbalance) = compute_l1_stats(1000, 0, 50, 0);
        assert_eq!(mid, 0.0);
        assert_eq!(spread, 0.0);
        assert_eq!(imbalance, 0.0);
    }

    #[test]
    fn test_compute_l1_stats_zero_volumes() {
        let (mid, spread, imbalance) = compute_l1_stats(1000, 1010, 0, 0);
        assert_eq!(mid, 1005.0);
        assert_eq!(spread, 10.0);
        assert_eq!(imbalance, 0.0); // total_top == 0 -> imbalance stays 0
    }

    #[test]
    fn test_compute_l1_stats_negative_imbalance() {
        let (_, _, imbalance) = compute_l1_stats(1000, 1010, 20, 80);
        // imbalance = (20 - 80) / 100 = -0.6
        assert!((imbalance - (-0.6)).abs() < 1e-12);
    }
}
