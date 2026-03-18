use pyo3::prelude::*;

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

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn test_capacity_minimum_one() {
        let rb = FastRingBuffer::new(0);
        assert_eq!(rb.capacity(), 1);
    }

    #[test]
    fn test_capacity_preserved() {
        let rb = FastRingBuffer::new(64);
        assert_eq!(rb.capacity(), 64);
    }
}
