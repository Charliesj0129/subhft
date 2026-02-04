use pyo3::prelude::*;

pub mod total_depth;
pub mod rl;

pub use total_depth::TotalDepthStrategy;
pub use rl::RLStrategy;

#[pymodule]
fn rust_strategy(_py: Python, m: &Bound<'_, PyModule>) -> PyResult<()> {
    m.add_class::<RLStrategy>()?;
    m.add_class::<rl::RLParams>()?;
    Ok(())
}

