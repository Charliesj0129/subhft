use numpy::{PyArray2, PyReadonlyArray1, PyReadonlyArray2};
use pyo3::prelude::*;
use pyo3::types::PyDict;
use pyo3::types::PyIterator;

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
