use pyo3::prelude::*;
use std::sync::{Arc, Mutex};
use std::collections::VecDeque;

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

    pub fn push(&self, event: String) {
        let mut q = self.queue.lock().unwrap();
        q.push_back(event);
    }

    pub fn pop(&self) -> Option<String> {
        let mut q = self.queue.lock().unwrap();
        q.pop_front()
    }
}
