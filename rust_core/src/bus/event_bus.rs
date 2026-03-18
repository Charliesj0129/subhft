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

impl Default for EventBus {
    fn default() -> Self {
        Self::new()
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn test_default_creates_empty_bus() {
        let bus = EventBus::default();
        assert!(Arc::strong_count(&bus.queue) == 1);
    }
}
