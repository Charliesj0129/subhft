
use ndarray::Array2;
use ort::session::Session;
use ort::value::{Value, Tensor};
use std::sync::Once;
use anyhow::Result;
use pyo3::prelude::*;

static INIT: Once = Once::new();

#[derive(Clone)]
#[pyclass]
pub struct RLParams {
    #[pyo3(get, set)]
    pub model_path: String,
    #[pyo3(get, set)]
    pub max_position: f64,
    #[pyo3(get, set)]
    pub tick_size: f64,
}

#[pymethods]
impl RLParams {
    #[new]
    pub fn new(model_path: String, max_position: f64, tick_size: f64) -> Self {
        Self { model_path, max_position, tick_size }
    }
}

#[pyclass]
pub struct RLStrategy {
    session: Session,
    params: RLParams,
    #[pyo3(get)]
    inventory: f64,
    prev_mid: f64,
    
    // Cycle 9: Alpha Features
    #[pyo3(get)]
    microprice: f64,
    #[pyo3(get)]
    ofi_i: f64,
    
    // OFI Internal State
    prev_best_bid: f64,
    prev_best_ask: f64,
    prev_bid_qty: f64,
    prev_ask_qty: f64,
}

#[pymethods]
impl RLStrategy {
    #[new]
    pub fn new(params: RLParams) -> PyResult<Self> {
        // Initialize ONNX Runtime once
        INIT.call_once(|| {
            let _ = ort::init()
                .with_name("hft_rust")
                .commit();
        });

        let session = Session::builder()
            .map_err(|e| PyErr::new::<pyo3::exceptions::PyRuntimeError, _>(e.to_string()))?
            .with_intra_threads(1)
            .map_err(|e| PyErr::new::<pyo3::exceptions::PyRuntimeError, _>(e.to_string()))?
            .commit_from_file(&params.model_path)
            .map_err(|e| PyErr::new::<pyo3::exceptions::PyRuntimeError, _>(e.to_string()))?;
            
        Ok(Self {
            session,
            params,
            inventory: 0.0,
            prev_mid: 0.0,
            microprice: 0.0,
            ofi_i: 0.0,
            prev_best_bid: 0.0,
            prev_best_ask: 0.0,
            prev_bid_qty: 0.0,
            prev_ask_qty: 0.0,
        })
    }
    
    // Accept lists of (price, qty) directly
    pub fn on_depth_py(&mut self, bids: Vec<(f64, f64)>, asks: Vec<(f64, f64)>, mid_price: f64) -> PyResult<usize> {
        self.on_depth(&bids, &asks, mid_price)
            .map_err(|e| PyErr::new::<pyo3::exceptions::PyRuntimeError, _>(e.to_string()))
    }
    
    pub fn update_position(&mut self, change: f64) {
        self.inventory += change;
    }
}

impl RLStrategy {
    // Internal Rust Method (kept for reference or internal use)
    fn compute_features(&self, bids: &[(f64, f64)], asks: &[(f64, f64)], mid_price: f64) -> Array2<f32> {
        let mut feats = Array2::<f32>::zeros((1, 11)); 
        
        let get_qty = |book: &[(f64, f64)], level: usize| -> f32 {
            if level < book.len() {
                book[level].1 as f32
            } else {
                0.0
            }
        };

        // Imbalances
        // L1 (Index 4)
        let bid1 = get_qty(bids, 0);
        let ask1 = get_qty(asks, 0);
        let imb1 = if bid1 + ask1 > 0.0 { (bid1 - ask1) / (bid1 + ask1) } else { 0.0 };
        feats[[0, 4]] = imb1;

        // L3 (Index 0)
        let bid3 = get_qty(bids, 2);
        let ask3 = get_qty(asks, 2);
        let imb3 = if bid3 + ask3 > 0.0 { (bid3 - ask3) / (bid3 + ask3) } else { 0.0 };
        feats[[0, 0]] = imb3;

        // L4 (Index 1)
        let bid4 = get_qty(bids, 3);
        let ask4 = get_qty(asks, 3);
        let imb4 = if bid4 + ask4 > 0.0 { (bid4 - ask4) / (bid4 + ask4) } else { 0.0 };
        feats[[0, 1]] = imb4;
        
        // L5 (Index 2)
        let bid5 = get_qty(bids, 4);
        let ask5 = get_qty(asks, 4);
        let imb5 = if bid5 + ask5 > 0.0 { (bid5 - ask5) / (bid5 + ask5) } else { 0.0 };
        feats[[0, 2]] = imb5;
        
        // Momentum (Index 3) - Set to 0.0 to match training data distribution
        feats[[0, 3]] = 0.0;
        
        // Zeros (Indices 5, 6, 7) - Already 0.0
        
        // New Cycle 10 Features
        // MicroPrice Dev (Index 8)
        // Alpha ~ MicroPrice - Mid.
        // Training script used `alpha_micro_dev` which is `spread * imb_adj`.
        // Rust `self.microprice` is absolute price. 
        // We need deviation.
        let micro_dev = (self.microprice - mid_price) as f32;
        feats[[0, 8]] = micro_dev;

        // OFI-I (Index 9)
        feats[[0, 9]] = self.ofi_i as f32;
        
        // Inventory (Index 10)
        feats[[0, 10]] = self.inventory as f32;
        
        feats
    }
    
    pub fn on_depth(&mut self, bids: &[(f64, f64)], asks: &[(f64, f64)], mid_price: f64) -> Result<usize> {
        // --- Cycle 9: Alpha Calculations ---
        
        let best_bid = if !bids.is_empty() { bids[0].0 } else { 0.0 };
        let best_ask = if !asks.is_empty() { asks[0].0 } else { 0.0 };
        let bid_qty = if !bids.is_empty() { bids[0].1 } else { 0.0 };
        let ask_qty = if !asks.is_empty() { asks[0].1 } else { 0.0 };
        
        // 1. Calculate MicroPrice
        // M = Mid + Spread * (2I - 1)^3 * 0.5
        let spread = best_ask - best_bid;
        let imm_imb = if bid_qty + ask_qty > 0.0 { bid_qty / (bid_qty + ask_qty) } else { 0.5 };
        // (2I - 1) maps [0,1] -> [-1,1]
        let imb_adj = (2.0 * imm_imb - 1.0).powi(3);
        self.microprice = mid_price + spread * imb_adj * 0.5;
        
        // 2. Calculate OFI-I
        // e_n (Bid)
        let d_bid = if best_bid > self.prev_best_bid {
            bid_qty
        } else if best_bid < self.prev_best_bid {
            -self.prev_bid_qty // Previous level cleared/cancelled
        } else {
            bid_qty - self.prev_bid_qty
        };
        
        // e_n (Ask)
        let d_ask = if best_ask < self.prev_best_ask {
            ask_qty // Improved offer (more sell pressure) -> wait, OFI sign?
            // Ask adding liquidity is usually negative price pressure?
            // Cont: OFI = e_n(Bid) - e_n(Ask). 
            // If Ask price drops (improves), it's aggressive selling (taking bid) or improving to new level.
            // If Ask Px < Prev Ask Px: New level established.
            // Actually, Cont IOFI def:
            // e_n(L_b) = q_b(T_n) if p_b > p_b_prev
            //          = q_b(T_n) - q_b_prev if p_b == p_b_prev
            //          = -q_b_prev if p_b < p_b_prev
            // e_n(L_a) is symmetric.
            // OFI = e_n(Bid) - e_n(Ask).
        } else if best_ask > self.prev_best_ask {
            -self.prev_ask_qty
        } else {
            ask_qty - self.prev_ask_qty
        };
        
        let raw_ofi = d_bid - d_ask;
        
        // Decay (Alpha ~ 0.9 per tick approx, or 0.1 decay)
        // OFI(t) = Raw + 0.9 * OFI(t-1)
        self.ofi_i = raw_ofi + 0.9 * self.ofi_i;
        
        // Update State
        self.prev_best_bid = best_bid;
        self.prev_best_ask = best_ask;
        self.prev_bid_qty = bid_qty;
        self.prev_ask_qty = ask_qty;
        
        // --- End Cycle 9 ---
        
        let input_tensor = self.compute_features(bids, asks, mid_price);
        
        // Run Inference
        let (shape, data) = (vec![1, 11], input_tensor.into_raw_vec());
        let input_val = Value::from_array((shape, data))?;
        let outputs = self.session.run(ort::inputs![input_val])?;
        
        // Extract Logits
        let (_shape, logits) = outputs[0].try_extract_tensor::<f32>()?;
        
        // Argmax
        let mut best_action = 0;
        let mut max_val = f32::NEG_INFINITY;
        
        for (i, &val) in logits.iter().enumerate() {
            if val > max_val {
                max_val = val;
                best_action = i;
            }
        }
        
        self.prev_mid = mid_price;
        Ok(best_action)
        
    }
}
