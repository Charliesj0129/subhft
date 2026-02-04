use crate::lob::LimitOrderBook;
use pyo3::prelude::*;

#[pyclass]
pub struct AlphaRegimePressure {
    // Volatility Monitor
    vol_alpha: f64,
    ewma_variance: f64,
    prev_mid: f64,
    vol_threshold: f64,

    // State
    initialized: bool,
}

#[pymethods]
impl AlphaRegimePressure {
    #[new]
    pub fn new(vol_window: usize, vol_threshold: f64) -> Self {
        // EWMA alpha for variance
        // Center of mass ~ window
        let vol_alpha = 2.0 / (vol_window as f64 + 1.0);

        AlphaRegimePressure {
            vol_alpha,
            ewma_variance: 0.0,
            prev_mid: f64::NAN,
            vol_threshold,
            initialized: false,
        }
    }

    pub fn calculate(&mut self, lob: &LimitOrderBook) -> f64 {
        // 1. Calculate Mid Price
        let best_bid_opt = lob.bids.iter().next_back();
        let best_ask_opt = lob.asks.iter().next();

        let (bid_p, bid_v) = match best_bid_opt {
            Some((&p, &v)) => (p as f64 / 10000.0, v),
            None => return 0.0,
        };

        let (ask_p, ask_v) = match best_ask_opt {
            Some((&p, &v)) => (p as f64 / 10000.0, v),
            None => return 0.0,
        };

        let mid = (bid_p + ask_p) / 2.0;

        // 2. Update Volatility (EWMA Variance of Returns)
        // Return = ln(pt / pt-1) approx (pt - pt-1)/pt-1
        // Used: (mid - prev) / prev

        let mut current_vol = 0.0;

        if self.initialized {
            if mid > 0.0 && self.prev_mid > 0.0 {
                let ret = (mid - self.prev_mid) / self.prev_mid;
                let ret_sq = ret * ret;

                // Update Variance
                self.ewma_variance =
                    self.vol_alpha * ret_sq + (1.0 - self.vol_alpha) * self.ewma_variance;
                current_vol = self.ewma_variance.sqrt();
            }
        } else {
            self.initialized = true;
        }
        self.prev_mid = mid;

        // 3. Check Regime
        if current_vol < self.vol_threshold {
            return 0.0; // Low Volatility -> Gate Closed
        }

        // 4. Calculate QueuePressure
        // Formula: BidVol - AskVol at L1
        // Note: Raw volume difference can be large.
        // Python code: bid_v - ask_v.
        // Should we normalize? The factor registry didn't.
        // But for trading, raw diff is fine if strategy scales it or uses sign.
        // Let's return raw diff.

        bid_v - ask_v
    }

    #[getter]
    pub fn get_current_vol(&self) -> f64 {
        self.ewma_variance.sqrt()
    }
}
