use pyo3::prelude::*;
use pyo3::types::PyTuple;
use std::collections::VecDeque;
use std::sync::{Arc, Mutex};

#[pyclass]
pub struct EventBus {
    queue: Arc<Mutex<VecDeque<String>>>,
}

#[pymethods]
impl EventBus {
    #[new]
    pub fn new() -> Self {
        Self {
            queue: Arc::new(Mutex::new(VecDeque::new())),
        }
    }

    pub fn push(&self, event: String) -> PyResult<()> {
        let mut q = self.queue.lock().map_err(|e| {
            PyErr::new::<pyo3::exceptions::PyRuntimeError, _>(format!("lock poisoned: {e}"))
        })?;
        q.push_back(event);
        Ok(())
    }

    pub fn pop(&self) -> PyResult<Option<String>> {
        let mut q = self.queue.lock().map_err(|e| {
            PyErr::new::<pyo3::exceptions::PyRuntimeError, _>(format!("lock poisoned: {e}"))
        })?;
        Ok(q.pop_front())
    }
}

impl Default for EventBus {
    fn default() -> Self {
        Self::new()
    }
}

#[pyclass]
pub struct FastRingBuffer {
    size: usize,
    buffer: Vec<Option<PyObject>>,
}

#[pymethods]
impl FastRingBuffer {
    #[new]
    pub fn new(size: usize) -> Self {
        let size = size.max(1);
        let buffer = vec![None; size];
        Self { size, buffer }
    }

    pub fn capacity(&self) -> usize {
        self.size
    }

    pub fn set(&mut self, idx: usize, event: PyObject) {
        let slot = idx % self.size;
        self.buffer[slot] = Some(event);
    }

    pub fn get<'py>(&self, py: Python<'py>, idx: usize) -> Option<PyObject> {
        let slot = idx % self.size;
        self.buffer[slot].as_ref().map(|obj| obj.clone_ref(py))
    }
}

#[derive(Clone)]
struct TickFrame {
    symbol: String,
    price: i64,
    volume: i64,
    total_volume: i64,
    is_simtrade: bool,
    is_odd_lot: bool,
    exch_ts: i64,
}

#[derive(Clone)]
struct LOBStatsFrame {
    symbol: String,
    ts: i64,
    mid_price_x2: i64,
    spread_scaled: i64,
    imbalance: f64,
    best_bid: i64,
    best_ask: i64,
    bid_depth: i64,
    ask_depth: i64,
}

#[derive(Clone)]
enum BidAskLevels {
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
struct BidAskFrame {
    symbol: String,
    levels: BidAskLevels,
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
}

#[pyclass]
pub struct FastTickRingBuffer {
    size: usize,
    buffer: Vec<Option<TickFrame>>,
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

#[pyclass]
pub struct FastLOBStatsRingBuffer {
    size: usize,
    buffer: Vec<Option<LOBStatsFrame>>,
}

#[pymethods]
impl FastLOBStatsRingBuffer {
    #[new]
    pub fn new(size: usize) -> Self {
        let size = size.max(1);
        let buffer = vec![None; size];
        Self { size, buffer }
    }

    #[allow(clippy::too_many_arguments)]
    pub fn set_stats(
        &mut self,
        idx: usize,
        symbol: String,
        ts: i64,
        mid_price_x2: i64,
        spread_scaled: i64,
        imbalance: f64,
        best_bid: i64,
        best_ask: i64,
        bid_depth: i64,
        ask_depth: i64,
    ) {
        let slot = idx % self.size;
        self.buffer[slot] = Some(LOBStatsFrame {
            symbol,
            ts,
            mid_price_x2,
            spread_scaled,
            imbalance,
            best_bid,
            best_ask,
            bid_depth,
            ask_depth,
        });
    }

    pub fn get<'py>(&self, py: Python<'py>, idx: usize) -> Option<PyObject> {
        let slot = idx % self.size;
        self.buffer[slot].as_ref().map(|f| {
            (
                f.symbol.clone(),
                f.ts,
                f.mid_price_x2,
                f.spread_scaled,
                f.imbalance,
                f.best_bid,
                f.best_ask,
                f.bid_depth,
                f.ask_depth,
            )
                .into_py(py)
        })
    }
}

#[pymethods]
impl FastTickRingBuffer {
    #[new]
    pub fn new(size: usize) -> Self {
        let size = size.max(1);
        let buffer = vec![None; size];
        Self { size, buffer }
    }

    pub fn capacity(&self) -> usize {
        self.size
    }

    #[allow(clippy::too_many_arguments)]
    pub fn set_tick(
        &mut self,
        idx: usize,
        symbol: String,
        price: i64,
        volume: i64,
        total_volume: i64,
        is_simtrade: bool,
        is_odd_lot: bool,
        exch_ts: i64,
    ) {
        let slot = idx % self.size;
        self.buffer[slot] = Some(TickFrame {
            symbol,
            price,
            volume,
            total_volume,
            is_simtrade,
            is_odd_lot,
            exch_ts,
        });
    }

    pub fn get<'py>(&self, py: Python<'py>, idx: usize) -> Option<PyObject> {
        let slot = idx % self.size;
        self.buffer[slot].as_ref().map(|f| {
            (
                "tick",
                f.symbol.clone(),
                f.price,
                f.volume,
                f.total_volume,
                f.is_simtrade,
                f.is_odd_lot,
                f.exch_ts,
            )
                .into_py(py)
        })
    }
}
