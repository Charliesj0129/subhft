use pyo3::prelude::*;
use numpy::{PyArray1, PyReadonlyArray1, IntoPyArray};
use numpy::ndarray::Array1;

#[pyclass]
pub struct AlphaMarkovTransition {
    alpha: f64,
    est_up: f64,
    est_dn: f64,
    est_flat: f64,
}

#[pymethods]
impl AlphaMarkovTransition {
    #[new]
    pub fn new(alpha: f64) -> Self {
        AlphaMarkovTransition {
            alpha,
            est_up: 0.0,
            est_dn: 0.0,
            est_flat: 0.0,
        }
    }

    /// Compute Markov Transition Signal
    /// Input: returns (1D array)
    /// Output: expected_next_return (1D array)
    fn compute<'py>(
        &mut self,
        py: Python<'py>,
        returns: PyReadonlyArray1<'py, f64>,
    ) -> PyResult<Py<PyArray1<f64>>> {
        let returns = returns.as_array();
        let n = returns.len();
        
        // Output array
        let mut signal = Array1::<f64>::zeros(n);
        
        // Iterate through returns
        // note: signal[i] is prediction for returns[i+1] based on state at i
        // state[i] is based on returns[i]
        
        for i in 0..(n - 1) {
            let r = returns[i];
            let target = returns[i+1];
            
            // Determine state
            // 1: Up, -1: Down, 0: Flat
            let prediction = if r > 0.0 {
                self.est_up
            } else if r < 0.0 {
                self.est_dn
            } else {
                self.est_flat
            };
            
            signal[i] = prediction;
            
            // Update expectation for the *current* state using the *target* (next return)
            if r > 0.0 {
                self.est_up = self.est_up * (1.0 - self.alpha) + target * self.alpha;
            } else if r < 0.0 {
                self.est_dn = self.est_dn * (1.0 - self.alpha) + target * self.alpha;
            } else {
                self.est_flat = self.est_flat * (1.0 - self.alpha) + target * self.alpha;
            }
        }
        
        // Final Signal Logic from optimization:
        // "Inverted the MarkovTransition signal by returning +signal instead of -signal"
        // Wait, the Python code had: return -_compute_markov_numba(returns) INITIALLY
        // Then I changed it to return +_compute_markov_numba(returns)??
        // Let's check `factor_registry.py` history or content.
        // History says: "Inverted the MarkovTransition signal by returning +signal instead of -signal to correct the negative correlation"
        // So the raw signal from `_compute_markov_numba` was correct, but previously it was being negated.
        // My Rust code here implements `_compute_markov_numba`.
        // So I should return `signal` as is.
        
        Ok(signal.into_pyarray_bound(py).unbind())
    }
}
