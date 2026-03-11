//! RustExposureStore — 3-level HashMap exposure tracker.
//!
//! Replaces Python's nested dict[str, dict[str, dict[str, int]]] + threading.Lock
//! with a Rust HashMap. All arithmetic is integer-only (Precision Law).
//! The GIL serializes access from Python, so no internal Mutex needed.

use pyo3::prelude::*;
use std::collections::HashMap;

#[pyclass]
pub struct RustExposureStore {
    global_max: i64,
    max_symbols: usize,
    // Flat map: (account, strategy_id, symbol) -> notional
    exposure: HashMap<(String, String, String), i64>,
    global_notional: i64,
    // Per-strategy limits: strategy_id -> max_notional_scaled
    limits: HashMap<String, i64>,
}

#[pymethods]
impl RustExposureStore {
    #[new]
    pub fn new(global_max: i64, max_symbols: usize) -> Self {
        RustExposureStore {
            global_max,
            max_symbols,
            exposure: HashMap::with_capacity(256),
            global_notional: 0,
            limits: HashMap::new(),
        }
    }

    /// Set per-strategy max notional limit.
    pub fn set_limit(&mut self, strategy_id: &str, max_notional_scaled: i64) {
        self.limits
            .insert(strategy_id.to_string(), max_notional_scaled);
    }

    /// Atomic check-and-update.
    ///
    /// Args:
    ///   account, strategy_id, symbol: exposure key components
    ///   intent_type: 2=CANCEL (skip), other=check
    ///   price, qty: scaled integers
    ///
    /// Returns:
    ///   (approved: bool, reason_code: u8)
    ///   0=OK, 1=GLOBAL_EXPOSURE_LIMIT, 2=STRATEGY_EXPOSURE_LIMIT, 3=SYMBOL_LIMIT_REACHED
    pub fn check_and_update(
        &mut self,
        account: &str,
        strategy_id: &str,
        symbol: &str,
        intent_type: i32,
        price: i64,
        qty: i64,
    ) -> (bool, u8) {
        // CANCEL always passes
        if intent_type == 2 {
            return (true, 0);
        }

        let notional = price * qty;

        // Global check
        if self.global_max > 0 && self.global_notional + notional > self.global_max {
            return (false, 1);
        }

        // Per-strategy limit
        if let Some(&limit) = self.limits.get(strategy_id) {
            if limit > 0 {
                let key = (
                    account.to_string(),
                    strategy_id.to_string(),
                    symbol.to_string(),
                );
                let current = self.exposure.get(&key).copied().unwrap_or(0);
                if current + notional > limit {
                    return (false, 2);
                }
            }
        }

        // Symbol cardinality bound
        let key = (
            account.to_string(),
            strategy_id.to_string(),
            symbol.to_string(),
        );
        let is_new = !self.exposure.contains_key(&key);
        if is_new && self.exposure.len() >= self.max_symbols {
            // Evict zero entries
            self.exposure.retain(|_, v| *v != 0);
            if self.exposure.len() >= self.max_symbols {
                return (false, 3);
            }
        }

        // Commit
        self.global_notional += notional;
        let entry = self.exposure.entry(key).or_insert(0);
        *entry += notional;

        (true, 0)
    }

    /// Release exposure on fill/cancel/reject.
    pub fn release(
        &mut self,
        account: &str,
        strategy_id: &str,
        symbol: &str,
        intent_type: i32,
        price: i64,
        qty: i64,
    ) {
        if intent_type == 2 {
            return;
        }
        let notional = price * qty;
        self.global_notional = (self.global_notional - notional).max(0);
        let key = (
            account.to_string(),
            strategy_id.to_string(),
            symbol.to_string(),
        );
        if let Some(v) = self.exposure.get_mut(&key) {
            *v = (*v - notional).max(0);
        }
    }

    /// Get current exposure for a specific key.
    pub fn get_exposure(&self, account: &str, strategy_id: &str, symbol: &str) -> i64 {
        let key = (
            account.to_string(),
            strategy_id.to_string(),
            symbol.to_string(),
        );
        self.exposure.get(&key).copied().unwrap_or(0)
    }

    /// Get global notional.
    pub fn get_global_notional(&self) -> i64 {
        self.global_notional
    }

    /// Number of tracked (account, strategy, symbol) entries.
    pub fn size(&self) -> usize {
        self.exposure.len()
    }

    /// Reason code to string.
    #[staticmethod]
    pub fn reason_str(code: u8) -> &'static str {
        match code {
            0 => "OK",
            1 => "GLOBAL_EXPOSURE_LIMIT",
            2 => "STRATEGY_EXPOSURE_LIMIT",
            3 => "SYMBOL_LIMIT_REACHED",
            _ => "UNKNOWN",
        }
    }
}
