use pyo3::prelude::*;

mod alpha;
mod alpha_flow; // New module
mod alpha_markov; // New module
mod alpha_meta; // Meta Alpha module
mod alpha_ofi;
mod alpha_pressure;
mod alpha_reversal;
mod alpha_transient;
mod bus;
mod fast_lob;
mod feature;
pub mod ipc;
mod lob;
mod positions;
pub mod risk;
mod strategy; // New Strategy

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
    m.add_class::<bus::EventBus>()?;
    m.add_class::<bus::FastRingBuffer>()?;
    m.add_class::<bus::FastTickRingBuffer>()?;
    m.add_class::<bus::FastBidAskRingBuffer>()?;
    m.add_class::<bus::FastLOBStatsRingBuffer>()?;
    m.add_class::<feature::LobFeatureKernelV1>()?;
    m.add_class::<ipc::ShmRingBuffer>()?;
    m.add_class::<risk::FastGate>()?;
    m.add_function(wrap_pyfunction!(fast_lob::scale_book, m)?)?;
    m.add_function(wrap_pyfunction!(fast_lob::scale_book_seq, m)?)?;
    m.add_function(wrap_pyfunction!(fast_lob::scale_book_pair, m)?)?;
    m.add_function(wrap_pyfunction!(fast_lob::scale_book_pair_stats, m)?)?;
    m.add_function(wrap_pyfunction!(fast_lob::scale_book_pair_stats_np, m)?)?;
    m.add_function(wrap_pyfunction!(fast_lob::compute_book_stats, m)?)?;
    m.add_function(wrap_pyfunction!(fast_lob::get_field, m)?)?;
    m.add_function(wrap_pyfunction!(fast_lob::normalize_tick_tuple, m)?)?;
    m.add_function(wrap_pyfunction!(fast_lob::normalize_bidask_tuple, m)?)?;
    m.add_function(wrap_pyfunction!(fast_lob::normalize_bidask_tuple_np, m)?)?;
    m.add_function(wrap_pyfunction!(
        fast_lob::normalize_bidask_tuple_with_synth,
        m
    )?)?;
    m.add_class::<strategy::AlphaStrategy>()?;
    m.add_class::<positions::RustPositionTracker>()?;
    Ok(())
}
