use numpy::PyReadonlyArray2;
use pyo3::prelude::*;

/// Per-symbol LOB book state managed entirely in Rust.
///
/// Replaces the Python `BookState._recompute()` hot path with pure integer
/// arithmetic.  The caller (Python `LOBEngine`) still manages the per-symbol
/// lock; this struct is NOT internally synchronized.
#[pyclass]
pub struct RustBookState {
    symbol: String,
    // Flattened [price, qty] pairs -- contiguous for cache locality.
    bids_flat: Vec<i64>,
    asks_flat: Vec<i64>,
    bid_rows: usize,
    ask_rows: usize,
    exch_ts: i64,
    version: u64,
    // Pre-computed stats (updated on every apply_update).
    mid_price_x2: i64,
    spread: i64,
    imbalance: f64,
    last_price: i64,
    last_volume: i64,
    bid_depth_total: i64,
    ask_depth_total: i64,
}

unsafe impl Send for RustBookState {}

#[pymethods]
impl RustBookState {
    #[new]
    pub fn new(symbol: String) -> Self {
        Self {
            symbol,
            bids_flat: Vec::new(),
            asks_flat: Vec::new(),
            bid_rows: 0,
            ask_rows: 0,
            exch_ts: 0,
            version: 0,
            mid_price_x2: 0,
            spread: 0,
            imbalance: 0.0,
            last_price: 0,
            last_volume: 0,
            bid_depth_total: 0,
            ask_depth_total: 0,
        }
    }

    /// Atomic book update from numpy arrays (shape Nx2, dtype i64).
    /// Recomputes all stats internally -- no Python fallback needed.
    pub fn apply_update(
        &mut self,
        bids: PyReadonlyArray2<i64>,
        asks: PyReadonlyArray2<i64>,
        exch_ts: i64,
    ) -> bool {
        if exch_ts < self.exch_ts {
            return false; // late packet
        }
        self.exch_ts = exch_ts;
        self._ingest_sides(&bids, &asks);
        self._recompute();
        self.version += 1;
        true
    }

    /// Pre-computed stats path (normalizer already computed stats in Rust).
    #[allow(clippy::too_many_arguments)]
    pub fn apply_update_with_stats(
        &mut self,
        bids: PyReadonlyArray2<i64>,
        asks: PyReadonlyArray2<i64>,
        exch_ts: i64,
        best_bid: i64,
        best_ask: i64,
        bid_depth: i64,
        ask_depth: i64,
        imbalance: f64,
    ) -> bool {
        if exch_ts < self.exch_ts {
            return false;
        }
        self.exch_ts = exch_ts;
        self._ingest_sides(&bids, &asks);
        self.bid_depth_total = bid_depth;
        self.ask_depth_total = ask_depth;
        self.mid_price_x2 = best_bid + best_ask;
        self.spread = best_ask - best_bid;
        self.imbalance = imbalance;
        self.version += 1;
        true
    }

    pub fn update_tick(&mut self, price: i64, volume: i64, exch_ts: i64) -> bool {
        if exch_ts < self.exch_ts {
            return false;
        }
        self.exch_ts = exch_ts;
        self.last_price = price;
        self.last_volume = volume;
        true
    }

    /// Low-allocation stats tuple matching Python BookState.get_stats_tuple().
    /// Returns: (symbol, exch_ts, mid_price_x2, spread, imbalance,
    ///           best_bid, best_ask, bid_depth_total, ask_depth_total)
    pub fn get_stats_tuple(
        &self,
    ) -> (String, i64, i64, i64, f64, i64, i64, i64, i64) {
        let best_bid = self._best_bid();
        let best_ask = self._best_ask();
        (
            self.symbol.clone(),
            self.exch_ts,
            self.mid_price_x2,
            self.spread,
            self.imbalance,
            best_bid,
            best_ask,
            self.bid_depth_total,
            self.ask_depth_total,
        )
    }

    /// L1 snapshot for strategy hot path.
    /// Returns: (exch_ts, best_bid, best_ask, mid_price_x2, spread,
    ///           bid_depth_total, ask_depth_total)
    pub fn get_l1_scaled(&self) -> (i64, i64, i64, i64, i64, i64, i64) {
        let best_bid = self._best_bid();
        let best_ask = self._best_ask();
        (
            self.exch_ts,
            best_bid,
            best_ask,
            self.mid_price_x2,
            self.spread,
            self.bid_depth_total,
            self.ask_depth_total,
        )
    }

    #[getter]
    pub fn version(&self) -> u64 {
        self.version
    }

    #[getter]
    pub fn exch_ts(&self) -> i64 {
        self.exch_ts
    }

    #[getter]
    pub fn mid_price_x2(&self) -> i64 {
        self.mid_price_x2
    }

    #[getter]
    pub fn spread(&self) -> i64 {
        self.spread
    }

    #[getter]
    pub fn imbalance(&self) -> f64 {
        self.imbalance
    }

    #[getter]
    pub fn bid_depth_total(&self) -> i64 {
        self.bid_depth_total
    }

    #[getter]
    pub fn ask_depth_total(&self) -> i64 {
        self.ask_depth_total
    }

    #[getter]
    pub fn last_price(&self) -> i64 {
        self.last_price
    }

    #[getter]
    pub fn last_volume(&self) -> i64 {
        self.last_volume
    }

    pub fn best_bid(&self) -> i64 {
        self._best_bid()
    }

    pub fn best_ask(&self) -> i64 {
        self._best_ask()
    }

    pub fn bid_rows(&self) -> usize {
        self.bid_rows
    }

    pub fn ask_rows(&self) -> usize {
        self.ask_rows
    }
}

impl RustBookState {
    fn _ingest_sides(
        &mut self,
        bids: &PyReadonlyArray2<i64>,
        asks: &PyReadonlyArray2<i64>,
    ) {
        let bids_arr = bids.as_array();
        let asks_arr = asks.as_array();

        let br = bids_arr.nrows();
        let ar = asks_arr.nrows();

        // Resize flat buffers (reuse allocation when possible).
        self.bids_flat.resize(br * 2, 0);
        self.asks_flat.resize(ar * 2, 0);
        self.bid_rows = br;
        self.ask_rows = ar;

        for i in 0..br {
            self.bids_flat[i * 2] = bids_arr[[i, 0]];
            self.bids_flat[i * 2 + 1] = bids_arr[[i, 1]];
        }
        for i in 0..ar {
            self.asks_flat[i * 2] = asks_arr[[i, 0]];
            self.asks_flat[i * 2 + 1] = asks_arr[[i, 1]];
        }
    }

    fn _recompute(&mut self) {
        let mut best_bid: i64 = 0;
        let mut best_ask: i64 = 0;
        let mut bid_vol_top: i64 = 0;
        let mut ask_vol_top: i64 = 0;

        self.bid_depth_total = 0;
        self.ask_depth_total = 0;

        if self.bid_rows > 0 {
            best_bid = self.bids_flat[0];
            bid_vol_top = self.bids_flat[1];
            for i in 0..self.bid_rows {
                self.bid_depth_total += self.bids_flat[i * 2 + 1];
            }
        }

        if self.ask_rows > 0 {
            best_ask = self.asks_flat[0];
            ask_vol_top = self.asks_flat[1];
            for i in 0..self.ask_rows {
                self.ask_depth_total += self.asks_flat[i * 2 + 1];
            }
        }

        if best_bid > 0 && best_ask > 0 {
            self.mid_price_x2 = best_bid + best_ask;
            self.spread = best_ask - best_bid;
            let total_top = bid_vol_top + ask_vol_top;
            if total_top > 0 {
                self.imbalance = (bid_vol_top - ask_vol_top) as f64 / total_top as f64;
            } else {
                self.imbalance = 0.0;
            }
        } else {
            self.mid_price_x2 = 0;
            self.spread = 0;
            self.imbalance = 0.0;
        }
    }

    fn _best_bid(&self) -> i64 {
        if self.bid_rows > 0 {
            self.bids_flat[0]
        } else {
            0
        }
    }

    fn _best_ask(&self) -> i64 {
        if self.ask_rows > 0 {
            self.asks_flat[0]
        } else {
            0
        }
    }
}
