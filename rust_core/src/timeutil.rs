//! Fast timestamp coercion: magnitude-detection for ns/us/ms/s conversion.
//!
//! Replaces Python's `coerce_ns()` 8-branch if/elif chain with a Rust function.
//! Called per tick on the hot path.

use pyo3::prelude::*;

/// Coerce an integer timestamp to nanoseconds based on magnitude.
///
/// Rules:
///   abs < 1e11 → seconds, * 1_000_000_000
///   abs < 1e14 → milliseconds, * 1_000_000
///   abs < 1e17 → microseconds, * 1_000
///   else       → already nanoseconds
#[pyfunction]
pub fn coerce_ns_int(ts: i64) -> i64 {
    let abs_ts = ts.unsigned_abs();
    if abs_ts < 100_000_000_000 {
        // < 1e11: seconds
        ts * 1_000_000_000
    } else if abs_ts < 100_000_000_000_000 {
        // < 1e14: milliseconds
        ts * 1_000_000
    } else if abs_ts < 100_000_000_000_000_000 {
        // < 1e17: microseconds
        ts * 1_000
    } else {
        // nanoseconds
        ts
    }
}

/// Coerce a float timestamp to nanoseconds based on magnitude.
#[pyfunction]
pub fn coerce_ns_float(ts: f64) -> i64 {
    let abs_ts = ts.abs();
    if abs_ts < 1e11 {
        (ts * 1e9) as i64
    } else if abs_ts < 1e14 {
        (ts * 1e6) as i64
    } else if abs_ts < 1e17 {
        (ts * 1e3) as i64
    } else {
        ts as i64
    }
}
