//! RustNormalizerFeatureFusedV1 — Fused normalize_bidask + BookState + FeatureEngine in 1 GIL crossing.
//!
//! Eliminates Python intermediation between normalize → LOB update → feature compute stages.
//! All prices are i64 scaled (Precision Law). No heap allocation on repeat calls (Allocator Law).
//!
//! ## Intentional Duplication
//!
//! `BookStateInner` and `FeatureKernelInner` are intentionally duplicated from
//! `normalizer_lob_fused.rs` and `feature.rs` respectively. This avoids cross-module
//! state sharing overhead in the fused pipeline, keeping the normalize → LOB → feature
//! computation in a single tight loop without indirect function calls or trait dispatch.

use std::collections::HashMap;

use ndarray::Array2;
use numpy::{IntoPyArray, PyReadonlyArray1};
use pyo3::prelude::*;

const FEATURE_COUNT: usize = 16;
type FeatureArray = [i64; FEATURE_COUNT];
const DEFAULT_EMA_ALPHA: f64 = 2.0 / 9.0;

/// Check if a slice of `[i64; 2]` is sorted descending by price (index 0).
#[inline(always)]
fn is_sorted_desc(levels: &[[i64; 2]]) -> bool {
    levels.windows(2).all(|w| w[0][0] >= w[1][0])
}

/// Check if a slice of `[i64; 2]` is sorted ascending by price (index 0).
#[inline(always)]
fn is_sorted_asc(levels: &[[i64; 2]]) -> bool {
    levels.windows(2).all(|w| w[0][0] <= w[1][0])
}

/// Internal per-symbol book state (duplicated from normalizer_lob_fused.rs).
struct BookStateInner {
    bids: Vec<[i64; 2]>,
    asks: Vec<[i64; 2]>,
    version: u64,
    best_bid: i64,
    best_ask: i64,
    bid_depth: i64,
    ask_depth: i64,
    mid_x2: i64,
    spread_scaled: i64,
    imbalance_ppm: i64,
    top_imbalance: f64,
}

impl BookStateInner {
    fn new() -> Self {
        Self {
            bids: Vec::with_capacity(8),
            asks: Vec::with_capacity(8),
            version: 0,
            best_bid: 0,
            best_ask: 0,
            bid_depth: 0,
            ask_depth: 0,
            mid_x2: 0,
            spread_scaled: 0,
            imbalance_ppm: 0,
            top_imbalance: 0.0,
        }
    }

    #[inline(always)]
    fn recompute_stats(&mut self) {
        self.best_bid = if self.bids.is_empty() {
            0
        } else {
            self.bids[0][0]
        };
        self.best_ask = if self.asks.is_empty() {
            0
        } else {
            self.asks[0][0]
        };
        self.bid_depth = self.bids.iter().map(|r| r[1]).sum();
        self.ask_depth = self.asks.iter().map(|r| r[1]).sum();

        if self.best_bid > 0 && self.best_ask > 0 {
            self.mid_x2 = self.best_bid + self.best_ask;
            self.spread_scaled = self.best_ask - self.best_bid;
            let total = self.bid_depth + self.ask_depth;
            self.imbalance_ppm = if total > 0 {
                (self.bid_depth - self.ask_depth) * 1_000_000 / total
            } else {
                0
            };
            let bv_top = self.bids[0][1];
            let av_top = self.asks[0][1];
            let top_total = bv_top + av_top;
            self.top_imbalance = if top_total > 0 {
                (bv_top - av_top) as f64 / top_total as f64
            } else {
                0.0
            };
        } else {
            self.mid_x2 = 0;
            self.spread_scaled = 0;
            self.imbalance_ppm = 0;
            self.top_imbalance = 0.0;
        }
    }
}

/// Internal feature kernel state (duplicated from feature.rs).
struct FeatureKernelInner {
    prev_best_bid: i64,
    prev_best_ask: i64,
    prev_l1_bid_qty: i64,
    prev_l1_ask_qty: i64,
    ofi_l1_cum: i64,
    ofi_l1_ema8: f64,
    spread_ema8: f64,
    imbalance_ema8_ppm: f64,
    initialized: bool,
    ema_alpha: f64,
}

impl FeatureKernelInner {
    fn new(ema_alpha: f64) -> Self {
        Self {
            prev_best_bid: 0,
            prev_best_ask: 0,
            prev_l1_bid_qty: 0,
            prev_l1_ask_qty: 0,
            ofi_l1_cum: 0,
            ofi_l1_ema8: 0.0,
            spread_ema8: 0.0,
            imbalance_ema8_ppm: 0.0,
            initialized: false,
            ema_alpha,
        }
    }

    #[inline(always)]
    #[allow(clippy::too_many_arguments)]
    fn compute(
        &mut self,
        best_bid: i64,
        best_ask: i64,
        mid_price_x2: i64,
        spread_scaled: i64,
        bid_depth: i64,
        ask_depth: i64,
        l1_bid_qty: i64,
        l1_ask_qty: i64,
    ) -> FeatureArray {
        let bid_depth = bid_depth.max(0);
        let ask_depth = ask_depth.max(0);
        let l1_bid_qty = l1_bid_qty.max(0);
        let l1_ask_qty = l1_ask_qty.max(0);

        let depth_total = bid_depth + ask_depth;
        let imbalance_ppm = if depth_total > 0 {
            py_round_i64(((bid_depth - ask_depth) as f64 * 1_000_000.0) / depth_total as f64)
        } else {
            0
        };

        let l1_total = l1_bid_qty + l1_ask_qty;
        let (l1_imbalance_ppm, microprice_x2) = if l1_total > 0 {
            let l1_imb =
                py_round_i64(((l1_bid_qty - l1_ask_qty) as f64 * 1_000_000.0) / l1_total as f64);
            let mp = py_round_i64(
                (2.0 * ((best_ask * l1_bid_qty + best_bid * l1_ask_qty) as f64)) / l1_total as f64,
            );
            (l1_imb, mp)
        } else {
            (0, mid_price_x2)
        };

        let (ofi_l1_raw, ofi_l1_cum, ofi_l1_ema8, spread_ema8_scaled, depth_imbalance_ema8_ppm) =
            if !self.initialized {
                self.spread_ema8 = spread_scaled as f64;
                self.imbalance_ema8_ppm = l1_imbalance_ppm as f64;
                self.initialized = true;
                (
                    0_i64,
                    0_i64,
                    0_i64,
                    py_round_i64(self.spread_ema8),
                    py_round_i64(self.imbalance_ema8_ppm),
                )
            } else {
                let b_flow = if best_bid > self.prev_best_bid {
                    l1_bid_qty
                } else if best_bid == self.prev_best_bid {
                    l1_bid_qty - self.prev_l1_bid_qty
                } else {
                    -self.prev_l1_bid_qty
                };

                let a_flow = if best_ask > self.prev_best_ask {
                    -self.prev_l1_ask_qty
                } else if best_ask == self.prev_best_ask {
                    l1_ask_qty - self.prev_l1_ask_qty
                } else {
                    l1_ask_qty
                };

                let ofi_raw = b_flow - a_flow;
                self.ofi_l1_cum += ofi_raw;
                let alpha = self.ema_alpha;
                self.ofi_l1_ema8 = (1.0 - alpha) * self.ofi_l1_ema8 + alpha * ofi_raw as f64;
                self.spread_ema8 = (1.0 - alpha) * self.spread_ema8 + alpha * spread_scaled as f64;
                self.imbalance_ema8_ppm =
                    (1.0 - alpha) * self.imbalance_ema8_ppm + alpha * l1_imbalance_ppm as f64;

                (
                    ofi_raw,
                    self.ofi_l1_cum,
                    py_round_i64(self.ofi_l1_ema8),
                    py_round_i64(self.spread_ema8),
                    py_round_i64(self.imbalance_ema8_ppm),
                )
            };

        self.prev_best_bid = best_bid;
        self.prev_best_ask = best_ask;
        self.prev_l1_bid_qty = l1_bid_qty;
        self.prev_l1_ask_qty = l1_ask_qty;

        [
            best_bid,
            best_ask,
            mid_price_x2,
            spread_scaled,
            bid_depth,
            ask_depth,
            imbalance_ppm,
            microprice_x2,
            l1_bid_qty,
            l1_ask_qty,
            l1_imbalance_ppm,
            ofi_l1_raw,
            ofi_l1_cum,
            ofi_l1_ema8,
            spread_ema8_scaled,
            depth_imbalance_ema8_ppm,
        ]
    }
}

#[inline(always)]
fn py_round_i64(x: f64) -> i64 {
    x.round_ties_even() as i64
}

/// Per-symbol fused state: book + feature kernel.
struct SymbolFusedState {
    book: BookStateInner,
    feature: FeatureKernelInner,
}

impl SymbolFusedState {
    fn new(ema_alpha: f64) -> Self {
        Self {
            book: BookStateInner::new(),
            feature: FeatureKernelInner::new(ema_alpha),
        }
    }
}

/// Convert Vec<[i64; 2]> to a numpy (N, 2) i64 array.
#[inline]
fn levels_to_numpy(py: Python<'_>, levels: &[[i64; 2]]) -> PyObject {
    let n = levels.len();
    if n == 0 {
        let arr = Array2::<i64>::zeros((0, 2));
        return arr.into_pyarray_bound(py).into();
    }
    let mut arr = Array2::<i64>::zeros((n, 2));
    for (i, row) in levels.iter().enumerate() {
        arr[[i, 0]] = row[0];
        arr[[i, 1]] = row[1];
    }
    arr.into_pyarray_bound(py).into()
}

/// Extract f64 slice from either numpy array (zero-copy) or Python sequence (fallback).
#[inline]
fn extract_f64_vec(obj: &Bound<'_, PyAny>) -> PyResult<Vec<f64>> {
    if let Ok(arr) = obj.extract::<PyReadonlyArray1<f64>>() {
        return Ok(arr.as_array().to_vec());
    }
    obj.extract::<Vec<f64>>()
}

/// Extract i64 slice from either numpy array (zero-copy) or Python sequence (fallback).
#[inline]
fn extract_i64_vec(obj: &Bound<'_, PyAny>) -> PyResult<Vec<i64>> {
    if let Ok(arr) = obj.extract::<PyReadonlyArray1<i64>>() {
        return Ok(arr.as_array().to_vec());
    }
    obj.extract::<Vec<i64>>()
}

#[pyclass]
pub struct RustNormalizerFeatureFusedV1 {
    states: HashMap<String, SymbolFusedState>,
    ema_alpha: f64,
}

#[pymethods]
impl RustNormalizerFeatureFusedV1 {
    #[new]
    #[pyo3(signature = (ema_alpha=None))]
    pub fn new(ema_alpha: Option<f64>) -> Self {
        Self {
            states: HashMap::with_capacity(16),
            ema_alpha: ema_alpha.unwrap_or(DEFAULT_EMA_ALPHA),
        }
    }

    /// Process bidask update: scale prices → update book → compute stats → compute 16 features.
    /// Returns tuple:
    ///   (bids_np, asks_np, best_bid, best_ask, bid_depth, ask_depth,
    ///    mid_x2, spread_scaled, imbalance_ppm, version, top_imbalance,
    ///    feature_values[16])
    #[allow(clippy::too_many_arguments)]
    #[pyo3(signature = (symbol, bid_prices, bid_volumes, ask_prices, ask_volumes, scale, tick_size_scaled))]
    pub fn process_bidask_with_features(
        &mut self,
        py: Python<'_>,
        symbol: &str,
        bid_prices: &Bound<'_, PyAny>,
        bid_volumes: &Bound<'_, PyAny>,
        ask_prices: &Bound<'_, PyAny>,
        ask_volumes: &Bound<'_, PyAny>,
        scale: i64,
        tick_size_scaled: i64,
    ) -> PyResult<PyObject> {
        let _ = tick_size_scaled;

        let bp = extract_f64_vec(bid_prices)?;
        let bv = extract_i64_vec(bid_volumes)?;
        let ap = extract_f64_vec(ask_prices)?;
        let av = extract_i64_vec(ask_volumes)?;

        let ema_alpha = self.ema_alpha;
        let state = self
            .states
            .entry(symbol.to_owned())
            .or_insert_with(|| SymbolFusedState::new(ema_alpha));

        // Build bid levels
        let n_bids = bp.len().min(bv.len());
        state.book.bids.clear();
        for i in 0..n_bids {
            let p = bp[i];
            if p == 0.0 {
                continue;
            }
            let scaled = (p * scale as f64).round_ties_even() as i64;
            if scaled <= 0 {
                continue;
            }
            state.book.bids.push([scaled, bv[i]]);
        }
        // Sort bids descending by price — skip if already sorted (broker pre-sorted fast path)
        if !is_sorted_desc(&state.book.bids) {
            state.book.bids.sort_unstable_by(|a, b| b[0].cmp(&a[0]));
        }

        // Build ask levels
        let n_asks = ap.len().min(av.len());
        state.book.asks.clear();
        for i in 0..n_asks {
            let p = ap[i];
            if p == 0.0 {
                continue;
            }
            let scaled = (p * scale as f64).round_ties_even() as i64;
            if scaled <= 0 {
                continue;
            }
            state.book.asks.push([scaled, av[i]]);
        }
        // Sort asks ascending by price — skip if already sorted (broker pre-sorted fast path)
        if !is_sorted_asc(&state.book.asks) {
            state.book.asks.sort_unstable_by(|a, b| a[0].cmp(&b[0]));
        }

        // Compute book stats
        state.book.recompute_stats();
        state.book.version += 1;

        // Extract L1 quantities for feature kernel
        let l1_bid_qty = if state.book.bids.is_empty() {
            0
        } else {
            state.book.bids[0][1]
        };
        let l1_ask_qty = if state.book.asks.is_empty() {
            0
        } else {
            state.book.asks[0][1]
        };

        // Compute features in the same GIL crossing
        let features = state.feature.compute(
            state.book.best_bid,
            state.book.best_ask,
            state.book.mid_x2,
            state.book.spread_scaled,
            state.book.bid_depth,
            state.book.ask_depth,
            l1_bid_qty,
            l1_ask_qty,
        );

        let bids_np = levels_to_numpy(py, &state.book.bids);
        let asks_np = levels_to_numpy(py, &state.book.asks);

        // Return book stats + feature values as a single tuple
        Ok((
            bids_np,
            asks_np,
            state.book.best_bid,
            state.book.best_ask,
            state.book.bid_depth,
            state.book.ask_depth,
            state.book.mid_x2,
            state.book.spread_scaled,
            state.book.imbalance_ppm,
            state.book.version,
            state.book.top_imbalance,
            features,
        )
            .into_py(py))
    }

    /// Reset state for a single symbol.
    pub fn reset_symbol(&mut self, symbol: &str) {
        self.states.remove(symbol);
    }

    /// Reset all state.
    pub fn reset_all(&mut self) {
        self.states.clear();
    }

    /// Get the book version for a symbol (0 if unknown).
    pub fn get_version(&self, symbol: &str) -> u64 {
        self.states.get(symbol).map_or(0, |s| s.book.version)
    }
}

impl Default for RustNormalizerFeatureFusedV1 {
    fn default() -> Self {
        Self::new(None)
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn test_is_sorted_helpers() {
        // Bids descending — already sorted
        assert!(is_sorted_desc(&[
            [1_050_000, 100],
            [1_040_000, 200],
            [1_030_000, 50]
        ]));
        // Asks ascending — already sorted
        assert!(is_sorted_asc(&[
            [1_060_000, 150],
            [1_070_000, 50],
            [1_080_000, 30]
        ]));
        // Unsorted
        assert!(!is_sorted_desc(&[[1_030_000, 50], [1_050_000, 100]]));
        assert!(!is_sorted_asc(&[[1_080_000, 30], [1_060_000, 150]]));
        // Empty and single-element are always sorted
        assert!(is_sorted_desc(&[]));
        assert!(is_sorted_asc(&[]));
        assert!(is_sorted_desc(&[[100, 1]]));
        assert!(is_sorted_asc(&[[100, 1]]));
    }

    #[test]
    fn test_py_round_i64() {
        assert_eq!(py_round_i64(2.5), 2); // ties-even rounds to even
        assert_eq!(py_round_i64(3.5), 4);
        assert_eq!(py_round_i64(-0.5), 0);
        assert_eq!(py_round_i64(1.6), 2);
        assert_eq!(py_round_i64(-1.6), -2);
    }

    #[test]
    fn test_book_state_recompute_basic() {
        let mut book = BookStateInner::new();
        book.bids = vec![[1_000_000, 100], [990_000, 200]];
        book.asks = vec![[1_010_000, 150], [1_020_000, 50]];
        book.recompute_stats();

        assert_eq!(book.best_bid, 1_000_000);
        assert_eq!(book.best_ask, 1_010_000);
        assert_eq!(book.mid_x2, 2_010_000);
        assert_eq!(book.spread_scaled, 10_000);
        assert_eq!(book.bid_depth, 300);
        assert_eq!(book.ask_depth, 200);
        // imbalance_ppm = (300 - 200) * 1_000_000 / 500 = 200_000
        assert_eq!(book.imbalance_ppm, 200_000);
    }

    #[test]
    fn test_book_state_empty_sides() {
        let mut book = BookStateInner::new();
        book.recompute_stats();
        assert_eq!(book.best_bid, 0);
        assert_eq!(book.best_ask, 0);
        assert_eq!(book.mid_x2, 0);
        assert_eq!(book.spread_scaled, 0);
        assert_eq!(book.imbalance_ppm, 0);
    }

    #[test]
    fn test_book_state_one_sided() {
        let mut book = BookStateInner::new();
        book.bids = vec![[1_000_000, 100]];
        book.recompute_stats();
        assert_eq!(book.best_bid, 1_000_000);
        assert_eq!(book.best_ask, 0);
        assert_eq!(book.mid_x2, 0); // needs both sides
    }

    #[test]
    fn test_feature_kernel_first_tick() {
        let mut kernel = FeatureKernelInner::new(DEFAULT_EMA_ALPHA);
        let features = kernel.compute(1_000_000, 1_010_000, 2_010_000, 10_000, 300, 200, 100, 150);

        // First tick: OFI should be 0 (no previous data)
        assert_eq!(features[0], 1_000_000); // best_bid
        assert_eq!(features[1], 1_010_000); // best_ask
        assert_eq!(features[2], 2_010_000); // mid_price_x2
        assert_eq!(features[3], 10_000); // spread_scaled
        assert_eq!(features[11], 0); // ofi_l1_raw = 0 on first tick
        assert_eq!(features[12], 0); // ofi_l1_cum = 0 on first tick
        assert!(kernel.initialized);
    }

    #[test]
    fn test_feature_kernel_ofi_computation() {
        let mut kernel = FeatureKernelInner::new(DEFAULT_EMA_ALPHA);
        // First tick (initialization)
        kernel.compute(1_000_000, 1_010_000, 2_010_000, 10_000, 300, 200, 100, 150);

        // Second tick: bid qty increases by 50 (same price), ask unchanged
        let f2 = kernel.compute(1_000_000, 1_010_000, 2_010_000, 10_000, 350, 200, 150, 150);

        // b_flow = 150 - 100 = 50 (same price), a_flow = 150 - 150 = 0
        // ofi_raw = 50 - 0 = 50
        assert_eq!(f2[11], 50); // ofi_l1_raw
        assert_eq!(f2[12], 50); // ofi_l1_cum
    }

    #[test]
    fn test_feature_kernel_microprice() {
        let mut kernel = FeatureKernelInner::new(DEFAULT_EMA_ALPHA);
        // bid=100, ask=102, bid_qty=200, ask_qty=100
        // microprice_x2 = 2 * (102*200 + 100*100) / 300 = 2 * 30400 / 300 = 202.666... → 203 (rounded)
        let features = kernel.compute(100, 102, 202, 2, 300, 200, 200, 100);
        let mp_x2 = features[7]; // microprice_x2
                                 // microprice_x2 = round(2.0 * (102*200 + 100*100) / 300) = round(202.666...) = 203
        assert_eq!(mp_x2, 203);
    }

    // --- Additional BookStateInner tests ---

    #[test]
    fn test_book_state_top_imbalance() {
        let mut book = BookStateInner::new();
        book.bids = vec![[1_050_000, 100]];
        book.asks = vec![[1_060_000, 100]];
        book.recompute_stats();
        assert_eq!(book.top_imbalance, 0.0); // Equal top vol

        let mut book2 = BookStateInner::new();
        book2.bids = vec![[1_050_000, 80]];
        book2.asks = vec![[1_060_000, 20]];
        book2.recompute_stats();
        assert!((book2.top_imbalance - 0.6).abs() < 1e-10);
    }

    #[test]
    fn test_book_state_empty_top_imbalance() {
        let mut book = BookStateInner::new();
        book.recompute_stats();
        assert_eq!(book.top_imbalance, 0.0);
    }

    #[test]
    fn test_book_version_tracking() {
        let mut book = BookStateInner::new();
        assert_eq!(book.version, 0);
        book.bids.push([100, 50]);
        book.asks.push([101, 50]);
        book.recompute_stats();
        book.version += 1;
        assert_eq!(book.version, 1);
    }

    // --- Additional FeatureKernelInner tests ---

    #[test]
    fn test_feature_kernel_ofi_direction() {
        let mut kernel = FeatureKernelInner::new(DEFAULT_EMA_ALPHA);
        // Init
        kernel.compute(100_0000, 101_0000, 201_0000, 1_0000, 50, 40, 50, 40);
        // Bid price up → b_flow = l1_bid_qty; ask price up → a_flow = -prev_l1_ask_qty
        let features = kernel.compute(101_0000, 102_0000, 203_0000, 1_0000, 60, 35, 60, 35);
        // b_flow = 60 (bid up), a_flow = -40 (ask up) → ofi = 60 - (-40) = 100
        assert_eq!(features[11], 100);
        assert_eq!(features[12], 100); // cum
    }

    #[test]
    fn test_feature_kernel_ema_convergence() {
        let mut kernel = FeatureKernelInner::new(DEFAULT_EMA_ALPHA);
        // Feed identical inputs many times — EMA should converge to the constant input
        for _ in 0..200 {
            kernel.compute(100_0000, 101_0000, 201_0000, 1_0000, 100, 100, 50, 50);
        }
        let features = kernel.compute(100_0000, 101_0000, 201_0000, 1_0000, 100, 100, 50, 50);
        // spread_ema should converge to spread_scaled = 1_0000
        assert_eq!(features[14], 1_0000);
    }

    // --- Fused pipeline end-to-end test (pure Rust, no Python) ---

    #[test]
    fn test_fused_pipeline_sorted_input() {
        let mut state = SymbolFusedState::new(DEFAULT_EMA_ALPHA);

        // Build sorted bids (desc) and asks (asc)
        state.book.bids = vec![[100_0000, 50], [99_0000, 30]];
        state.book.asks = vec![[101_0000, 40], [102_0000, 20]];
        state.book.recompute_stats();
        state.book.version += 1;

        let l1_bid_qty = state.book.bids[0][1];
        let l1_ask_qty = state.book.asks[0][1];

        let features = state.feature.compute(
            state.book.best_bid,
            state.book.best_ask,
            state.book.mid_x2,
            state.book.spread_scaled,
            state.book.bid_depth,
            state.book.ask_depth,
            l1_bid_qty,
            l1_ask_qty,
        );

        assert_eq!(features[0], 100_0000); // best_bid
        assert_eq!(features[1], 101_0000); // best_ask
        assert_eq!(features[2], 201_0000); // mid_x2
        assert_eq!(features[3], 1_0000); // spread
        assert_eq!(features[4], 80); // bid_depth (50+30)
        assert_eq!(features[5], 60); // ask_depth (40+20)
    }

    // --- Additional py_round_i64 tests ---

    #[test]
    fn test_py_round_ties_even_extended() {
        // Banker's rounding: 0.5 rounds to even
        assert_eq!(py_round_i64(0.5), 0); // rounds to 0 (even)
        assert_eq!(py_round_i64(1.5), 2); // rounds to 2 (even)
        assert_eq!(py_round_i64(2.5), 2); // rounds to 2 (even)
        assert_eq!(py_round_i64(3.5), 4); // rounds to 4 (even)
        assert_eq!(py_round_i64(-0.5), 0); // rounds to 0 (even)
        assert_eq!(py_round_i64(-1.5), -2); // rounds to -2 (even)
    }

    #[test]
    fn test_py_round_normal_cases() {
        assert_eq!(py_round_i64(1.3), 1);
        assert_eq!(py_round_i64(1.7), 2);
        assert_eq!(py_round_i64(-1.3), -1);
        assert_eq!(py_round_i64(-1.7), -2);
    }
}
