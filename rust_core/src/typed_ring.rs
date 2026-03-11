/// FastTypedRingBuffer — fixed-capacity ring buffer of MdEventFrame.
///
/// Cursor-based publish/get with O(1) writes and lookups.
/// Follows the Allocator Law: single Vec pre-allocated at construction.
use pyo3::prelude::*;
use pyo3::types::PyTuple;

use crate::md_event_frame::MdEventFrame;

#[pyclass]
pub struct FastTypedRingBuffer {
    buf: Vec<MdEventFrame>,
    cap: usize,
    next_seq: u64,
}

#[pymethods]
impl FastTypedRingBuffer {
    #[new]
    pub fn new(capacity: usize) -> Self {
        let cap = capacity.max(1);
        let mut buf = Vec::with_capacity(cap);
        buf.resize(cap, MdEventFrame::default());
        Self {
            buf,
            cap,
            next_seq: 1,
        }
    }

    /// Write a frame into the ring, assign seq, advance cursor. Returns seq.
    #[allow(clippy::too_many_arguments)]
    pub fn publish(
        &mut self,
        kind: u8,
        flags: u8,
        symbol_id: u32,
        exch_ts_ns: u64,
        local_ts_ns: u64,
        price0: i64,
        price1: i64,
        qty0: i64,
        qty1: i64,
        aux0: i64,
        aux1: i64,
        ratio0: f64,
    ) -> u64 {
        let seq = self.next_seq;
        let idx = (seq as usize) % self.cap;
        let frame = &mut self.buf[idx];
        frame.kind = kind;
        frame.flags = flags;
        frame.reserved = 0;
        frame.symbol_id = symbol_id;
        frame.seq = seq;
        frame.exch_ts_ns = exch_ts_ns;
        frame.local_ts_ns = local_ts_ns;
        frame.price0 = price0;
        frame.price1 = price1;
        frame.qty0 = qty0;
        frame.qty1 = qty1;
        frame.aux0 = aux0;
        frame.aux1 = aux1;
        frame.ratio0 = ratio0;
        self.next_seq += 1;
        seq
    }

    /// Retrieve frame by seq. Returns None if seq is out of range or overwritten.
    pub fn get(&self, py: Python<'_>, seq: u64) -> PyResult<Option<PyObject>> {
        // seq 0 is sentinel "no data"
        if seq == 0 || seq >= self.next_seq {
            return Ok(None);
        }
        // Check if seq has been overwritten
        let oldest = self.next_seq.saturating_sub(self.cap as u64).max(1);
        if seq < oldest {
            return Ok(None);
        }
        let idx = (seq as usize) % self.cap;
        let f = &self.buf[idx];
        let tuple = PyTuple::new_bound(
            py,
            &[
                f.kind.into_py(py),
                f.flags.into_py(py),
                f.reserved.into_py(py),
                f.symbol_id.into_py(py),
                f.seq.into_py(py),
                f.exch_ts_ns.into_py(py),
                f.local_ts_ns.into_py(py),
                f.price0.into_py(py),
                f.price1.into_py(py),
                f.qty0.into_py(py),
                f.qty1.into_py(py),
                f.aux0.into_py(py),
                f.aux1.into_py(py),
                f.ratio0.into_py(py),
            ],
        );
        Ok(Some(tuple.into_py(py)))
    }

    /// Current write cursor position (next seq to be assigned).
    pub fn cursor(&self) -> i64 {
        self.next_seq as i64
    }

    /// Ring capacity.
    pub fn capacity(&self) -> usize {
        self.cap
    }
}

impl Default for FastTypedRingBuffer {
    fn default() -> Self {
        Self::new(1024)
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn test_basic_publish_and_fields() {
        let mut ring = FastTypedRingBuffer::new(8);
        assert_eq!(ring.capacity(), 8);
        assert_eq!(ring.cursor(), 1); // next_seq starts at 1

        let seq = ring.publish(1, 2, 42, 100, 200, 10, 20, 30, 40, 50, 60, 0.5);
        assert_eq!(seq, 1);
        assert_eq!(ring.cursor(), 2);

        let idx = (seq as usize) % ring.cap;
        let f = &ring.buf[idx];
        assert_eq!(f.kind, 1);
        assert_eq!(f.flags, 2);
        assert_eq!(f.symbol_id, 42);
        assert_eq!(f.seq, 1);
        assert_eq!(f.price0, 10);
        assert!((f.ratio0 - 0.5).abs() < f64::EPSILON);
    }

    #[test]
    fn test_overwrite_detection() {
        let mut ring = FastTypedRingBuffer::new(4);
        // Publish 6 frames (capacity 4), so seq 1 and 2 should be overwritten
        for i in 0..6u64 {
            ring.publish(1, 0, i as u32, i, i, 0, 0, 0, 0, 0, 0, 0.0);
        }
        // oldest should be 3 (next_seq=7, 7-4=3)
        assert_eq!(ring.next_seq, 7);
        // seq 1 and 2 are gone
        let oldest = ring.next_seq.saturating_sub(ring.cap as u64).max(1);
        assert_eq!(oldest, 3);
    }
}
