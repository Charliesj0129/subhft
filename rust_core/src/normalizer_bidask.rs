use pyo3::prelude::*;
use pyo3::types::PyDict;

/// V2 bid/ask normalizer — scaled-int conversion with metadata.
#[pyfunction]
pub fn normalize_bidask_v2(
    _py: Python<'_>,
    bids: Vec<(f64, f64)>,
    asks: Vec<(f64, f64)>,
    price_scale: i64,
) -> PyResult<PyObject> {
    let dict = PyDict::new_bound(_py);
    let scaled_bids: Vec<(i64, i64)> = bids
        .iter()
        .map(|(p, q)| ((p * price_scale as f64) as i64, *q as i64))
        .collect();
    let scaled_asks: Vec<(i64, i64)> = asks
        .iter()
        .map(|(p, q)| ((p * price_scale as f64) as i64, *q as i64))
        .collect();
    dict.set_item("bids", scaled_bids)?;
    dict.set_item("asks", scaled_asks)?;
    Ok(dict.into())
}
