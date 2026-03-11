//! RustNormalizerLobFused — Fused normalize_bidask + BookState.apply_update + stats compute.
//!
//! Eliminates Python intermediation between normalize → LOB update → stats stages.
//! All prices are i64 scaled (Precision Law). No heap allocation on repeat calls
//! for the same symbol thanks to `Vec::clear()` + capacity reuse (Allocator Law).

use std::collections::HashMap;

use ndarray::Array2;
use numpy::IntoPyArray;
use pyo3::prelude::*;

/// Internal per-symbol book state.
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
    /// Top-of-book imbalance as f64, matching normalizer_bidask.rs convention:
    /// (bids[0][1] - asks[0][1]) / (bids[0][1] + asks[0][1])
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
            // Top-of-book imbalance (matches normalizer_bidask.rs convention)
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

/// Convert Vec<[i64; 2]> to a numpy (N, 2) i64 array.
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

#[pyclass]
pub struct RustNormalizerLobFused {
    books: HashMap<String, BookStateInner>,
}

#[pymethods]
impl RustNormalizerLobFused {
    #[new]
    pub fn new() -> Self {
        Self {
            books: HashMap::with_capacity(16),
        }
    }

    /// Process a full bidask update: scale prices, update book state, compute stats.
    ///
    /// Returns tuple:
    ///   (bids_np, asks_np, best_bid, best_ask, bid_depth, ask_depth,
    ///    mid_x2, spread_scaled, imbalance_ppm, version, top_imbalance)
    #[allow(clippy::too_many_arguments)]
    #[pyo3(signature = (symbol, bid_prices, bid_volumes, ask_prices, ask_volumes, scale, tick_size_scaled))]
    pub fn process_bidask(
        &mut self,
        py: Python<'_>,
        symbol: &str,
        bid_prices: Vec<f64>,
        bid_volumes: Vec<i64>,
        ask_prices: Vec<f64>,
        ask_volumes: Vec<i64>,
        scale: i64,
        tick_size_scaled: i64,
    ) -> PyResult<PyObject> {
        let _ = tick_size_scaled; // reserved for future synthetic side generation

        let book = self
            .books
            .entry(symbol.to_owned())
            .or_insert_with(BookStateInner::new);

        // Build bid levels: scale prices, filter zeros, pair with volumes
        let n_bids = bid_prices.len().min(bid_volumes.len());
        book.bids.clear();
        for i in 0..n_bids {
            let p = bid_prices[i];
            if p == 0.0 {
                continue;
            }
            let scaled = (p * scale as f64).round_ties_even() as i64;
            if scaled <= 0 {
                continue;
            }
            book.bids.push([scaled, bid_volumes[i]]);
        }
        // Sort bids descending by price
        book.bids.sort_unstable_by(|a, b| b[0].cmp(&a[0]));

        // Build ask levels
        let n_asks = ask_prices.len().min(ask_volumes.len());
        book.asks.clear();
        for i in 0..n_asks {
            let p = ask_prices[i];
            if p == 0.0 {
                continue;
            }
            let scaled = (p * scale as f64).round_ties_even() as i64;
            if scaled <= 0 {
                continue;
            }
            book.asks.push([scaled, ask_volumes[i]]);
        }
        // Sort asks ascending by price
        book.asks.sort_unstable_by(|a, b| a[0].cmp(&b[0]));

        // Compute stats
        book.recompute_stats();
        book.version += 1;

        // Build numpy arrays
        let bids_np = levels_to_numpy(py, &book.bids);
        let asks_np = levels_to_numpy(py, &book.asks);

        Ok((
            bids_np,
            asks_np,
            book.best_bid,
            book.best_ask,
            book.bid_depth,
            book.ask_depth,
            book.mid_x2,
            book.spread_scaled,
            book.imbalance_ppm,
            book.version,
            book.top_imbalance,
        )
            .into_py(py))
    }

    /// Get cached stats tuple for a symbol.
    /// Returns: (symbol, 0, mid_x2, spread_scaled, imbalance_f64, best_bid, best_ask, bid_depth, ask_depth)
    pub fn get_stats_tuple(&self, py: Python<'_>, symbol: &str) -> PyResult<Option<PyObject>> {
        match self.books.get(symbol) {
            Some(book) => {
                let imbalance_f64 = book.imbalance_ppm as f64 / 1_000_000.0;
                Ok(Some(
                    (
                        symbol.to_owned(),
                        0_i64,
                        book.mid_x2,
                        book.spread_scaled,
                        imbalance_f64,
                        book.best_bid,
                        book.best_ask,
                        book.bid_depth,
                        book.ask_depth,
                    )
                        .into_py(py),
                ))
            }
            None => Ok(None),
        }
    }

    /// Reset state for a single symbol.
    pub fn reset_symbol(&mut self, symbol: &str) {
        self.books.remove(symbol);
    }

    /// Reset all state.
    pub fn reset_all(&mut self) {
        self.books.clear();
    }

    /// Get the book version for a symbol (0 if unknown).
    pub fn get_version(&self, symbol: &str) -> u64 {
        self.books.get(symbol).map_or(0, |b| b.version)
    }
}

impl Default for RustNormalizerLobFused {
    fn default() -> Self {
        Self::new()
    }
}
