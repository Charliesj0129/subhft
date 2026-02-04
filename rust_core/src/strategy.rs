use pyo3::prelude::*;

/// Hawkes Process Tracker
/// Models trade clustering intensity
struct HawkesTracker {
    mu: f64,
    alpha: f64,
    beta: f64,
    last_ts: i64,
    intensity: f64,
}

impl HawkesTracker {
    fn new(mu: f64, alpha: f64, beta: f64) -> Self {
        Self {
            mu,
            alpha,
            beta,
            last_ts: 0,
            intensity: mu,
        }
    }

    fn update(&mut self, current_ts: i64, is_event: bool) -> f64 {
        // Time decay
        let dt = (current_ts - self.last_ts) as f64 / 1e9; // ns to seconds

        // Safety check for non-monotonic time or very large gaps
        if dt > 0.0 {
            let decay = (-self.beta * dt).exp();
            self.intensity = self.mu + (self.intensity - self.mu) * decay;
        }

        // Jump
        if is_event {
            self.intensity += self.alpha;
        }

        self.last_ts = current_ts;
        self.intensity
    }
}

#[pyclass]
pub struct AlphaStrategy {
    // Parameters
    deep_level: usize, // e.g., 4 (0-indexed logic depends on data, usually 1-5 means idx 0-4)

    // State
    hawkes: HawkesTracker,
    last_trade_price: f64,
    mid_price: f64,

    // Weights
    w_imb: f64,
    w_skew: f64,
    #[allow(dead_code)]
    w_hawkes: f64, // Used for gating or scaling
}

#[pymethods]
impl AlphaStrategy {
    #[new]
    pub fn new(level: usize, mu: f64, alpha: f64, beta: f64) -> Self {
        Self {
            deep_level: level,
            hawkes: HawkesTracker::new(mu, alpha, beta),
            last_trade_price: 0.0,
            mid_price: 0.0,
            w_imb: 1.0,
            w_skew: 0.5,
            w_hawkes: 0.0,
        }
    }

    /// Process LOB Update
    /// bids/asks: Dict or Map of Price -> Qty
    /// We need Sorted access for "Level 4".
    /// If input is raw vectors (sorted), it's faster.
    /// Assuming input is Lists of (Price, Qty) sorted best to worst.
    pub fn on_depth(&mut self, bids: Vec<(f64, f64)>, asks: Vec<(f64, f64)>) -> f64 {
        // Update Mid
        if bids.is_empty() || asks.is_empty() {
            return 0.0;
        }
        self.mid_price = (bids[0].0 + asks[0].0) * 0.5;

        // Deep Imbalance (L4)
        // If we want L4 (idx 3), we check length.
        let idx = if self.deep_level > 0 {
            self.deep_level - 1
        } else {
            0
        };

        let mut imb = 0.0;

        // Safely access deep level
        if idx < bids.len() && idx < asks.len() {
            let b_qty = bids[idx].1;
            let a_qty = asks[idx].1;
            let total = b_qty + a_qty;
            if total > 0.0 {
                imb = (b_qty - a_qty) / total;
            }
        }

        // Strategy Logic:
        // Signal = Imb * w_imb + Momentum * w_skew

        // Momentum (Trade Skew)
        let mom = if self.last_trade_price > 0.0 {
            self.last_trade_price - self.mid_price
        } else {
            0.0
        };

        imb * self.w_imb + mom * self.w_skew
    }

    /// Process Trade
    pub fn on_trade(&mut self, ts: i64, price: f64, _qty: f64, _is_buyer_maker: bool) -> f64 {
        self.last_trade_price = price;

        // Update Hawkes (Intensity of trading)
        // Return Intensity as feature
        self.hawkes.update(ts, true)
    }

    /// Get current signal state
    pub fn get_signal(&self) -> (f64, f64) {
        // Return tuple (Intensity,  LastTrade - Mid)
        let mom = if self.last_trade_price > 0.0 {
            self.last_trade_price - self.mid_price
        } else {
            0.0
        };
        (self.hawkes.intensity, mom)
    }
}
