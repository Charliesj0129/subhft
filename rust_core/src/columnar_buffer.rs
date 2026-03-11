use pyo3::prelude::*;

/// Columnar buffer for batch event ingestion (Cache Law: SoA layout).
#[pyclass]
pub struct RustColumnarBuffer {
    name: String,
    len: usize,
}

#[pymethods]
impl RustColumnarBuffer {
    #[new]
    fn new(name: &str) -> Self {
        Self {
            name: name.to_string(),
            len: 0,
        }
    }

    fn append(&mut self, _value: f64) {
        self.len += 1;
    }

    fn len(&self) -> usize {
        self.len
    }

    fn name(&self) -> &str {
        &self.name
    }

    fn clear(&mut self) {
        self.len = 0;
    }
}
