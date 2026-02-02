use pyo3::prelude::*;
use numpy::{PyArray1, PyReadonlyArray1, IntoPyArray};
use numpy::ndarray::Array1;

#[pyclass]
pub struct AlphaTransientReprice {
    window_size: usize,
}

#[pymethods]
impl AlphaTransientReprice {
    #[new]
    pub fn new(window_size: usize) -> Self {
        AlphaTransientReprice { window_size }
    }

    /// Compute Transient Reprice (Mean Reversion of Returns)
    /// Logic: signal[t] = - (mid[t] - mid[t-k]) / mid[t-k]
    /// Optimized to avoid allocating a 'mid' array.
    fn compute<'py>(
        &self,
        py: Python<'py>,
        bid_p: PyReadonlyArray1<'py, f64>,
        ask_p: PyReadonlyArray1<'py, f64>,
    ) -> PyResult<Py<PyArray1<f64>>> {
        let bid_p = bid_p.as_array();
        let ask_p = ask_p.as_array();
        
        let n = bid_p.len();
        if ask_p.len() != n {
            return Err(pyo3::exceptions::PyValueError::new_err("Input arrays must have same length"));
        }
        
        let k = self.window_size;
        
        // Output array
        let mut signal = Array1::<f64>::zeros(n);
        
        // Loop from k to n
        // mid[t] = (bid[t] + ask[t]) / 2
        for t in k..n {
            let mid_now = (bid_p[t] + ask_p[t]) * 0.5;
            let mid_prev = (bid_p[t-k] + ask_p[t-k]) * 0.5;
            
            // Avoid division by zero if mid_prev is somehow 0 (unlikely for price)
            let val = if mid_prev.abs() > 1e-9 {
                (mid_now - mid_prev) / mid_prev
            } else {
                0.0
            };
            
            // Mean Reversion: invert the return
            signal[t] = -val;
        }

        Ok(signal.into_pyarray_bound(py).unbind())
    }
}
