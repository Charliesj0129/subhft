use pyo3::prelude::*;

mod lob;
mod bus;

use lob::LimitOrderBook;
use bus::EventBus;

/// A Python module implemented in Rust.
#[pymodule]
fn rust_core(_py: Python, m: &Bound<'_, PyModule>) -> PyResult<()> {
    m.add_class::<EventBus>()?;
    
    // exposing LOB wrapper if needed, for now just EventBus is pyclass
    // We might want to wrap LOB in a pyclass later
    
    Ok(())
}
