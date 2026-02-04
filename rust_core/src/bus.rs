use pyo3::prelude::*;
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
