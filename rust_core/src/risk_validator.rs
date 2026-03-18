//! Fused PriceBand + MaxNotional risk validator in Rust.
//!
//! Replaces two Python validator classes with a single Rust struct.
//! All arithmetic is integer-only (Precision Law). Caches are HashMap-based
//! for O(1) lookup without Python dict overhead.

use pyo3::prelude::*;
use std::collections::HashMap;

/// Rejection codes matching Python reason strings.
const OK: u8 = 0;
const PRICE_ZERO_OR_NEG: u8 = 1;
const PRICE_EXCEEDS_CAP: u8 = 2;
const PRICE_OUTSIDE_BAND: u8 = 3;
const MAX_NOTIONAL_EXCEEDED: u8 = 4;

#[pyclass]
pub struct RustRiskValidator {
    // PriceBand config
    max_price_cap_scaled: i64,
    tick_size_scaled: i64,
    default_band_ticks: i64,
    // MaxNotional config
    default_max_notional_scaled: i64,
    // Per-strategy band ticks cache: strategy_id -> band_ticks
    band_ticks_cache: HashMap<String, i64>,
    // Per-(strategy, symbol) max notional cache
    max_notional_cache: HashMap<(String, String), i64>,
}

#[pymethods]
impl RustRiskValidator {
    /// Create a new fused risk validator.
    ///
    /// Args:
    ///   max_price_cap_scaled: absolute price cap in scaled units
    ///   tick_size_scaled: tick size in scaled units
    ///   default_band_ticks: default number of ticks for price band
    ///   default_max_notional_scaled: default max notional in scaled units
    #[new]
    pub fn new(
        max_price_cap_scaled: i64,
        tick_size_scaled: i64,
        default_band_ticks: i64,
        default_max_notional_scaled: i64,
    ) -> Self {
        RustRiskValidator {
            max_price_cap_scaled,
            tick_size_scaled,
            default_band_ticks,
            default_max_notional_scaled,
            band_ticks_cache: HashMap::new(),
            max_notional_cache: HashMap::new(),
        }
    }

    /// Configure band_ticks for a specific strategy.
    pub fn set_band_ticks(&mut self, strategy_id: &str, band_ticks: i64) {
        self.band_ticks_cache
            .insert(strategy_id.to_string(), band_ticks);
    }

    /// Configure max_notional for a specific (strategy, symbol) pair.
    pub fn set_max_notional(&mut self, strategy_id: &str, symbol: &str, max_notional_scaled: i64) {
        self.max_notional_cache.insert(
            (strategy_id.to_string(), symbol.to_string()),
            max_notional_scaled,
        );
    }

    /// Fused check: PriceBand + MaxNotional in one call.
    ///
    /// Args:
    ///   intent_type: 0=NEW, 1=MODIFY, 2=CANCEL
    ///   price: scaled integer price
    ///   qty: quantity
    ///   strategy_id: strategy identifier
    ///   mid_price: current mid price from LOB (0 if unavailable)
    ///
    /// Returns:
    ///   (approved: bool, reject_code: u8)
    ///   reject_code: 0=OK, 1=PRICE_ZERO_OR_NEG, 2=PRICE_EXCEEDS_CAP,
    ///                3=PRICE_OUTSIDE_BAND, 4=MAX_NOTIONAL_EXCEEDED
    pub fn check(
        &self,
        intent_type: i32,
        price: i64,
        qty: i64,
        strategy_id: &str,
        symbol: &str,
        mid_price: i64,
    ) -> (bool, u8) {
        // CANCEL always passes
        if intent_type == 2 {
            return (true, OK);
        }

        // --- PriceBand checks ---

        if price <= 0 {
            return (false, PRICE_ZERO_OR_NEG);
        }

        if price > self.max_price_cap_scaled {
            return (false, PRICE_EXCEEDS_CAP);
        }

        // LOB-relative price band (only when mid_price available)
        if mid_price > 0 {
            let band_ticks = self
                .band_ticks_cache
                .get(strategy_id)
                .copied()
                .unwrap_or(self.default_band_ticks);
            let band_width = band_ticks * self.tick_size_scaled;
            let lower = mid_price - band_width;
            let upper = mid_price + band_width;
            if price < lower || price > upper {
                return (false, PRICE_OUTSIDE_BAND);
            }
        }

        // --- MaxNotional check ---

        let notional = price * qty;
        let max_notional = self
            .max_notional_cache
            .get(&(strategy_id.to_string(), symbol.to_string()))
            .copied()
            .unwrap_or(self.default_max_notional_scaled);
        if notional > max_notional {
            return (false, MAX_NOTIONAL_EXCEEDED);
        }

        (true, OK)
    }

    /// Map reject_code to reason string (for Python interop).
    #[staticmethod]
    pub fn reason_str(code: u8) -> &'static str {
        match code {
            OK => "OK",
            PRICE_ZERO_OR_NEG => "PRICE_ZERO_OR_NEG",
            PRICE_EXCEEDS_CAP => "PRICE_EXCEEDS_CAP",
            PRICE_OUTSIDE_BAND => "PRICE_OUTSIDE_BAND",
            MAX_NOTIONAL_EXCEEDED => "MAX_NOTIONAL_EXCEEDED",
            _ => "UNKNOWN",
        }
    }

    /// Class constants for reject codes
    #[classattr]
    const OK: u8 = OK;
    #[classattr]
    const PRICE_ZERO_OR_NEG: u8 = PRICE_ZERO_OR_NEG;
    #[classattr]
    const PRICE_EXCEEDS_CAP: u8 = PRICE_EXCEEDS_CAP;
    #[classattr]
    const PRICE_OUTSIDE_BAND: u8 = PRICE_OUTSIDE_BAND;
    #[classattr]
    const MAX_NOTIONAL_EXCEEDED: u8 = MAX_NOTIONAL_EXCEEDED;

    /// Reset all per-strategy/symbol caches.
    pub fn reset(&mut self) {
        self.band_ticks_cache.clear();
        self.max_notional_cache.clear();
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn make_validator() -> RustRiskValidator {
        RustRiskValidator::new(
            10_000_000_000,  // max_price_cap = 1M at x10000
            100,             // tick_size = 0.01 at x10000
            500,             // default_band_ticks
            100_000_000_000, // default_max_notional = 10M at x10000
        )
    }

    fn make_tight_notional_validator() -> RustRiskValidator {
        RustRiskValidator::new(
            10_000_000_000,
            100,
            500,
            10_000_000, // tight max_notional = 1000 at x10000
        )
    }

    #[test]
    fn test_valid_order_passes() {
        let v = make_validator();
        let (ok, code) = v.check(0, 100_0000, 10, "s1", "2330", 100_0000);
        assert!(ok);
        assert_eq!(code, OK);
    }

    #[test]
    fn test_cancel_always_passes() {
        let v = make_validator();
        let (ok, code) = v.check(2, 0, 0, "s1", "2330", 0);
        assert!(ok);
        assert_eq!(code, OK);
    }

    #[test]
    fn test_price_zero_rejected() {
        let v = make_validator();
        let (ok, code) = v.check(0, 0, 10, "s1", "2330", 100_0000);
        assert!(!ok);
        assert_eq!(code, PRICE_ZERO_OR_NEG);
    }

    #[test]
    fn test_price_negative_rejected() {
        let v = make_validator();
        let (ok, code) = v.check(0, -100, 10, "s1", "2330", 100_0000);
        assert!(!ok);
        assert_eq!(code, PRICE_ZERO_OR_NEG);
    }

    #[test]
    fn test_price_exceeds_cap() {
        let v = make_validator();
        let (ok, code) = v.check(0, 20_000_000_000, 1, "s1", "2330", 100_0000);
        assert!(!ok);
        assert_eq!(code, PRICE_EXCEEDS_CAP);
    }

    #[test]
    fn test_price_outside_band() {
        let v = make_validator();
        // mid=100_0000, band=500*100=50000, range=[50000, 150000]
        let (ok, code) = v.check(0, 200_0000, 1, "s1", "2330", 100_0000);
        assert!(!ok);
        assert_eq!(code, PRICE_OUTSIDE_BAND);
    }

    #[test]
    fn test_max_notional_exceeded() {
        let v = make_tight_notional_validator();
        // price=100_0000, qty=200 → notional=200_000_000 > 10_000_000
        let (ok, code) = v.check(0, 100_0000, 200, "s1", "2330", 100_0000);
        assert!(!ok);
        assert_eq!(code, MAX_NOTIONAL_EXCEEDED);
    }

    #[test]
    fn test_no_band_check_without_mid() {
        let v = make_validator();
        // mid_price=0 → skip band check; price very far but passes
        let (ok, _) = v.check(0, 5_000_000_000, 1, "s1", "2330", 0);
        assert!(ok);
    }

    #[test]
    fn test_per_strategy_band_ticks() {
        let mut v = make_validator();
        v.set_band_ticks("tight_strat", 10);
        // band = 10 * 100 = 1000; mid=100_0000, range=[99_9000, 100_1000]
        let (ok, code) = v.check(0, 100_2000, 1, "tight_strat", "2330", 100_0000);
        assert!(!ok);
        assert_eq!(code, PRICE_OUTSIDE_BAND);
        // But default strategy with 500 ticks still passes
        let (ok, _) = v.check(0, 100_2000, 1, "s1", "2330", 100_0000);
        assert!(ok);
    }

    #[test]
    fn test_per_strategy_symbol_notional() {
        let mut v = make_validator();
        v.set_max_notional("s1", "2330", 500_0000);
        // notional = 100_0000 * 10 = 1000_0000 > 500_0000
        let (ok, code) = v.check(0, 100_0000, 10, "s1", "2330", 100_0000);
        assert!(!ok);
        assert_eq!(code, MAX_NOTIONAL_EXCEEDED);
        // Different symbol uses default
        let (ok, _) = v.check(0, 100_0000, 10, "s1", "2317", 100_0000);
        assert!(ok);
    }

    #[test]
    fn test_reset_clears_caches() {
        let mut v = make_validator();
        v.set_band_ticks("s1", 10);
        v.set_max_notional("s1", "2330", 1);
        v.reset();
        // After reset, defaults should apply
        let (ok, _) = v.check(0, 100_0000, 1, "s1", "2330", 100_0000);
        assert!(ok);
    }

    #[test]
    fn test_reason_str() {
        assert_eq!(RustRiskValidator::reason_str(0), "OK");
        assert_eq!(RustRiskValidator::reason_str(1), "PRICE_ZERO_OR_NEG");
        assert_eq!(RustRiskValidator::reason_str(4), "MAX_NOTIONAL_EXCEEDED");
        assert_eq!(RustRiskValidator::reason_str(99), "UNKNOWN");
    }

    #[test]
    fn test_modify_order_checked() {
        let v = make_validator();
        // intent_type=1 (MODIFY) should also be checked
        let (ok, code) = v.check(1, 0, 10, "s1", "2330", 100_0000);
        assert!(!ok);
        assert_eq!(code, PRICE_ZERO_OR_NEG);
    }
}
