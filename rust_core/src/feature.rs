use pyo3::prelude::*;

#[pyclass]
pub struct LobFeatureKernelV1 {
    prev_best_bid: i64,
    prev_best_ask: i64,
    prev_l1_bid_qty: i64,
    prev_l1_ask_qty: i64,
    ofi_l1_cum: i64,
    ofi_l1_ema8: f64,
    spread_ema8: f64,
    imbalance_ema8_ppm: f64,
    initialized: bool,
}

unsafe impl Send for LobFeatureKernelV1 {}

#[pymethods]
impl LobFeatureKernelV1 {
    #[new]
    pub fn new() -> Self {
        Self {
            prev_best_bid: 0,
            prev_best_ask: 0,
            prev_l1_bid_qty: 0,
            prev_l1_ask_qty: 0,
            ofi_l1_cum: 0,
            ofi_l1_ema8: 0.0,
            spread_ema8: 0.0,
            imbalance_ema8_ppm: 0.0,
            initialized: false,
        }
    }

    pub fn reset(&mut self) {
        self.prev_best_bid = 0;
        self.prev_best_ask = 0;
        self.prev_l1_bid_qty = 0;
        self.prev_l1_ask_qty = 0;
        self.ofi_l1_cum = 0;
        self.ofi_l1_ema8 = 0.0;
        self.spread_ema8 = 0.0;
        self.imbalance_ema8_ppm = 0.0;
        self.initialized = false;
    }

    #[allow(clippy::too_many_arguments)]
    pub fn update(
        &mut self,
        best_bid: i64,
        best_ask: i64,
        mid_price_x2: i64,
        spread_scaled: i64,
        bid_depth: i64,
        ask_depth: i64,
        l1_bid_qty: i64,
        l1_ask_qty: i64,
    ) -> Vec<i64> {
        let bid_depth = bid_depth.max(0);
        let ask_depth = ask_depth.max(0);
        let l1_bid_qty = l1_bid_qty.max(0);
        let l1_ask_qty = l1_ask_qty.max(0);

        let depth_total = bid_depth + ask_depth;
        let imbalance_ppm = if depth_total > 0 {
            py_round_i64(((bid_depth - ask_depth) as f64 * 1_000_000.0) / depth_total as f64)
        } else {
            0
        };

        let l1_total = l1_bid_qty + l1_ask_qty;
        let (l1_imbalance_ppm, microprice_x2) = if l1_total > 0 {
            let l1_imb =
                py_round_i64(((l1_bid_qty - l1_ask_qty) as f64 * 1_000_000.0) / l1_total as f64);
            let mp = py_round_i64(
                (2.0 * ((best_ask * l1_bid_qty + best_bid * l1_ask_qty) as f64)) / l1_total as f64,
            );
            (l1_imb, mp)
        } else {
            (0, mid_price_x2)
        };

        let (ofi_l1_raw, ofi_l1_cum, ofi_l1_ema8, spread_ema8_scaled, depth_imbalance_ema8_ppm) =
            if !self.initialized {
                self.spread_ema8 = spread_scaled as f64;
                self.imbalance_ema8_ppm = l1_imbalance_ppm as f64;
                self.initialized = true;
                (
                    0_i64,
                    0_i64,
                    0_i64,
                    py_round_i64(self.spread_ema8),
                    py_round_i64(self.imbalance_ema8_ppm),
                )
            } else {
                let b_flow = if best_bid > self.prev_best_bid {
                    l1_bid_qty
                } else if best_bid == self.prev_best_bid {
                    l1_bid_qty - self.prev_l1_bid_qty
                } else {
                    -self.prev_l1_bid_qty
                };

                let a_flow = if best_ask > self.prev_best_ask {
                    -self.prev_l1_ask_qty
                } else if best_ask == self.prev_best_ask {
                    l1_ask_qty - self.prev_l1_ask_qty
                } else {
                    l1_ask_qty
                };

                let ofi_raw = b_flow - a_flow;
                self.ofi_l1_cum += ofi_raw;
                let alpha = 2.0 / 9.0;
                self.ofi_l1_ema8 = (1.0 - alpha) * self.ofi_l1_ema8 + alpha * ofi_raw as f64;
                self.spread_ema8 = (1.0 - alpha) * self.spread_ema8 + alpha * spread_scaled as f64;
                self.imbalance_ema8_ppm =
                    (1.0 - alpha) * self.imbalance_ema8_ppm + alpha * l1_imbalance_ppm as f64;

                (
                    ofi_raw,
                    self.ofi_l1_cum,
                    py_round_i64(self.ofi_l1_ema8),
                    py_round_i64(self.spread_ema8),
                    py_round_i64(self.imbalance_ema8_ppm),
                )
            };

        self.prev_best_bid = best_bid;
        self.prev_best_ask = best_ask;
        self.prev_l1_bid_qty = l1_bid_qty;
        self.prev_l1_ask_qty = l1_ask_qty;

        vec![
            best_bid,
            best_ask,
            mid_price_x2,
            spread_scaled,
            bid_depth,
            ask_depth,
            imbalance_ppm,
            microprice_x2,
            l1_bid_qty,
            l1_ask_qty,
            l1_imbalance_ppm,
            ofi_l1_raw,
            ofi_l1_cum,
            ofi_l1_ema8,
            spread_ema8_scaled,
            depth_imbalance_ema8_ppm,
        ]
    }
}

#[inline]
fn py_round_i64(x: f64) -> i64 {
    // Python round() uses bankers rounding (ties to even).
    // Rust stable provides round_ties_even on f64.
    x.round_ties_even() as i64
}
