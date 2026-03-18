use pyo3::prelude::*;

#[derive(Clone)]
pub(crate) struct LOBStatsFrame {
    pub symbol: String,
    pub ts: i64,
    pub mid_price_x2: i64,
    pub spread_scaled: i64,
    pub imbalance: f64,
    pub best_bid: i64,
    pub best_ask: i64,
    pub bid_depth: i64,
    pub ask_depth: i64,
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

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn test_lob_stats_frame_clone() {
        let frame = LOBStatsFrame {
            symbol: "2330".to_string(),
            ts: 1000,
            mid_price_x2: 200_0000,
            spread_scaled: 1_0000,
            imbalance: 0.1,
            best_bid: 99_5000,
            best_ask: 100_5000,
            bid_depth: 500,
            ask_depth: 400,
        };
        let cloned = frame.clone();
        assert_eq!(cloned.mid_price_x2, 200_0000);
        assert_eq!(cloned.spread_scaled, 1_0000);
        assert_eq!(cloned.symbol, "2330");
    }

    #[test]
    fn test_lob_ring_construction() {
        let rb = FastLOBStatsRingBuffer::new(8);
        assert_eq!(rb.size, 8);
    }

    #[test]
    fn test_lob_ring_min_capacity() {
        let rb = FastLOBStatsRingBuffer::new(0);
        assert_eq!(rb.size, 1);
    }
}
