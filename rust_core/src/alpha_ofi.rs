use numpy::ndarray::Array1;
use numpy::{IntoPyArray, PyArray1, PyReadonlyArray1};
use pyo3::prelude::*;

#[pyclass]
pub struct AlphaOFI {
    // No internal state needed for basic OFI, but struct required for class
}

#[pymethods]
impl AlphaOFI {
    #[new]
    pub fn new() -> Self {
        AlphaOFI {}
    }

    /// Compute Order Flow Imbalance (OFI)
    /// Input: bid_p, ask_p, bid_v, ask_v (1D arrays)
    /// Output: ofi (1D array)
    /// Performance: O(N) single pass, no intermediate allocations
    fn compute<'py>(
        &self,
        py: Python<'py>,
        bid_p: PyReadonlyArray1<'py, f64>,
        ask_p: PyReadonlyArray1<'py, f64>,
        bid_v: PyReadonlyArray1<'py, f64>,
        ask_v: PyReadonlyArray1<'py, f64>,
    ) -> PyResult<Py<PyArray1<f64>>> {
        let bid_p = bid_p.as_array();
        let ask_p = ask_p.as_array();
        let bid_v = bid_v.as_array();
        let ask_v = ask_v.as_array();

        let n = bid_p.len();
        // Ensure all lengths match
        if ask_p.len() != n || bid_v.len() != n || ask_v.len() != n {
            return Err(pyo3::exceptions::PyValueError::new_err(
                "Input arrays must have same length",
            ));
        }

        // Allocate output array once
        let mut ofi = Array1::<f64>::zeros(n);

        // Loop from 1 to n (skip 0)
        for t in 1..n {
            // Bid flow
            let b_flow = if bid_p[t] > bid_p[t - 1] {
                bid_v[t]
            } else if bid_p[t] < bid_p[t - 1] {
                -bid_v[t - 1]
            } else {
                bid_v[t] - bid_v[t - 1]
            };

            // Ask flow
            let a_flow = if ask_p[t] < ask_p[t - 1] {
                ask_v[t]
            } else if ask_p[t] > ask_p[t - 1] {
                -ask_v[t - 1]
            } else {
                ask_v[t] - ask_v[t - 1]
            };

            ofi[t] = a_flow - b_flow;
        }

        // Convert to Python Object (Zero-Copy if possible, but here we transfer ownership of new array)
        Ok(ofi.into_pyarray_bound(py).unbind())
    }
}

impl Default for AlphaOFI {
    fn default() -> Self {
        Self::new()
    }
}
