use pyo3::prelude::*;
use std::collections::HashMap;

/// Internal position state — all values in fixed-point (same scale as fill.price).
struct PositionState {
    net_qty: i64,
    avg_price_scaled: i64,
    realized_pnl_scaled: i64,
    fees_scaled: i64,
    last_update_ts: i64,
}

impl PositionState {
    fn new() -> Self {
        Self {
            net_qty: 0,
            avg_price_scaled: 0,
            realized_pnl_scaled: 0,
            fees_scaled: 0,
            last_update_ts: 0,
        }
    }
}

/// Pure-integer position tracker.
///
/// All arithmetic uses i64 fixed-point values at the same scale as the
/// incoming fill prices.  No float conversion is ever performed.
#[pyclass]
pub struct RustPositionTracker {
    positions: HashMap<String, PositionState>,
}

impl Default for RustPositionTracker {
    fn default() -> Self {
        Self::new()
    }
}

#[pymethods]
impl RustPositionTracker {
    #[new]
    pub fn new() -> Self {
        Self {
            positions: HashMap::new(),
        }
    }

    /// Process a fill and return the updated position state as a tuple.
    ///
    /// Arguments (all integers):
    ///   key          – "{account}:{strategy}:{symbol}"
    ///   side         – 0 = BUY, 1 = SELL  (matches Python Side IntEnum)
    ///   qty          – fill quantity (always positive)
    ///   price_scaled – fill price in fixed-point
    ///   fee          – fee in fixed-point
    ///   tax          – tax in fixed-point
    ///   match_ts     – exchange match timestamp (nanoseconds)
    ///   multiplier   – contract point-value multiplier (stocks=1, futures=point_value)
    ///
    /// Returns:
    ///   (net_qty, avg_price_scaled, realized_pnl_scaled, fees_scaled)
    #[allow(clippy::too_many_arguments)]
    #[pyo3(signature = (key, side, qty, price_scaled, fee, tax, match_ts, multiplier=1))]
    pub fn update(
        &mut self,
        key: String,
        side: i64,
        qty: i64,
        price_scaled: i64,
        fee: i64,
        tax: i64,
        match_ts: i64,
        multiplier: i64,
    ) -> (i64, i64, i64, i64) {
        let pos = self.positions.entry(key).or_insert_with(PositionState::new);

        let is_buy = side == 0; // Side.BUY == 0
        let signed_fill_qty: i64 = if is_buy { qty } else { -qty };

        // Accumulate fees
        pos.fees_scaled += fee + tax;

        // Determine if this fill closes existing exposure
        let current_sign = if pos.net_qty > 0 {
            1
        } else if pos.net_qty < 0 {
            -1
        } else {
            0
        };
        let fill_sign: i64 = if is_buy { 1 } else { -1 };

        let closing = current_sign != 0 && fill_sign != current_sign;

        if closing {
            let abs_net = pos.net_qty.abs();
            let abs_fill = qty; // qty is always positive
            let close_qty = abs_net.min(abs_fill);

            // PnL in fixed-point:
            //   Long closing (sell):  (fill_price - avg_price) * close_qty * multiplier
            //   Short closing (buy):  (avg_price - fill_price) * close_qty * multiplier
            // multiplier: stocks=1, futures=point_value (TMF=10, MXF=50, TXF=200)
            let pnl = if is_buy {
                // Covering a short
                (pos.avg_price_scaled - price_scaled) * close_qty * multiplier
            } else {
                // Selling a long
                (price_scaled - pos.avg_price_scaled) * close_qty * multiplier
            };
            pos.realized_pnl_scaled += pnl;

            pos.net_qty += signed_fill_qty;

            // If we flipped sides, the remainder starts at the new fill price
            if (current_sign > 0 && pos.net_qty < 0) || (current_sign < 0 && pos.net_qty > 0) {
                pos.avg_price_scaled = price_scaled;
            }
        } else {
            // Opening or increasing position
            if pos.net_qty == 0 {
                pos.avg_price_scaled = price_scaled;
                pos.net_qty += signed_fill_qty;
            } else {
                // Weighted average:
                //   new_avg = (old_net * old_avg + signed_qty * fill_price) / new_net
                let total_val = pos.net_qty * pos.avg_price_scaled + signed_fill_qty * price_scaled;
                pos.net_qty += signed_fill_qty;
                if pos.net_qty != 0 {
                    pos.avg_price_scaled = total_val / pos.net_qty;
                }
            }
        }

        pos.last_update_ts = match_ts;

        (
            pos.net_qty,
            pos.avg_price_scaled,
            pos.realized_pnl_scaled,
            pos.fees_scaled,
        )
    }

    /// Get current state for a position key.
    /// Returns (net_qty, avg_price_scaled, realized_pnl_scaled, fees_scaled)
    /// or (0, 0, 0, 0) if the key does not exist.
    pub fn get(&self, key: &str) -> (i64, i64, i64, i64) {
        match self.positions.get(key) {
            Some(pos) => (
                pos.net_qty,
                pos.avg_price_scaled,
                pos.realized_pnl_scaled,
                pos.fees_scaled,
            ),
            None => (0, 0, 0, 0),
        }
    }

    /// Reset a single position to zero.
    pub fn reset(&mut self, key: &str) {
        self.positions.remove(key);
    }

    /// Number of tracked positions.
    pub fn len(&self) -> usize {
        self.positions.len()
    }

    /// Group positions by strategy_id.
    ///
    /// Parses keys in "{account}:{strategy}:{symbol}" format and returns
    /// a HashMap<strategy_id, HashMap<symbol, net_qty>>.
    /// Keys that don't match the format are grouped under "*".
    pub fn get_positions_by_strategy(&self) -> HashMap<String, HashMap<String, i64>> {
        let mut result: HashMap<String, HashMap<String, i64>> = HashMap::new();
        for (key, pos) in &self.positions {
            let parts: Vec<&str> = key.splitn(3, ':').collect();
            if parts.len() >= 3 {
                let strategy_id = parts[1].to_string();
                let symbol = parts[2].to_string();
                result
                    .entry(strategy_id)
                    .or_default()
                    .insert(symbol, pos.net_qty);
            } else {
                result
                    .entry("*".to_string())
                    .or_default()
                    .insert(key.clone(), pos.net_qty);
            }
        }
        result
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    const BUY: i64 = 0;
    const SELL: i64 = 1;

    #[test]
    fn test_open_long_then_close() {
        let mut tracker = RustPositionTracker::new();
        let key = "acc:strat:SYM".to_string();

        // Buy 10 @ 1000 (scaled), multiplier=1 (stock)
        let (net, avg, pnl, fees) = tracker.update(key.clone(), BUY, 10, 1000, 5, 0, 100, 1);
        assert_eq!(net, 10);
        assert_eq!(avg, 1000);
        assert_eq!(pnl, 0);
        assert_eq!(fees, 5);

        // Sell 10 @ 1050 → PnL = (1050-1000)*10*1 = 500
        let (net, _avg, pnl, fees) = tracker.update(key.clone(), SELL, 10, 1050, 5, 0, 200, 1);
        assert_eq!(net, 0);
        assert_eq!(pnl, 500);
        assert_eq!(fees, 10);
    }

    #[test]
    fn test_open_short_then_close() {
        let mut tracker = RustPositionTracker::new();
        let key = "acc:strat:SYM".to_string();

        // Sell 5 @ 2000 (open short)
        let (net, avg, pnl, _) = tracker.update(key.clone(), SELL, 5, 2000, 0, 0, 100, 1);
        assert_eq!(net, -5);
        assert_eq!(avg, 2000);
        assert_eq!(pnl, 0);

        // Buy 5 @ 1900 (cover) → PnL = (2000-1900)*5*1 = 500
        let (net, _avg, pnl, _) = tracker.update(key.clone(), BUY, 5, 1900, 0, 0, 200, 1);
        assert_eq!(net, 0);
        assert_eq!(pnl, 500);
    }

    #[test]
    fn test_increase_long_weighted_avg() {
        let mut tracker = RustPositionTracker::new();
        let key = "acc:strat:SYM".to_string();

        // Buy 10 @ 1000
        tracker.update(key.clone(), BUY, 10, 1000, 0, 0, 100, 1);
        // Buy 10 @ 1200 → avg = (10*1000 + 10*1200) / 20 = 1100
        let (net, avg, pnl, _) = tracker.update(key.clone(), BUY, 10, 1200, 0, 0, 200, 1);
        assert_eq!(net, 20);
        assert_eq!(avg, 1100);
        assert_eq!(pnl, 0);
    }

    #[test]
    fn test_flip_position() {
        let mut tracker = RustPositionTracker::new();
        let key = "acc:strat:SYM".to_string();

        // Buy 10 @ 1000
        tracker.update(key.clone(), BUY, 10, 1000, 0, 0, 100, 1);
        // Sell 15 @ 1100 → close 10 (pnl=1000*1), open short 5 @ 1100
        let (net, avg, pnl, _) = tracker.update(key.clone(), SELL, 15, 1100, 0, 0, 200, 1);
        assert_eq!(net, -5);
        assert_eq!(avg, 1100);
        assert_eq!(pnl, 1000); // (1100-1000)*10*1
    }

    #[test]
    fn test_futures_multiplier_10() {
        let mut tracker = RustPositionTracker::new();
        let key = "acc:strat:TMFD6".to_string();

        // Buy 1 @ 333450000, multiplier=10 (微台指)
        tracker.update(key.clone(), BUY, 1, 333_450_000, 0, 0, 100, 10);
        // Sell 1 @ 333440000 → PnL = (333440000-333450000)*1*10 = -100000
        let (net, _avg, pnl, _) = tracker.update(key.clone(), SELL, 1, 333_440_000, 0, 0, 200, 10);
        assert_eq!(net, 0);
        assert_eq!(pnl, -100_000); // -10 NTD (descaled)
    }

    #[test]
    fn test_futures_multiplier_50() {
        let mut tracker = RustPositionTracker::new();
        let key = "acc:strat:MXFD6".to_string();

        // Buy 1 @ 333450000, multiplier=50 (小台指)
        tracker.update(key.clone(), BUY, 1, 333_450_000, 0, 0, 100, 50);
        // Sell 1 @ 333440000 → PnL = (333440000-333450000)*1*50 = -500000
        let (net, _avg, pnl, _) = tracker.update(key.clone(), SELL, 1, 333_440_000, 0, 0, 200, 50);
        assert_eq!(net, 0);
        assert_eq!(pnl, -500_000); // -50 NTD (descaled)
    }
}
