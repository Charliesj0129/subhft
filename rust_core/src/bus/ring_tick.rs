use pyo3::prelude::*;

#[derive(Clone)]
pub(crate) struct TickFrame {
    pub symbol: String,
    pub price: i64,
    pub volume: i64,
    pub total_volume: i64,
    pub is_simtrade: bool,
    pub is_odd_lot: bool,
    pub exch_ts: i64,
}

#[pyclass]
pub struct FastTickRingBuffer {
    size: usize,
    buffer: Vec<Option<TickFrame>>,
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

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn test_tick_frame_clone() {
        let frame = TickFrame {
            symbol: "2330".to_string(),
            price: 100_0000,
            volume: 500,
            total_volume: 10000,
            is_simtrade: false,
            is_odd_lot: false,
            exch_ts: 1234567890,
        };
        let cloned = frame.clone();
        assert_eq!(cloned.symbol, "2330");
        assert_eq!(cloned.price, 100_0000);
        assert_eq!(cloned.volume, 500);
        assert_eq!(cloned.total_volume, 10000);
        assert!(!cloned.is_simtrade);
        assert!(!cloned.is_odd_lot);
        assert_eq!(cloned.exch_ts, 1234567890);
    }

    #[test]
    fn test_tick_ring_capacity() {
        let rb = FastTickRingBuffer::new(16);
        assert_eq!(rb.capacity(), 16);
    }

    #[test]
    fn test_tick_ring_min_capacity() {
        let rb = FastTickRingBuffer::new(0);
        assert_eq!(rb.capacity(), 1);
    }
}
