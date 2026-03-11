//! RustDedupStore — LRU idempotency cache.
//!
//! Replaces Python OrderedDict-based LRU with Rust Vec-based implementation.
//! Uses a HashMap for O(1) lookup + Vec for LRU ordering.
//! No disk persistence (handled by Python wrapper for backward compat).

use pyo3::prelude::*;
use std::collections::HashMap;

/// Record state: None=-1, true=1, false=0
const RESERVED: i8 = -1;
const REJECTED: i8 = 0;
const APPROVED: i8 = 1;

#[derive(Clone)]
struct DedupRecord {
    approved: i8, // -1=reserved, 0=rejected, 1=approved
    reason_code: String,
    cmd_id: i64,
    order: u64, // insertion/access order for LRU
}

#[pyclass]
pub struct RustDedupStore {
    window_size: usize,
    records: HashMap<String, DedupRecord>,
    order_counter: u64,
    // Track the minimum order to know what to evict
}

#[pymethods]
impl RustDedupStore {
    #[new]
    pub fn new(window_size: usize) -> Self {
        RustDedupStore {
            window_size,
            records: HashMap::with_capacity(window_size.min(1024)),
            order_counter: 0,
        }
    }

    /// Check if key exists; if not, reserve it.
    ///
    /// Returns:
    ///   (is_hit: bool, approved: i8, reason_code: str, cmd_id: i64)
    ///   is_hit=false means new key was reserved
    ///   is_hit=true means existing record found (approved/reason/cmd_id populated)
    pub fn check_or_reserve(&mut self, key: &str) -> (bool, i8, String, i64) {
        if key.is_empty() {
            return (false, RESERVED, String::new(), 0);
        }

        if let Some(rec) = self.records.get_mut(key) {
            // Hit: update LRU order
            self.order_counter += 1;
            rec.order = self.order_counter;
            return (true, rec.approved, rec.reason_code.clone(), rec.cmd_id);
        }

        // Miss: reserve
        self.order_counter += 1;
        let rec = DedupRecord {
            approved: RESERVED,
            reason_code: String::new(),
            cmd_id: 0,
            order: self.order_counter,
        };
        self.records.insert(key.to_string(), rec);

        // Evict oldest if over window
        if self.records.len() > self.window_size {
            self._evict_oldest();
        }

        (false, RESERVED, String::new(), 0)
    }

    /// Record final decision for a key.
    pub fn commit(&mut self, key: &str, approved: bool, reason_code: &str, cmd_id: i64) {
        if key.is_empty() {
            return;
        }
        let approved_val = if approved { APPROVED } else { REJECTED };
        if let Some(rec) = self.records.get_mut(key) {
            rec.approved = approved_val;
            rec.reason_code = reason_code.to_string();
            rec.cmd_id = cmd_id;
        } else {
            self.order_counter += 1;
            self.records.insert(
                key.to_string(),
                DedupRecord {
                    approved: approved_val,
                    reason_code: reason_code.to_string(),
                    cmd_id,
                    order: self.order_counter,
                },
            );
        }
    }

    /// Number of entries.
    pub fn size(&self) -> usize {
        self.records.len()
    }

    /// Check if a key exists without modifying LRU order.
    pub fn contains(&self, key: &str) -> bool {
        self.records.contains_key(key)
    }
}

impl RustDedupStore {
    fn _evict_oldest(&mut self) {
        if self.records.is_empty() {
            return;
        }
        // Find key with minimum order
        let mut min_order = u64::MAX;
        let mut min_key = String::new();
        for (k, rec) in &self.records {
            if rec.order < min_order {
                min_order = rec.order;
                min_key = k.clone();
            }
        }
        if !min_key.is_empty() {
            self.records.remove(&min_key);
        }
    }
}
