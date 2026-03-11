use pyo3::prelude::*;

/// Rust-native feature engine v2 for LOB-derived microstructure features.
#[pyclass]
pub struct RustFeatureEngineV2 {
    n_features: usize,
}

#[pymethods]
impl RustFeatureEngineV2 {
    #[new]
    fn new(n_features: usize) -> Self {
        Self { n_features }
    }

    fn n_features(&self) -> usize {
        self.n_features
    }
}
