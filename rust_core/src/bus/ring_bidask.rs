use pyo3::prelude::*;
use pyo3::types::PyTuple;

#[derive(Clone)]
pub(crate) enum BidAskLevels {
    Py {
        bids: PyObject,
        asks: PyObject,
    },
    Packed {
        bid_flat: Vec<i64>,
        bid_rows: usize,
        ask_flat: Vec<i64>,
        ask_rows: usize,
    },
}

#[derive(Clone)]
pub(crate) struct BidAskFrame {
    pub symbol: String,
    pub levels: BidAskLevels,
    pub exch_ts: i64,
    pub is_snapshot: bool,
    pub has_stats: bool,
    pub best_bid: i64,
    pub best_ask: i64,
    pub bid_depth: i64,
    pub ask_depth: i64,
    pub mid_price: f64,
    pub spread: f64,
    pub imbalance: f64,
}

#[pyclass]
pub struct FastBidAskRingBuffer {
    size: usize,
    buffer: Vec<Option<BidAskFrame>>,
}

#[pymethods]
impl FastBidAskRingBuffer {
    #[new]
    pub fn new(size: usize) -> Self {
        let size = size.max(1);
        let buffer = vec![None; size];
        Self { size, buffer }
    }

    #[allow(clippy::too_many_arguments)]
    pub fn set_bidask(
        &mut self,
        idx: usize,
        symbol: String,
        bids: PyObject,
        asks: PyObject,
        exch_ts: i64,
        is_snapshot: bool,
        has_stats: bool,
        best_bid: i64,
        best_ask: i64,
        bid_depth: i64,
        ask_depth: i64,
        mid_price: f64,
        spread: f64,
        imbalance: f64,
    ) {
        let slot = idx % self.size;
        self.buffer[slot] = Some(BidAskFrame {
            symbol,
            levels: BidAskLevels::Py { bids, asks },
            exch_ts,
            is_snapshot,
            has_stats,
            best_bid,
            best_ask,
            bid_depth,
            ask_depth,
            mid_price,
            spread,
            imbalance,
        });
    }

    #[allow(clippy::too_many_arguments)]
    pub fn set_bidask_packed(
        &mut self,
        idx: usize,
        symbol: String,
        bid_flat: Vec<i64>,
        bid_rows: usize,
        ask_flat: Vec<i64>,
        ask_rows: usize,
        exch_ts: i64,
        is_snapshot: bool,
        has_stats: bool,
        best_bid: i64,
        best_ask: i64,
        bid_depth: i64,
        ask_depth: i64,
        mid_price: f64,
        spread: f64,
        imbalance: f64,
    ) {
        let slot = idx % self.size;
        self.buffer[slot] = Some(BidAskFrame {
            symbol,
            levels: BidAskLevels::Packed {
                bid_flat,
                bid_rows,
                ask_flat,
                ask_rows,
            },
            exch_ts,
            is_snapshot,
            has_stats,
            best_bid,
            best_ask,
            bid_depth,
            ask_depth,
            mid_price,
            spread,
            imbalance,
        });
    }

    pub fn get<'py>(&self, py: Python<'py>, idx: usize) -> Option<PyObject> {
        let slot = idx % self.size;
        self.buffer[slot].as_ref().map(|f| {
            let (bids, asks) = match &f.levels {
                BidAskLevels::Py { bids, asks } => (bids.clone_ref(py), asks.clone_ref(py)),
                BidAskLevels::Packed {
                    bid_flat,
                    bid_rows,
                    ask_flat,
                    ask_rows,
                } => {
                    let mut bids_rows: Vec<Vec<i64>> = Vec::with_capacity(*bid_rows);
                    for i in 0..*bid_rows {
                        let base = i * 2;
                        if base + 1 >= bid_flat.len() {
                            break;
                        }
                        bids_rows.push(vec![bid_flat[base], bid_flat[base + 1]]);
                    }
                    let mut asks_rows: Vec<Vec<i64>> = Vec::with_capacity(*ask_rows);
                    for i in 0..*ask_rows {
                        let base = i * 2;
                        if base + 1 >= ask_flat.len() {
                            break;
                        }
                        asks_rows.push(vec![ask_flat[base], ask_flat[base + 1]]);
                    }
                    (bids_rows.into_py(py), asks_rows.into_py(py))
                }
            };
            if f.has_stats {
                PyTuple::new_bound(
                    py,
                    [
                        "bidask".into_py(py),
                        f.symbol.clone().into_py(py),
                        bids,
                        asks,
                        f.exch_ts.into_py(py),
                        f.is_snapshot.into_py(py),
                        f.best_bid.into_py(py),
                        f.best_ask.into_py(py),
                        f.bid_depth.into_py(py),
                        f.ask_depth.into_py(py),
                        f.mid_price.into_py(py),
                        f.spread.into_py(py),
                        f.imbalance.into_py(py),
                    ],
                )
                .into_py(py)
            } else {
                (
                    "bidask",
                    f.symbol.clone(),
                    bids,
                    asks,
                    f.exch_ts,
                    f.is_snapshot,
                )
                    .into_py(py)
            }
        })
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn test_bidask_ring_construction() {
        let rb = FastBidAskRingBuffer::new(32);
        assert_eq!(rb.size, 32);
    }

    #[test]
    fn test_bidask_ring_min_capacity() {
        let rb = FastBidAskRingBuffer::new(0);
        assert_eq!(rb.size, 1);
    }

    #[test]
    fn test_bidask_packed_levels_clone() {
        let levels = BidAskLevels::Packed {
            bid_flat: vec![100, 50, 99, 30],
            bid_rows: 2,
            ask_flat: vec![101, 40, 102, 20],
            ask_rows: 2,
        };
        let cloned = levels.clone();
        match cloned {
            BidAskLevels::Packed {
                bid_flat, bid_rows, ..
            } => {
                assert_eq!(bid_rows, 2);
                assert_eq!(bid_flat, vec![100, 50, 99, 30]);
            }
            _ => panic!("Expected Packed variant"),
        }
    }

    #[test]
    fn test_bidask_frame_clone() {
        let frame = BidAskFrame {
            symbol: "2317".to_string(),
            levels: BidAskLevels::Packed {
                bid_flat: vec![100, 10],
                bid_rows: 1,
                ask_flat: vec![101, 10],
                ask_rows: 1,
            },
            exch_ts: 999,
            is_snapshot: true,
            has_stats: true,
            best_bid: 100,
            best_ask: 101,
            bid_depth: 10,
            ask_depth: 10,
            mid_price: 100.5,
            spread: 1.0,
            imbalance: 0.0,
        };
        let cloned = frame.clone();
        assert_eq!(cloned.symbol, "2317");
        assert_eq!(cloned.exch_ts, 999);
        assert!(cloned.is_snapshot);
    }
}
