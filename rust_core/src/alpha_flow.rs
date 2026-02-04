use pyo3::prelude::*;
use std::collections::VecDeque;

/// Matched Filter Trade Flow Factor
///
/// Normalizes Net Trade Flow by Long-Term Volume (Capacity).
/// Signal = RollingSum(SignedFlow, fast) / RollingMean(Volume, slow)
#[pyclass]
pub struct MatchedFilterTradeFlow {
    fast_window: usize,
    slow_window: usize,

    // State
    trade_vol_history: VecDeque<f64>,
    trade_side_history: VecDeque<f64>,

    // Running sums for O(1) updates
    sum_signed_flow_fast: f64,
    sum_vol_slow: f64,
}

#[pymethods]
impl MatchedFilterTradeFlow {
    #[new]
    pub fn new(fast_window: usize, slow_window: usize) -> Self {
        MatchedFilterTradeFlow {
            fast_window,
            slow_window,
            trade_vol_history: VecDeque::with_capacity(slow_window),
            trade_side_history: VecDeque::with_capacity(slow_window),
            sum_signed_flow_fast: 0.0,
            sum_vol_slow: 0.0,
        }
    }

    pub fn update(&mut self, trade_vol: f64, trade_side: f64) -> f64 {
        let signed_flow = trade_vol * trade_side;

        // Add new
        self.trade_vol_history.push_back(trade_vol);
        self.trade_side_history.push_back(trade_side);

        self.sum_signed_flow_fast += signed_flow;
        self.sum_vol_slow += trade_vol;

        // Remove old (Fast)
        if self.trade_vol_history.len() > self.fast_window {
            let old_vol =
                self.trade_vol_history[self.trade_vol_history.len() - 1 - self.fast_window];
            let old_side =
                self.trade_side_history[self.trade_side_history.len() - 1 - self.fast_window];
            self.sum_signed_flow_fast -= old_vol * old_side;
        }

        // Remove old (Slow)
        if self.trade_vol_history.len() > self.slow_window {
            let old_vol = self.trade_vol_history.pop_front().unwrap_or(0.0);
            let _ = self.trade_side_history.pop_front().unwrap_or(0.0);
            self.sum_vol_slow -= old_vol;
        }

        // Compute Signal
        // Capacity = Avg Volume = Sum / N
        if self.trade_vol_history.len() < self.slow_window {
            return 0.0; // Warming up
        }

        let capacity = self.sum_vol_slow / (self.slow_window as f64);

        if capacity > 1e-8 {
            self.sum_signed_flow_fast / capacity
        } else {
            0.0
        }
    }
}
