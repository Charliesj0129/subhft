use numpy::{PyArray2, PyReadonlyArray1, PyReadonlyArray2};
use pyo3::prelude::*;
use pyo3::types::PyDict;
use pyo3::types::PyIterator;
use pyo3::types::PyList;
use pyo3::types::PyTuple;

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

    let out = PyArray2::<i64>::zeros(py, [rows, 2], false);
    let mut out_view = unsafe { out.as_array_mut() };
    let mut idx = 0usize;
    for (&p, &v) in prices.iter().zip(vols.iter()) {
        if p > 0.0 {
            out_view[(idx, 0)] = (p * scale as f64) as i64;
            out_view[(idx, 1)] = v;
            idx += 1;
        }
    }

    Ok(out.into_py(py))
}

#[pyfunction]
pub fn scale_book_seq(
    py: Python<'_>,
    prices: &PyAny,
    vols: &PyAny,
    scale: i64,
) -> PyResult<Py<PyArray2<i64>>> {
    scale_book_seq_inner(py, prices, vols, scale)
}

#[pyfunction]
pub fn scale_book_pair(
    py: Python<'_>,
    bid_prices: &PyAny,
    bid_vols: &PyAny,
    ask_prices: &PyAny,
    ask_vols: &PyAny,
    scale: i64,
) -> PyResult<(Py<PyArray2<i64>>, Py<PyArray2<i64>>)> {
    let bids = scale_book_seq_inner(py, bid_prices, bid_vols, scale)?;
    let asks = scale_book_seq_inner(py, ask_prices, ask_vols, scale)?;
    Ok((bids, asks))
}

#[pyfunction]
pub fn scale_book_pair_stats(
    py: Python<'_>,
    bid_prices: &PyAny,
    bid_vols: &PyAny,
    ask_prices: &PyAny,
    ask_vols: &PyAny,
    scale: i64,
) -> PyResult<(
    Py<PyArray2<i64>>,
    Py<PyArray2<i64>>,
    (i64, i64, i64, i64, f64, f64, f64),
)> {
    let bids = scale_book_seq_inner(py, bid_prices, bid_vols, scale)?;
    let asks = scale_book_seq_inner(py, ask_prices, ask_vols, scale)?;

    let bids_view = bids.as_ref(py).readonly();
    let asks_view = asks.as_ref(py).readonly();
    let stats = compute_book_stats(bids_view, asks_view)?;

    Ok((bids, asks, stats))
}

#[pyfunction]
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

fn scale_side_with_stats(
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

    let out = PyArray2::<i64>::zeros(py, [rows, 2], false);
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

    Ok((out.into_py(py), best_price, top_vol, depth_total))
}

fn scale_book_seq_inner(
    py: Python<'_>,
    prices: &PyAny,
    vols: &PyAny,
    scale: i64,
) -> PyResult<Py<PyArray2<i64>>> {
    let mut price_iter = PyIterator::from_object(prices)?;
    let mut vol_iter = PyIterator::from_object(vols)?;
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
    let out = PyArray2::<i64>::zeros(py, [rows, 2], false);
    let mut out_view = unsafe { out.as_array_mut() };
    for i in 0..rows {
        out_view[(i, 0)] = flat[i * 2];
        out_view[(i, 1)] = flat[i * 2 + 1];
    }

    Ok(out.into_py(py))
}

#[pyfunction]
pub fn compute_book_stats(
    bids: PyReadonlyArray2<i64>,
    asks: PyReadonlyArray2<i64>,
) -> PyResult<(i64, i64, i64, i64, f64, f64, f64)> {
    let bids = bids.as_array();
    let asks = asks.as_array();

    let mut best_bid = 0i64;
    let mut best_ask = 0i64;
    let mut bid_top_vol = 0i64;
    let mut ask_top_vol = 0i64;

    let mut bid_depth_total = 0i64;
    let mut ask_depth_total = 0i64;

    if bids.nrows() > 0 {
        let row0 = bids.row(0);
        if row0.len() >= 2 {
            best_bid = row0[0];
            bid_top_vol = row0[1];
        }
        for row in bids.rows() {
            if row.len() >= 2 {
                bid_depth_total += row[1];
            }
        }
    }

    if asks.nrows() > 0 {
        let row0 = asks.row(0);
        if row0.len() >= 2 {
            best_ask = row0[0];
            ask_top_vol = row0[1];
        }
        for row in asks.rows() {
            if row.len() >= 2 {
                ask_depth_total += row[1];
            }
        }
    }

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

    Ok((
        best_bid,
        best_ask,
        bid_depth_total,
        ask_depth_total,
        mid_price,
        spread,
        imbalance,
    ))
}

#[pyfunction]
pub fn get_field(payload: &PyAny, keys: &PyAny) -> PyResult<PyObject> {
    let py = payload.py();
    let mut key_iter = PyIterator::from_object(keys)?;

    if let Ok(dict) = payload.downcast::<PyDict>() {
        while let Some(item) = key_iter.next() {
            let key = item?;
            if let Ok(value) = dict.get_item(key) {
                if !value.is_none() {
                    return Ok(value.into_py(py));
                }
            }
        }
        return Ok(py.None());
    }

    while let Some(item) = key_iter.next() {
        let key = item?;
        let name = key.str()?.to_str()?;
        if let Ok(value) = payload.getattr(name) {
            if !value.is_none() {
                return Ok(value.into_py(py));
            }
        }
    }

    Ok(py.None())
}

/// Like `normalize_bidask_tuple_np` but with built-in synthetic side synthesis.
///
/// If one side has no valid levels (all prices ≤ 0), a 1-lot level is
/// synthesized at `best ± tick_size_scaled * synthetic_ticks`.
///
/// Returns the same 13-element tuple as `normalize_bidask_tuple_np` with
/// a 14th `synthesized: bool` element appended.
#[pyfunction]
#[allow(clippy::too_many_arguments)]
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
        let synth = PyArray2::<i64>::zeros(py, [1, 2], false);
        {
            let mut v = unsafe { synth.as_array_mut() };
            v[(0, 0)] = synth_price;
            v[(0, 1)] = 1;
        }
        (synth.into_py(py), synth_price, 1i64, 1i64)
    } else {
        (bids, best_bid, bid_top_vol, bid_depth_total)
    };

    let (eff_asks, eff_best_ask, eff_ask_top_vol, eff_ask_depth) = if !has_asks && has_bids {
        let synth_price = (best_bid + tick_offset).max(1);
        let synth = PyArray2::<i64>::zeros(py, [1, 2], false);
        {
            let mut v = unsafe { synth.as_array_mut() };
            v[(0, 0)] = synth_price;
            v[(0, 1)] = 1;
        }
        (synth.into_py(py), synth_price, 1i64, 1i64)
    } else {
        (asks, best_ask, ask_top_vol, ask_depth_total)
    };

    let synthesized = (!has_bids && has_asks) || (!has_asks && has_bids);

    let mut mid_price = 0.0f64;
    let mut spread = 0.0f64;
    let mut imbalance = 0.0f64;

    if eff_best_bid > 0 && eff_best_ask > 0 {
        mid_price = (eff_best_bid + eff_best_ask) as f64 / 2.0;
        spread = (eff_best_ask - eff_best_bid) as f64;
        let total_top = eff_bid_top_vol + eff_ask_top_vol;
        if total_top > 0 {
            imbalance = (eff_bid_top_vol - eff_ask_top_vol) as f64 / total_top as f64;
        }
    }

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

fn get_optional(payload: &PyAny, keys: &[&str]) -> Option<PyObject> {
    let py = payload.py();
    if let Ok(dict) = payload.downcast::<PyDict>() {
        for key in keys {
            if let Ok(value_opt) = dict.get_item(*key) {
                if let Some(value) = value_opt {
                    if !value.is_none() {
                        return Some(value.into_py(py));
                    }
                }
            }
        }
        return None;
    }

    for key in keys {
        if let Ok(value) = payload.getattr(*key) {
            if !value.is_none() {
                return Some(value.into_py(py));
            }
        }
    }

    None
}

fn extract_ts(value: Option<PyObject>, py: Python<'_>) -> PyResult<i64> {
    if let Some(obj) = value {
        let obj = obj.as_ref(py);
        if obj.hasattr("timestamp")? {
            let ts = obj.call_method0("timestamp")?;
            let ts_f: f64 = ts.extract()?;
            return Ok((ts_f * 1e9) as i64);
        }
        if let Ok(ts_i) = obj.extract::<i64>() {
            return Ok(ts_i);
        }
        if let Ok(ts_f) = obj.extract::<f64>() {
            return Ok(ts_f as i64);
        }
    }
    Ok(0)
}

#[pyfunction]
pub fn normalize_tick_tuple(
    py: Python<'_>,
    payload: &PyAny,
    symbol: &str,
    scale: i64,
) -> PyResult<PyObject> {
    if symbol.is_empty() {
        return Ok(py.None());
    }

    let ts_obj = get_optional(payload, &["ts", "datetime"]);
    let exch_ts = extract_ts(ts_obj, py)?;

    let close_obj = get_optional(payload, &["close", "Close", "price", "Price"]);
    let volume_obj = get_optional(payload, &["volume", "Volume"]);
    let total_volume_obj = get_optional(payload, &["total_volume", "totalVolume"]);
    let simtrade_obj = get_optional(payload, &["simtrade", "sim_trade"]);
    let oddlot_obj = get_optional(payload, &["intraday_odd", "odd_lot"]);

    let price = if let Some(obj) = close_obj {
        let obj = obj.as_ref(py);
        if let Ok(p) = obj.extract::<f64>() {
            (p * scale as f64) as i64
        } else if let Ok(p) = obj.extract::<i64>() {
            p * scale
        } else {
            0
        }
    } else {
        0
    };

    let volume = if let Some(obj) = volume_obj {
        obj.as_ref(py).extract::<i64>().unwrap_or(0)
    } else {
        0
    };

    let total_volume = if let Some(obj) = total_volume_obj {
        obj.as_ref(py).extract::<i64>().unwrap_or(0)
    } else {
        0
    };

    let is_simtrade = if let Some(obj) = simtrade_obj {
        let obj = obj.as_ref(py);
        if let Ok(flag) = obj.extract::<bool>() {
            flag
        } else if let Ok(flag) = obj.extract::<i64>() {
            flag != 0
        } else if let Ok(flag) = obj.extract::<f64>() {
            flag != 0.0
        } else {
            false
        }
    } else {
        false
    };

    let is_odd_lot = if let Some(obj) = oddlot_obj {
        let obj = obj.as_ref(py);
        if let Ok(flag) = obj.extract::<bool>() {
            flag
        } else if let Ok(flag) = obj.extract::<i64>() {
            flag != 0
        } else if let Ok(flag) = obj.extract::<f64>() {
            flag != 0.0
        } else {
            false
        }
    } else {
        false
    };

    let result = (
        "tick",
        symbol,
        price,
        volume,
        total_volume,
        is_simtrade,
        is_odd_lot,
        exch_ts,
    );
    Ok(result.into_py(py))
}

#[pyfunction]
pub fn normalize_bidask_tuple(
    py: Python<'_>,
    payload: &PyAny,
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
        bid_prices_obj.as_ref(py),
        bid_vols_obj.as_ref(py),
        ask_prices_obj.as_ref(py),
        ask_vols_obj.as_ref(py),
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
