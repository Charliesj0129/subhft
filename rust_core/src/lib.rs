use pyo3::prelude::*;

mod alpha;
mod alpha_flow; // New module
mod alpha_markov; // New module
mod alpha_meta; // Meta Alpha module
mod alpha_ofi;
mod alpha_pressure;
mod alpha_reversal;
mod alpha_transient;
mod book_state;
mod bus;
mod circuit_breaker;
mod columnar_buffer;
mod dedup;
mod exposure;
mod fast_lob;
mod feature;
mod feature_engine;
pub mod ipc;
mod lob;
mod metrics_sampler;
mod normalizer_bidask;
mod normalizer_tick;
mod positions;
mod record_mapper;
pub mod risk;
mod risk_validator;
mod storm_guard;
mod strategy; // New Strategy
mod symbol_intern;
mod timeutil;
// Wave 4 modules
mod gateway_fused;
mod md_event_frame;
mod normalizer_lob_fused;
mod typed_ring;

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
    m.add_class::<risk_validator::RustRiskValidator>()?;
    m.add_class::<exposure::RustExposureStore>()?;
    m.add_class::<circuit_breaker::RustCircuitBreaker>()?;
    m.add_class::<dedup::RustDedupStore>()?;
    m.add_function(wrap_pyfunction!(timeutil::coerce_ns_int, m)?)?;
    m.add_function(wrap_pyfunction!(timeutil::coerce_ns_float, m)?)?;
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
    m.add_class::<storm_guard::RustStormGuardValidator>()?;
    m.add_function(wrap_pyfunction!(record_mapper::to_ch_price_scaled, m)?)?;
    m.add_function(wrap_pyfunction!(record_mapper::map_tick_record, m)?)?;
    m.add_function(wrap_pyfunction!(record_mapper::map_bidask_record, m)?)?;
    m.add_function(wrap_pyfunction!(record_mapper::map_order_record, m)?)?;
    m.add_function(wrap_pyfunction!(record_mapper::map_fill_record, m)?)?;
    m.add_function(wrap_pyfunction!(normalizer_bidask::normalize_bidask_v2, m)?)?;
    m.add_function(wrap_pyfunction!(normalizer_tick::normalize_tick_v2, m)?)?;
    m.add_class::<columnar_buffer::RustColumnarBuffer>()?;
    m.add_class::<book_state::RustBookState>()?;
    m.add_class::<feature::RustFeaturePipelineV1>()?;
    m.add_class::<feature_engine::RustFeatureEngineV2>()?;
    m.add_class::<metrics_sampler::RustMetricsSampler>()?;
    // Wave 4 classes
    m.add_class::<symbol_intern::SymbolInternTable>()?;
    m.add_class::<typed_ring::FastTypedRingBuffer>()?;
    m.add_class::<gateway_fused::RustGatewayFusedCheck>()?;
    m.add_class::<normalizer_lob_fused::RustNormalizerLobFused>()?;
    Ok(())
}
