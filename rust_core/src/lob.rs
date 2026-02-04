use pyo3::prelude::*;
use serde::{Deserialize, Serialize};
use std::collections::BTreeMap;

#[derive(Debug, Clone, Serialize, Deserialize)]
#[pyclass]
pub struct PriceLevel {
    #[pyo3(get, set)]
    pub price: f64,
    #[pyo3(get, set)]
    pub quantity: f64,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
#[pyclass]
pub struct LimitOrderBook {
    #[pyo3(get)]
    pub symbol: String,
    pub bids: BTreeMap<u64, f64>, // Price(scaled) -> Qty
    pub asks: BTreeMap<u64, f64>,
}

#[pymethods]
impl LimitOrderBook {
    #[new]
    pub fn new(symbol: String) -> Self {
        Self {
            symbol,
            bids: BTreeMap::new(),
            asks: BTreeMap::new(),
        }
    }

    pub fn update(&mut self, is_bid: bool, price: f64, quantity: f64) {
        let scaled_price = (price * 10000.0) as u64; // Simple scaling for key
        let book = if is_bid {
            &mut self.bids
        } else {
            &mut self.asks
        };

        if quantity <= 0.0 {
            book.remove(&scaled_price);
        } else {
            book.insert(scaled_price, quantity);
        }
    }

    pub fn top_bids(&self, depth: usize) -> Vec<PriceLevel> {
        self.bids
            .iter()
            .rev()
            .take(depth)
            .map(|(p, q)| PriceLevel {
                price: *p as f64 / 10000.0,
                quantity: *q,
            })
            .collect()
    }

    pub fn top_asks(&self, depth: usize) -> Vec<PriceLevel> {
        self.asks
            .iter()
            .take(depth)
            .map(|(p, q)| PriceLevel {
                price: *p as f64 / 10000.0,
                quantity: *q,
            })
            .collect()
    }
}
