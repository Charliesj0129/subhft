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

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn test_hawkes_initial_intensity() {
        let h = HawkesTracker::new(0.1, 0.5, 1.0);
        assert_eq!(h.intensity, 0.1);
    }

    #[test]
    fn test_hawkes_jump_on_event() {
        let mut h = HawkesTracker::new(0.1, 0.5, 1.0);
        let intensity = h.update(1_000_000_000, true);
        // intensity = mu + (mu - mu)*decay + alpha = 0.1 + 0.5 = 0.6
        assert!((intensity - 0.6).abs() < 1e-10);
    }

    #[test]
    fn test_hawkes_decay() {
        let mut h = HawkesTracker::new(0.1, 0.5, 1.0);
        h.update(1_000_000_000, true); // intensity = 0.6
                                       // After 1 second with beta=1.0: decay = exp(-1) ≈ 0.368
                                       // intensity = 0.1 + (0.6 - 0.1) * 0.368 = 0.1 + 0.184 = 0.284
        let intensity = h.update(2_000_000_000, false);
        assert!((intensity - (0.1 + 0.5 * (-1.0_f64).exp())).abs() < 1e-6);
    }

    #[test]
    fn test_hawkes_no_event_returns_decayed() {
        let mut h = HawkesTracker::new(0.1, 0.5, 1.0);
        let intensity = h.update(1_000_000_000, false);
        // No event, first update with dt=1s from 0
        // decay = exp(-1) ≈ 0.368
        // intensity = 0.1 + (0.1 - 0.1) * decay = 0.1
        assert!((intensity - 0.1).abs() < 1e-10);
    }

    #[test]
    fn test_strategy_on_depth_empty() {
        let mut s = AlphaStrategy::new(4, 0.1, 0.5, 1.0);
        let signal = s.on_depth(vec![], vec![]);
        assert_eq!(signal, 0.0);
    }

    #[test]
    fn test_strategy_on_depth_basic() {
        let mut s = AlphaStrategy::new(1, 0.1, 0.5, 1.0);
        let bids = vec![(100.0, 200.0)];
        let asks = vec![(102.0, 100.0)];
        let signal = s.on_depth(bids, asks);
        // Level 0 (deep_level=1 → idx=0): imb = (200-100)/(200+100) = 0.333...
        // mom = 0 (no trade yet)
        // signal = imb * 1.0 + 0.0 * 0.5 = 0.333...
        assert!((signal - 1.0 / 3.0).abs() < 1e-10);
    }

    #[test]
    fn test_strategy_on_trade() {
        let mut s = AlphaStrategy::new(4, 0.1, 0.5, 1.0);
        let intensity = s.on_trade(1_000_000_000, 100.5, 10.0, true);
        assert!(intensity > 0.1); // Should have jumped
        assert_eq!(s.last_trade_price, 100.5);
    }

    #[test]
    fn test_strategy_get_signal_initial() {
        let s = AlphaStrategy::new(4, 0.1, 0.5, 1.0);
        let (intensity, mom) = s.get_signal();
        assert_eq!(intensity, 0.1);
        assert_eq!(mom, 0.0);
    }

    #[test]
    fn test_strategy_get_signal_after_trade() {
        let mut s = AlphaStrategy::new(4, 0.1, 0.5, 1.0);
        s.on_depth(vec![(100.0, 50.0)], vec![(102.0, 50.0)]);
        s.on_trade(1_000_000_000, 101.5, 10.0, true);
        let (intensity, mom) = s.get_signal();
        assert!(intensity > 0.1);
        assert!((mom - (101.5 - 101.0)).abs() < 1e-10);
    }

    #[test]
    fn test_strategy_deep_level_bounds() {
        let mut s = AlphaStrategy::new(5, 0.1, 0.5, 1.0);
        // Only 2 levels available but deep_level wants idx 4
        let bids = vec![(100.0, 200.0), (99.0, 150.0)];
        let asks = vec![(101.0, 100.0), (102.0, 80.0)];
        let signal = s.on_depth(bids, asks);
        // idx=4, len=2 → doesn't enter imbalance calc → imb=0
        assert_eq!(signal, 0.0);
    }

    #[test]
    fn test_strategy_level_zero() {
        let mut s = AlphaStrategy::new(0, 0.1, 0.5, 1.0);
        let bids = vec![(100.0, 200.0)];
        let asks = vec![(102.0, 100.0)];
        let signal = s.on_depth(bids, asks);
        // deep_level=0 → idx=0
        assert!((signal - 1.0 / 3.0).abs() < 1e-10);
    }

    #[test]
    fn test_strategy_momentum_with_trade() {
        let mut s = AlphaStrategy::new(1, 0.1, 0.5, 1.0);
        s.on_depth(vec![(100.0, 100.0)], vec![(102.0, 100.0)]);
        s.on_trade(1_000_000_000, 103.0, 10.0, true);
        let signal = s.on_depth(vec![(100.0, 100.0)], vec![(102.0, 100.0)]);
        // imb = 0, mom = 103.0 - 101.0 = 2.0
        // signal = 0 * 1.0 + 2.0 * 0.5 = 1.0
        assert!((signal - 1.0).abs() < 1e-10);
    }
}
