use pyo3::prelude::*;
use std::collections::HashMap;

/// Reason codes for check_intent results.
const REASON_APPROVED: u8 = 0;
const REASON_DUPLICATE: u8 = 1;
const REASON_RISK_LIMIT: u8 = 2;
const REASON_EXPOSURE_LIMIT: u8 = 3;
const REASON_PRICE_BAND: u8 = 4;
const REASON_QTY_LIMIT: u8 = 5;

/// Maximum dedup map size before triggering LRU eviction.
const DEDUP_MAX_SIZE: usize = 10_000;

/// Fused gateway check combining dedup + policy + exposure + risk
/// into a single Rust call, eliminating 7 sequential Python-to-Rust
/// roundtrips in the gateway service.
#[pyclass]
pub struct RustGatewayFusedCheck {
    // Dedup state: idempotency_key -> timestamp_ns
    dedup: HashMap<String, u64>,
    dedup_ttl_ns: u64,

    // Exposure state
    symbol_exposure: HashMap<String, i64>,
    global_exposure: i64,
    global_limit: i64,
    per_symbol_limit: i64,

    // Risk limits
    max_notional: i64,
    price_band_bps: i64,
    max_order_qty: i64,
}

unsafe impl Send for RustGatewayFusedCheck {}

#[pymethods]
impl RustGatewayFusedCheck {
    #[new]
    pub fn new() -> Self {
        Self {
            dedup: HashMap::with_capacity(1024),
            dedup_ttl_ns: 5_000_000_000, // 5s default
            symbol_exposure: HashMap::with_capacity(64),
            global_exposure: 0,
            global_limit: 0,
            per_symbol_limit: 0,
            max_notional: 0,
            price_band_bps: 0,
            max_order_qty: 0,
        }
    }

    /// Configure risk parameters.
    pub fn configure_risk(&mut self, max_notional: i64, price_band_bps: i64, max_order_qty: i64) {
        self.max_notional = max_notional;
        self.price_band_bps = price_band_bps;
        self.max_order_qty = max_order_qty;
    }

    /// Configure exposure limits.
    pub fn configure_exposure(&mut self, global_limit: i64, per_symbol_limit: i64) {
        self.global_limit = global_limit;
        self.per_symbol_limit = per_symbol_limit;
    }

    /// Configure dedup TTL in nanoseconds.
    pub fn configure_dedup(&mut self, ttl_ns: u64) {
        self.dedup_ttl_ns = ttl_ns;
    }

    /// Single fused check replacing 7 sequential calls.
    /// Returns (approved: bool, reason_code: u8).
    ///
    /// reason_code: 0=approved, 1=duplicate, 2=risk_limit,
    ///              3=exposure_limit, 4=price_band, 5=qty_limit
    #[allow(clippy::too_many_arguments)]
    pub fn check_intent(
        &mut self,
        idempotency_key: &str,
        _intent_type: i32,
        symbol: &str,
        price: i64,
        qty: i64,
        _strategy_id: &str,
        reference_price: i64,
        now_ns: u64,
    ) -> (bool, u8) {
        // LRU eviction when map is too large
        if self.dedup.len() > DEDUP_MAX_SIZE {
            self.evict_expired(now_ns);
        }

        // 1. Dedup check
        if let Some(&stored_ts) = self.dedup.get(idempotency_key) {
            if now_ns.wrapping_sub(stored_ts) < self.dedup_ttl_ns {
                return (false, REASON_DUPLICATE);
            }
        }

        // 2. Qty limit
        if self.max_order_qty > 0 && qty > self.max_order_qty {
            return (false, REASON_QTY_LIMIT);
        }

        // 3. Price band
        if reference_price > 0 {
            let deviation = (price - reference_price).abs();
            // deviation * 10000 / reference_price > price_band_bps
            // Rearranged to avoid division: deviation * 10000 > price_band_bps * reference_price
            if self.price_band_bps > 0
                && deviation.saturating_mul(10_000)
                    > self.price_band_bps.saturating_mul(reference_price)
            {
                return (false, REASON_PRICE_BAND);
            }
        }

        let notional = price.saturating_mul(qty);

        // 4. Per-symbol exposure
        if self.per_symbol_limit > 0 {
            let current = self.symbol_exposure.get(symbol).copied().unwrap_or(0);
            if current.saturating_add(notional) > self.per_symbol_limit {
                return (false, REASON_EXPOSURE_LIMIT);
            }
        }

        // 5. Global exposure
        if self.global_limit > 0
            && self.global_exposure.saturating_add(notional) > self.global_limit
        {
            return (false, REASON_EXPOSURE_LIMIT);
        }

        // 6. Risk notional
        if self.max_notional > 0 && notional > self.max_notional {
            return (false, REASON_RISK_LIMIT);
        }

        // All checks passed: update state
        self.dedup.insert(idempotency_key.to_string(), now_ns);
        *self.symbol_exposure.entry(symbol.to_string()).or_insert(0) += notional;
        self.global_exposure += notional;

        (true, REASON_APPROVED)
    }

    /// Get human-readable reason string for a reason code.
    #[staticmethod]
    pub fn reason_str(code: u8) -> &'static str {
        match code {
            REASON_APPROVED => "approved",
            REASON_DUPLICATE => "duplicate",
            REASON_RISK_LIMIT => "risk_limit",
            REASON_EXPOSURE_LIMIT => "exposure_limit",
            REASON_PRICE_BAND => "price_band",
            REASON_QTY_LIMIT => "qty_limit",
            _ => "unknown",
        }
    }

    /// Reset all state.
    pub fn reset(&mut self) {
        self.dedup.clear();
        self.symbol_exposure.clear();
        self.global_exposure = 0;
    }

    /// Get current global exposure.
    pub fn global_exposure(&self) -> i64 {
        self.global_exposure
    }

    /// Get per-symbol exposure.
    pub fn symbol_exposure(&self, symbol: &str) -> i64 {
        self.symbol_exposure.get(symbol).copied().unwrap_or(0)
    }
}

impl RustGatewayFusedCheck {
    /// Evict dedup entries older than TTL.
    fn evict_expired(&mut self, now_ns: u64) {
        self.dedup
            .retain(|_key, &mut ts| now_ns.wrapping_sub(ts) < self.dedup_ttl_ns);
    }
}
