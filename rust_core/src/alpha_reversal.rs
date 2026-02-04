use crate::lob::LimitOrderBook;
use pyo3::prelude::*;

#[pyclass]
pub struct AlphaRegimeReversal {
    // Volatility Monitor
    vol_alpha: f64,
    ewma_variance: f64,
    prev_mid: f64,
    vol_threshold: f64,

    // SMA State
    window_size: usize,
    buffer: Vec<f64>,
    sum: f64,
    idx: usize,
    count: usize,

    // State
    initialized: bool,
}

#[pymethods]
impl AlphaRegimeReversal {
    #[new]
    pub fn new(vol_window: usize, vol_threshold: f64, sma_window: usize) -> Self {
        let vol_alpha = 2.0 / (vol_window as f64 + 1.0);

        AlphaRegimeReversal {
            // Volatility
            vol_alpha,
            ewma_variance: 0.0,
            prev_mid: f64::NAN,
            vol_threshold,

            // SMA
            window_size: sma_window,
            buffer: vec![0.0; sma_window],
            sum: 0.0,
            idx: 0,
            count: 0,

            initialized: false,
        }
    }

    pub fn calculate(&mut self, lob: &LimitOrderBook) -> f64 {
        // 1. Calculate Mid Price
        let best_bid_opt = lob.bids.iter().next_back();
        let best_ask_opt = lob.asks.iter().next();

        let (bid_p, _) = match best_bid_opt {
            Some((&p, &v)) => (p as f64 / 10000.0, v),
            None => return 0.0,
        };

        let (ask_p, _) = match best_ask_opt {
            Some((&p, &v)) => (p as f64 / 10000.0, v),
            None => return 0.0,
        };

        let mid = (bid_p + ask_p) / 2.0;

        // 2. Update Volatility (Same logic as AlphaRegimePressure)
        let mut current_vol = 0.0;

        if self.initialized {
            if mid > 0.0 && self.prev_mid > 0.0 {
                let ret = (mid - self.prev_mid) / self.prev_mid;
                let ret_sq = ret * ret;
                self.ewma_variance =
                    self.vol_alpha * ret_sq + (1.0 - self.vol_alpha) * self.ewma_variance;
                current_vol = self.ewma_variance.sqrt();
            }
        } else {
            self.initialized = true;
        }
        self.prev_mid = mid;

        // 3. Update SMA
        // O(1) rolling sum
        let old_val = self.buffer[self.idx];
        self.buffer[self.idx] = mid;

        if self.count < self.window_size {
            // Filling buffer
            self.sum += mid;
            self.count += 1;
        } else {
            // Window full
            self.sum = self.sum - old_val + mid;
        }

        self.idx = (self.idx + 1) % self.window_size;

        // 4. Check Regime
        if current_vol < self.vol_threshold {
            return 0.0; // Gate Closed
        }

        // 5. Calculate Reversal Signal
        // Deviation = (Price - MA) / MA
        // Signal = -Deviation
        if self.count > 0 {
            let ma = self.sum / (self.count as f64);
            if ma > 1e-9 {
                let deviation = (mid - ma) / ma;
                return -deviation;
            }
        }

        0.0
    }

    #[getter]
    pub fn get_current_vol(&self) -> f64 {
        self.ewma_variance.sqrt()
    }

    #[getter]
    pub fn get_current_ma(&self) -> f64 {
        if self.count > 0 {
            self.sum / (self.count as f64)
        } else {
            0.0
        }
    }
}
