use pyo3::prelude::*;

mod lob;
mod alpha;
mod alpha_pressure;
mod alpha_reversal;
mod alpha_ofi;
mod alpha_transient;
mod alpha_markov; // New module
mod alpha_flow; // New module
mod alpha_meta; // Meta Alpha module
mod fast_lob;
pub mod ipc;
pub mod risk;

/// The HFT Platform Rust Core Module
#[pymodule]
fn rust_core(_py: Python, m: &Bound<'_, PyModule>) -> PyResult<()> {
    m.add_class::<lob::LimitOrderBook>()?;
    m.add_class::<alpha::AlphaDepthSlope>()?;
    m.add_class::<alpha_pressure::AlphaRegimePressure>()?;
    m.add_class::<alpha_reversal::AlphaRegimeReversal>()?;
    m.add_class::<alpha_ofi::AlphaOFI>()?;
    m.add_class::<alpha_transient::AlphaTransientReprice>()?;
    m.add_class::<alpha_markov::AlphaMarkovTransition>()?;
    m.add_class::<alpha_flow::MatchedFilterTradeFlow>()?;
    m.add_class::<alpha_meta::MetaAlpha>()?;
    m.add_class::<ipc::ShmRingBuffer>()?;
    m.add_class::<risk::FastGate>()?;
    m.add_function(wrap_pyfunction!(fast_lob::scale_book, m)?)?;
    m.add_function(wrap_pyfunction!(fast_lob::scale_book_seq, m)?)?;
    m.add_function(wrap_pyfunction!(fast_lob::scale_book_pair, m)?)?;
    m.add_function(wrap_pyfunction!(fast_lob::scale_book_pair_stats, m)?)?;
    m.add_function(wrap_pyfunction!(fast_lob::compute_book_stats, m)?)?;
    m.add_function(wrap_pyfunction!(fast_lob::get_field, m)?)?;
    Ok(())
}
