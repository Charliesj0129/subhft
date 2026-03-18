use pyo3::prelude::*;
use pyo3::types::PyDict;

const CLICKHOUSE_PRICE_SCALE: f64 = 1_000_000.0;

/// Convert a price to ClickHouse-scaled integer (x1e6).
#[pyfunction]
pub fn to_ch_price_scaled(price: i64, price_scale: i64) -> i64 {
    if price_scale == 0 {
        return 0;
    }
    // price is already in x(price_scale) format; convert to x1e6
    let factor = CLICKHOUSE_PRICE_SCALE / price_scale as f64;
    (price as f64 * factor) as i64
}

/// Map a tick event to a ClickHouse record dict.
#[pyfunction]
pub fn map_tick_record(
    py: Python<'_>,
    symbol: &str,
    price: i64,
    volume: i64,
    ts_ns: i64,
    price_scale: i64,
) -> PyResult<PyObject> {
    let dict = PyDict::new_bound(py);
    dict.set_item("symbol", symbol)?;
    dict.set_item("price_scaled", to_ch_price_scaled(price, price_scale))?;
    dict.set_item("volume", volume)?;
    dict.set_item("ts_ns", ts_ns)?;
    Ok(dict.into())
}

/// Map a bid/ask event to a ClickHouse record dict.
#[pyfunction]
#[allow(clippy::too_many_arguments)]
pub fn map_bidask_record(
    py: Python<'_>,
    symbol: &str,
    bid_price: i64,
    ask_price: i64,
    bid_qty: i64,
    ask_qty: i64,
    ts_ns: i64,
    price_scale: i64,
) -> PyResult<PyObject> {
    let dict = PyDict::new_bound(py);
    dict.set_item("symbol", symbol)?;
    dict.set_item(
        "bid_price_scaled",
        to_ch_price_scaled(bid_price, price_scale),
    )?;
    dict.set_item(
        "ask_price_scaled",
        to_ch_price_scaled(ask_price, price_scale),
    )?;
    dict.set_item("bid_qty", bid_qty)?;
    dict.set_item("ask_qty", ask_qty)?;
    dict.set_item("ts_ns", ts_ns)?;
    Ok(dict.into())
}

/// Map an order event to a ClickHouse record dict.
#[pyfunction]
pub fn map_order_record(
    py: Python<'_>,
    symbol: &str,
    price: i64,
    qty: i64,
    side: &str,
    ts_ns: i64,
    price_scale: i64,
) -> PyResult<PyObject> {
    let dict = PyDict::new_bound(py);
    dict.set_item("symbol", symbol)?;
    dict.set_item("price_scaled", to_ch_price_scaled(price, price_scale))?;
    dict.set_item("qty", qty)?;
    dict.set_item("side", side)?;
    dict.set_item("ts_ns", ts_ns)?;
    Ok(dict.into())
}

/// Map a fill event to a ClickHouse record dict.
#[pyfunction]
pub fn map_fill_record(
    py: Python<'_>,
    symbol: &str,
    price: i64,
    qty: i64,
    fee: i64,
    ts_ns: i64,
    price_scale: i64,
) -> PyResult<PyObject> {
    let dict = PyDict::new_bound(py);
    dict.set_item("symbol", symbol)?;
    dict.set_item("price_scaled", to_ch_price_scaled(price, price_scale))?;
    dict.set_item("qty", qty)?;
    dict.set_item("fee_scaled", to_ch_price_scaled(fee, price_scale))?;
    dict.set_item("ts_ns", ts_ns)?;
    Ok(dict.into())
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn test_to_ch_price_scaled_10000() {
        // price=500_0000 at scale=10000 → 500_0000 * (1e6/10000) = 500_000_000
        assert_eq!(to_ch_price_scaled(500_0000, 10000), 500_000_000);
    }

    #[test]
    fn test_to_ch_price_scaled_zero_scale() {
        assert_eq!(to_ch_price_scaled(123, 0), 0);
    }

    #[test]
    fn test_to_ch_price_scaled_same_scale() {
        // scale = 1_000_000 → factor = 1.0
        assert_eq!(to_ch_price_scaled(500_000_000, 1_000_000), 500_000_000);
    }

    #[test]
    fn test_to_ch_price_zero() {
        assert_eq!(to_ch_price_scaled(0, 10000), 0);
    }

    #[test]
    fn test_to_ch_price_negative() {
        // Negative prices (shouldn't happen but test edge case)
        assert_eq!(to_ch_price_scaled(-100_0000, 10000), -100_000_000);
    }

    #[test]
    fn test_to_ch_price_small_scale() {
        // scale = 100 → factor = 10000
        assert_eq!(to_ch_price_scaled(5000, 100), 50_000_000);
    }
}
