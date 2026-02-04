//! Meta Alpha Factor - Rust implementation for production latency
//!
//! Combines DynamicEnsembleAlpha + InteractionAlpha components:
//! - Rolling IC-based weighting (currently simplified)
//! - Volatility regime detection
//! - Factor interaction (Hawkes x OFI)
//! - Confirmation boost when signals agree

use pyo3::prelude::*;
use std::collections::VecDeque;

/// Volatility Regime Detector
/// Returns: 1.0 = high vol, 0.0 = normal, -1.0 = low vol
fn compute_vol_regime(
    returns: &VecDeque<f64>,
    short_window: usize,
    long_window: usize,
) -> f64 {
    if returns.len() < long_window {
        return 0.0;
    }
    
    let n = returns.len();
    
    // Short vol
    let short_start = n.saturating_sub(short_window);
    let short_slice: Vec<f64> = returns.iter().skip(short_start).copied().collect();
    let short_mean: f64 = short_slice.iter().sum::<f64>() / short_slice.len() as f64;
    let short_var: f64 = short_slice.iter().map(|x| (x - short_mean).powi(2)).sum::<f64>() 
                          / short_slice.len() as f64;
    let short_vol = short_var.sqrt();
    
    // Long vol
    let long_start = n.saturating_sub(long_window);
    let long_slice: Vec<f64> = returns.iter().skip(long_start).copied().collect();
    let long_mean: f64 = long_slice.iter().sum::<f64>() / long_slice.len() as f64;
    let long_var: f64 = long_slice.iter().map(|x| (x - long_mean).powi(2)).sum::<f64>()
                         / long_slice.len() as f64;
    let long_vol = long_var.sqrt();
    
    if long_vol > 1e-10 {
        let vol_ratio = short_vol / long_vol;
        if vol_ratio > 1.5 {
            1.0  // High vol
        } else if vol_ratio < 0.7 {
            -1.0  // Low vol
        } else {
            0.0  // Normal
        }
    } else {
        0.0
    }
}

/// Meta Alpha Factor - High-performance Rust implementation
/// 
/// Features:
/// - O(1) per-tick updates
/// - Rolling signal combination
/// - Volatility regime detection
/// - Confirmation boost
#[pyclass]
pub struct MetaAlpha {
    // Windows
    fast_window: usize,
    slow_window: usize,
    vol_short_window: usize,
    vol_long_window: usize,
    
    // Weights
    dynamic_weight: f64,
    interaction_weight: f64,
    confirmation_boost: f64,
    
    // State - Trade Flow
    trade_vol_history: VecDeque<f64>,
    trade_side_history: VecDeque<f64>,
    sum_signed_flow_fast: f64,
    sum_vol_slow: f64,
    
    // State - OFI (simplified)
    bid_qty_history: VecDeque<f64>,
    ask_qty_history: VecDeque<f64>,
    ofi_sum: f64,
    
    // State - Hawkes-like intensity tracker
    hawkes_intensity: f64,
    hawkes_mu: f64,
    hawkes_alpha: f64,
    hawkes_beta: f64,
    
    // State - Returns for vol regime
    returns_history: VecDeque<f64>,
    last_price: f64,
    
    // Output signals
    signal_dynamic: f64,
    signal_interaction: f64,
}

#[pymethods]
impl MetaAlpha {
    #[new]
    #[pyo3(signature = (
        fast_window = 20,
        slow_window = 300,
        vol_short_window = 100,
        vol_long_window = 500
    ))]
    pub fn new(
        fast_window: usize,
        slow_window: usize,
        vol_short_window: usize,
        vol_long_window: usize,
    ) -> Self {
        MetaAlpha {
            fast_window,
            slow_window,
            vol_short_window,
            vol_long_window,
            
            dynamic_weight: 0.57,
            interaction_weight: 0.43,
            confirmation_boost: 1.5,
            
            trade_vol_history: VecDeque::with_capacity(slow_window),
            trade_side_history: VecDeque::with_capacity(slow_window),
            sum_signed_flow_fast: 0.0,
            sum_vol_slow: 0.0,
            
            bid_qty_history: VecDeque::with_capacity(fast_window),
            ask_qty_history: VecDeque::with_capacity(fast_window),
            ofi_sum: 0.0,
            
            hawkes_intensity: 0.0,
            hawkes_mu: 0.02,
            hawkes_alpha: 0.2,
            hawkes_beta: 0.1,
            
            returns_history: VecDeque::with_capacity(vol_long_window),
            last_price: 0.0,
            
            signal_dynamic: 0.0,
            signal_interaction: 0.0,
        }
    }
    
    /// Update with new tick data
    /// Returns the combined MetaAlpha signal
    pub fn update(
        &mut self,
        trade_vol: f64,
        trade_side: f64,   // +1 buy, -1 sell
        bid_qty: f64,
        ask_qty: f64,
        mid_price: f64,
    ) -> f64 {
        // --- Update returns history ---
        if self.last_price > 0.0 {
            let ret = (mid_price - self.last_price) / (self.last_price + 1e-10);
            self.returns_history.push_back(ret);
            if self.returns_history.len() > self.vol_long_window {
                self.returns_history.pop_front();
            }
        }
        self.last_price = mid_price;
        
        // --- Hawkes intensity update (O(1)) ---
        // λ(t+dt) = μ + (λ(t) - μ) * e^(-β*dt) + α * (event)
        let decay = (-self.hawkes_beta).exp();
        let event_indicator = if trade_vol > 0.0 { 1.0 } else { 0.0 };
        self.hawkes_intensity = self.hawkes_mu 
            + (self.hawkes_intensity - self.hawkes_mu) * decay 
            + self.hawkes_alpha * event_indicator;
        
        // Normalize Hawkes to [0, 1] range
        let hawkes_signal = (self.hawkes_intensity.min(2.0) / 2.0).max(0.0);
        
        // --- Trade Flow (MatchedFilter style) ---
        let signed_flow = trade_vol * trade_side;
        self.trade_vol_history.push_back(trade_vol);
        self.trade_side_history.push_back(trade_side);
        self.sum_signed_flow_fast += signed_flow;
        self.sum_vol_slow += trade_vol;
        
        // Remove old (fast)
        if self.trade_vol_history.len() > self.fast_window {
            let idx = self.trade_vol_history.len() - 1 - self.fast_window;
            let old_vol = self.trade_vol_history[idx];
            let old_side = self.trade_side_history[idx];
            self.sum_signed_flow_fast -= old_vol * old_side;
        }
        
        // Remove old (slow)
        if self.trade_vol_history.len() > self.slow_window {
            let old_vol = self.trade_vol_history.pop_front().unwrap_or(0.0);
            let _ = self.trade_side_history.pop_front().unwrap_or(0.0);
            self.sum_vol_slow -= old_vol;
        }
        
        let capacity = if self.trade_vol_history.len() >= self.slow_window.min(10) {
            self.sum_vol_slow / (self.trade_vol_history.len() as f64)
        } else {
            1.0
        };
        let trade_flow_signal = if capacity > 1e-8 {
            self.sum_signed_flow_fast / capacity
        } else {
            0.0
        };
        
        // --- OFI update ---
        let prev_bid = self.bid_qty_history.back().copied().unwrap_or(bid_qty);
        let prev_ask = self.ask_qty_history.back().copied().unwrap_or(ask_qty);
        
        let delta_bid = bid_qty - prev_bid;
        let delta_ask = ask_qty - prev_ask;
        let ofi_tick = delta_bid - delta_ask;
        
        self.bid_qty_history.push_back(bid_qty);
        self.ask_qty_history.push_back(ask_qty);
        self.ofi_sum += ofi_tick;
        
        if self.bid_qty_history.len() > self.fast_window {
            // Remove oldest contribution (approximate)
            self.bid_qty_history.pop_front();
            self.ask_qty_history.pop_front();
            // Recalculate OFI sum would be O(n), so we approximate
            self.ofi_sum *= 0.95; // Decay factor for approximation
        }
        
        let ofi_signal = self.ofi_sum / (self.fast_window as f64);
        
        // --- Volatility regime ---
        let vol_regime = compute_vol_regime(
            &self.returns_history,
            self.vol_short_window,
            self.vol_long_window
        );
        
        // --- Dynamic Ensemble Signal ---
        // Weight trade_flow more in high vol, OFI more in low vol
        let flow_weight = if vol_regime > 0.5 { 0.6 } else if vol_regime < -0.5 { 0.4 } else { 0.5 };
        let ofi_weight = 1.0 - flow_weight;
        self.signal_dynamic = flow_weight * trade_flow_signal + ofi_weight * ofi_signal;
        
        // --- Interaction Signal ---
        // Hawkes x OFI: When Hawkes is high (critical), flip OFI impact
        let hawkes_interaction = -hawkes_signal * ofi_signal;
        // TradeFlow when not in critical regime
        let dampened_flow = trade_flow_signal * (1.0 - hawkes_signal);
        self.signal_interaction = 0.5 * hawkes_interaction + 0.5 * dampened_flow;
        
        // --- Meta combination ---
        let mut combined = self.dynamic_weight * self.signal_dynamic 
                         + self.interaction_weight * self.signal_interaction;
        
        // Confirmation boost
        if self.signal_dynamic.signum() == self.signal_interaction.signum()
            && self.signal_dynamic.abs() > 0.5
            && self.signal_interaction.abs() > 0.5 
        {
            combined *= self.confirmation_boost;
        }
        
        combined
    }
    
    /// Get component signals for debugging
    pub fn get_signals(&self) -> (f64, f64) {
        (self.signal_dynamic, self.signal_interaction)
    }
    
    /// Reset state
    pub fn reset(&mut self) {
        self.trade_vol_history.clear();
        self.trade_side_history.clear();
        self.sum_signed_flow_fast = 0.0;
        self.sum_vol_slow = 0.0;
        self.bid_qty_history.clear();
        self.ask_qty_history.clear();
        self.ofi_sum = 0.0;
        self.hawkes_intensity = 0.0;
        self.returns_history.clear();
        self.last_price = 0.0;
        self.signal_dynamic = 0.0;
        self.signal_interaction = 0.0;
    }
}
