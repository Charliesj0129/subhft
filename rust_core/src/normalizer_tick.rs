use pyo3::prelude::*;
use pyo3::types::PyDict;

/// V2 tick normalizer — scaled-int conversion with metadata.
#[pyfunction]
pub fn normalize_tick_v2(
    _py: Python<'_>,
    price: f64,
    volume: f64,
    price_scale: i64,
) -> PyResult<PyObject> {
    let dict = PyDict::new_bound(_py);
    dict.set_item("price", (price * price_scale as f64) as i64)?;
    dict.set_item("volume", volume as i64)?;
    Ok(dict.into())
}
