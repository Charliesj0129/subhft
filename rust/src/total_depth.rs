
// src/strategies/total_depth.rs

/// TotalDepth Strategy
/// Logic: (BidQty - AskQty) / (BidQty + AskQty) -> Signal
/// Skew: Signal * Multiplier -> Order Placement

pub struct TotalDepthParams {
    pub skew_multiplier: f64,
    pub max_position: f64,
    pub half_spread_ticks: f64,
}

pub struct TotalDepthStrategy {
    asset_no: usize,
    tick_size: f64,
    lot_size: f64,
    params: TotalDepthParams,
    current_pos: f64,
}

impl TotalDepthStrategy {
    pub fn new(asset_no: usize, tick_size: f64, lot_size: f64, params: TotalDepthParams) -> Self {
        Self {
            asset_no,
            tick_size,
            lot_size,
            params,
            current_pos: 0.0,
        }
    }

    pub fn on_depth(&mut self, bid_qty: f64, ask_qty: f64, mid_price: f64) -> Option<(f64, f64)> {
        // 1. Calculate Signal
        let total_qty = bid_qty + ask_qty;
        if total_qty == 0.0 {
            return None;
        }
        
        let signal = (bid_qty - ask_qty) / total_qty;
        
        // 2. Calculate Skew
        let skew = signal * self.params.skew_multiplier; // e.g. 10 ticks
        
        // 3. Calculate Prices
        let half_spread = self.params.half_spread_ticks * self.tick_size;
        
        let raw_bid = mid_price - half_spread + (skew * self.tick_size);
        let raw_ask = mid_price + half_spread + (skew * self.tick_size);
        
        // Round to tick
        let bid_price = (raw_bid / self.tick_size).round() * self.tick_size;
        let ask_price = (raw_ask / self.tick_size).round() * self.tick_size;
        
        Some((bid_price, ask_price))
    }
    
    pub fn update_position(&mut self, change: f64) {
        self.current_pos += change;
    }
}
