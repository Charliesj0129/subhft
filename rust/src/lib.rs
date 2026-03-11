use pyo3::prelude::*;

pub mod rl;
pub mod total_depth;

pub use rl::RLStrategy;
pub use total_depth::TotalDepthStrategy;

#[pymodule]
fn rust_strategy(_py: Python, m: &Bound<'_, PyModule>) -> PyResult<()> {
    m.add_class::<RLStrategy>()?;
    m.add_class::<rl::RLParams>()?;
    Ok(())
}
