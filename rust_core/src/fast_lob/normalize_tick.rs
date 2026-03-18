use pyo3::prelude::*;

use super::stats::{extract_ts, get_optional};

#[pyfunction]
pub fn normalize_tick_tuple(
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

    let close_obj = get_optional(payload, &["close", "Close", "price", "Price"]);
    let volume_obj = get_optional(payload, &["volume", "Volume"]);
    let total_volume_obj = get_optional(payload, &["total_volume", "totalVolume"]);
    let simtrade_obj = get_optional(payload, &["simtrade", "sim_trade"]);
    let oddlot_obj = get_optional(payload, &["intraday_odd", "odd_lot"]);

    let price = if let Some(obj) = close_obj {
        let obj = obj.bind(py);
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
        obj.bind(py).extract::<i64>().unwrap_or(0)
    } else {
        0
    };

    let total_volume = if let Some(obj) = total_volume_obj {
        obj.bind(py).extract::<i64>().unwrap_or(0)
    } else {
        0
    };

    let is_simtrade = if let Some(obj) = simtrade_obj {
        let obj = obj.bind(py);
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
        let obj = obj.bind(py);
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
