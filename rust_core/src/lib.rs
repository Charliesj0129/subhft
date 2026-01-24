use pyo3::prelude::*;

mod lob;
pub mod ipc;
pub mod risk;

/// The HFT Platform Rust Core Module
#[pymodule]
fn rust_core(_py: Python, m: &Bound<'_, PyModule>) -> PyResult<()> {
    m.add_class::<lob::LimitOrderBook>()?;
    m.add_class::<ipc::ShmRingBuffer>()?;
    m.add_class::<risk::FastGate>()?;
    Ok(())
}
