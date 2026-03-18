use numpy::PyReadonlyArray2;
use pyo3::prelude::*;
use pyo3::types::PyDict;

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

    let (mid_price, spread, imbalance) =
        super::scale::compute_l1_stats(best_bid, best_ask, bid_top_vol, ask_top_vol);

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
pub fn get_field(payload: &Bound<'_, PyAny>, keys: &Bound<'_, PyAny>) -> PyResult<PyObject> {
    let py = payload.py();
    let mut key_iter = keys.iter()?;

    if let Ok(dict) = payload.downcast::<PyDict>() {
        for item in key_iter.by_ref() {
            let key = item?;
            if let Ok(Some(value)) = dict.get_item(&key) {
                if !value.is_none() {
                    return Ok(value.into_py(py));
                }
            }
        }
        return Ok(py.None());
    }

    for item in key_iter {
        let key = item?;
        let key_str = key.str()?;
        let name = key_str.to_str()?;
        if let Ok(value) = payload.getattr(name) {
            if !value.is_none() {
                return Ok(value.into_py(py));
            }
        }
    }

    Ok(py.None())
}

pub(super) fn get_optional(payload: &Bound<'_, PyAny>, keys: &[&str]) -> Option<PyObject> {
    let py = payload.py();
    if let Ok(dict) = payload.downcast::<PyDict>() {
        for key in keys {
            if let Ok(Some(value)) = dict.get_item(*key) {
                if !value.is_none() {
                    return Some(value.into_py(py));
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

pub(super) fn extract_ts(value: Option<PyObject>, py: Python<'_>) -> PyResult<i64> {
    if let Some(obj) = value {
        let obj = obj.bind(py);
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
