use pyo3::prelude::*;

mod lob;
mod alpha;
mod alpha_pressure;
mod alpha_reversal; // New module
pub mod ipc;
pub mod risk;

/// The HFT Platform Rust Core Module
#[pymodule]
fn rust_core(_py: Python, m: &Bound<'_, PyModule>) -> PyResult<()> {
    m.add_class::<lob::LimitOrderBook>()?;
    m.add_class::<alpha::AlphaDepthSlope>()?;
    m.add_class::<alpha_pressure::AlphaRegimePressure>()?;
    m.add_class::<alpha_reversal::AlphaRegimeReversal>()?;
    m.add_class::<ipc::ShmRingBuffer>()?;
    m.add_class::<risk::FastGate>()?;
    Ok(())
}
