use numpy::ndarray::Array1;
use numpy::{IntoPyArray, PyArray1, PyReadonlyArray1};
use pyo3::prelude::*;

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

        let mut signal = Array1::<f64>::zeros(n);

        for i in 0..(n - 1) {
            let r = returns[i];
            let target = returns[i + 1];

            let prediction = if r > 0.0 {
                self.est_up
            } else if r < 0.0 {
                self.est_dn
            } else {
                self.est_flat
            };

            signal[i] = prediction;

            if r > 0.0 {
                self.est_up = self.est_up * (1.0 - self.alpha) + target * self.alpha;
            } else if r < 0.0 {
                self.est_dn = self.est_dn * (1.0 - self.alpha) + target * self.alpha;
            } else {
                self.est_flat = self.est_flat * (1.0 - self.alpha) + target * self.alpha;
            }
        }

        Ok(signal.into_pyarray_bound(py).unbind())
    }
}
