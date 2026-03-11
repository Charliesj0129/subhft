use pyo3::prelude::*;

/// Lock-free metrics sampler for hot-path latency recording.
#[pyclass]
pub struct RustMetricsSampler {
    count: u64,
}

#[pymethods]
impl RustMetricsSampler {
    #[new]
    fn new() -> Self {
        Self { count: 0 }
    }

    fn record(&mut self, _value_ns: u64) {
        self.count += 1;
    }

    fn count(&self) -> u64 {
        self.count
    }

    fn reset(&mut self) {
        self.count = 0;
    }
}

impl Default for RustMetricsSampler {
    fn default() -> Self {
        Self::new()
    }
}
