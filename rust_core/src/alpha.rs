use crate::lob::LimitOrderBook;
use pyo3::prelude::*;

#[pyclass]
pub struct AlphaDepthSlope {
    alpha: f64,
    ewma_signal: f64,
    initialized: bool,
}

#[pymethods]
impl AlphaDepthSlope {
    #[new]
    pub fn new(window_size: usize) -> Self {
        let alpha = 2.0 / (window_size as f64 + 1.0);
        AlphaDepthSlope {
            alpha,
            ewma_signal: 0.0,
            initialized: false,
        }
    }

    pub fn calculate(&mut self, lob: &LimitOrderBook) -> f64 {
        // Compute depth slope similar to Python implementation
        // We need top N levels. Let's use 10 levels for regressions.

        let depth_levels = 10;

        // Helper to compute slope
        // Returns slope of (level_idx vs log(volume))
        let bid_slope = Self::compute_side_slope(&lob.bids, depth_levels, true);
        let ask_slope = Self::compute_side_slope(&lob.asks, depth_levels, false);

        // Raw Signal
        let raw_signal = bid_slope - ask_slope;

        // EWMA Smoothing
        if !self.initialized {
            self.ewma_signal = raw_signal;
            self.initialized = true;
        } else {
            self.ewma_signal = self.alpha * raw_signal + (1.0 - self.alpha) * self.ewma_signal;
        }

        self.ewma_signal
    }
}

impl AlphaDepthSlope {
    fn compute_side_slope(
        book: &std::collections::BTreeMap<u64, f64>,
        n_levels: usize,
        reverse: bool,
    ) -> f64 {
        // Collect volumes for top N levels
        // Bids are reverse sorted (highest price first), Asks are sorted (lowest price first)
        // But BTreeMap is always sorted by key (price).
        // So for Bids (high prices), we need iter().rev()
        // For Asks (low prices), we need iter()

        let volumes: Vec<f64> = if reverse {
            book.iter().rev().take(n_levels).map(|(_, v)| *v).collect()
        } else {
            book.iter().take(n_levels).map(|(_, v)| *v).collect()
        };

        let n = volumes.len();
        if n < 2 {
            return 0.0;
        }

        // Linear Regression: Level (x) vs Log(Volume) (y)
        // x = 1, 2, ..., n
        // y = log(v + 1)

        let mut sum_x = 0.0;
        let mut sum_y = 0.0;
        let mut sum_xy = 0.0;
        let mut sum_x2 = 0.0;

        for (i, v) in volumes.iter().enumerate() {
            let x = (i + 1) as f64;
            let y = (v + 1.0).ln();

            sum_x += x;
            sum_y += y;
            sum_xy += x * y;
            sum_x2 += x * x;
        }

        let n_f = n as f64;
        let var_x = sum_x2 - (sum_x * sum_x) / n_f;

        if var_x.abs() < 1e-9 {
            return 0.0;
        }

        let cov_xy = sum_xy - (sum_x * sum_y) / n_f;

        cov_xy / var_x
    }
}
