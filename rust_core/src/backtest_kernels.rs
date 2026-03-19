//! Backtest hot-path kernels: signal-to-position conversion and latency simulation.
//!
//! These replace the Python loops in `research/backtest/hft_native_runner.py`
//! with zero-allocation Rust equivalents. Both functions are O(n) single-pass.

use pyo3::prelude::*;

/// Convert a signal array into a position ladder.
///
/// For each element:
///   direction = +1 if signal > threshold, -1 if signal < -threshold, else 0.
///   positions[i] = clamp(positions[i-1] + direction, -max_pos, max_pos)
///
/// positions[0] is always 0.0 (flat start).
///
/// # Arguments
/// * `signals` - Signal values (one per time step).
/// * `threshold` - Absolute threshold for signal activation.
/// * `max_pos` - Maximum absolute position (symmetric clamp).
///
/// # Returns
/// Position array of the same length as `signals`.
#[pyfunction]
pub fn signals_to_positions(signals: Vec<f64>, threshold: f64, max_pos: i32) -> Vec<f64> {
    let n = signals.len();
    if n == 0 {
        return Vec::new();
    }

    let max_f = f64::from(max_pos);
    let neg_max_f = -max_f;
    let mut positions = vec![0.0_f64; n];

    for i in 1..n {
        let sig = signals[i];
        let prev = positions[i - 1];

        if sig > threshold {
            let p = prev + 1.0;
            positions[i] = if p <= max_f { p } else { max_f };
        } else if sig < -threshold {
            let p = prev - 1.0;
            positions[i] = if p >= neg_max_f { p } else { neg_max_f };
        } else {
            positions[i] = prev;
        }
    }

    positions
}

/// Simulate broker latency by delaying position changes.
///
/// State machine: when `desired[i]` differs from `desired[i-1]`, a new order is
/// submitted that arrives at `min(n-1, i + submit_steps)`. Until arrival, the
/// executed position holds its previous value. A newer submission cancels any
/// pending order (last-write-wins).
///
/// executed[0] is always 0.0 (flat start).
///
/// # Arguments
/// * `desired` - Desired position array (output of `signals_to_positions`).
/// * `submit_steps` - Broker round-trip delay in number of steps.
///
/// # Returns
/// Executed position array of the same length as `desired`.
#[pyfunction]
pub fn apply_latency_to_positions(desired: Vec<f64>, submit_steps: i32) -> Vec<f64> {
    let n = desired.len();
    if n == 0 {
        return Vec::new();
    }

    let steps = submit_steps.max(0) as usize;
    let mut executed = vec![0.0_f64; n];

    // pending_due == usize::MAX means no pending order
    let mut pending_due: usize = usize::MAX;
    let mut pending_target: f64 = 0.0;

    for i in 1..n {
        // Carry forward previous executed position
        executed[i] = executed[i - 1];

        // Check if pending order has arrived
        if pending_due != usize::MAX && i >= pending_due {
            executed[i] = pending_target;
            pending_due = usize::MAX;
        }

        let target = desired[i];

        // No change in desired position — nothing to submit
        if target == desired[i - 1] {
            continue;
        }

        // Desired already matches executed — cancel any pending
        if target == executed[i] {
            pending_due = usize::MAX;
            continue;
        }

        // Submit new order (overwrites any existing pending)
        let due = i + steps;
        pending_due = if due < n { due } else { n - 1 };
        pending_target = target;
    }

    executed
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn test_signals_to_positions_empty() {
        let result = signals_to_positions(vec![], 0.5, 3);
        assert!(result.is_empty());
    }

    #[test]
    fn test_signals_to_positions_basic() {
        // signals: [0, 1, 1, -1, -1, 0]
        // threshold=0.5, max_pos=2
        // directions: [_, +1, +1, -1, -1, 0]
        // positions: [0, 1, 2, 1, 0, 0]
        let signals = vec![0.0, 1.0, 1.0, -1.0, -1.0, 0.0];
        let result = signals_to_positions(signals, 0.5, 2);
        assert_eq!(result, vec![0.0, 1.0, 2.0, 1.0, 0.0, 0.0]);
    }

    #[test]
    fn test_signals_to_positions_clamp() {
        // All positive signals, max_pos=1
        let signals = vec![0.0, 1.0, 1.0, 1.0];
        let result = signals_to_positions(signals, 0.5, 1);
        assert_eq!(result, vec![0.0, 1.0, 1.0, 1.0]);
    }

    #[test]
    fn test_signals_to_positions_negative_clamp() {
        let signals = vec![0.0, -1.0, -1.0, -1.0];
        let result = signals_to_positions(signals, 0.5, 1);
        assert_eq!(result, vec![0.0, -1.0, -1.0, -1.0]);
    }

    #[test]
    fn test_apply_latency_empty() {
        let result = apply_latency_to_positions(vec![], 3);
        assert!(result.is_empty());
    }

    #[test]
    fn test_apply_latency_no_delay() {
        // submit_steps=0: order submitted at step i, executed at step i
        // Due to carry-forward + check ordering, there is a 1-step inherent delay
        let desired = vec![0.0, 1.0, 1.0, 2.0];
        let result = apply_latency_to_positions(desired, 0);
        assert_eq!(result, vec![0.0, 0.0, 1.0, 1.0]);
    }

    #[test]
    fn test_apply_latency_basic() {
        // desired:  [0, 1, 1, 1, 1, 1]
        // submit_steps=2: change at i=1, arrives at i=3
        // executed: [0, 0, 0, 1, 1, 1]
        let desired = vec![0.0, 1.0, 1.0, 1.0, 1.0, 1.0];
        let result = apply_latency_to_positions(desired, 2);
        assert_eq!(result, vec![0.0, 0.0, 0.0, 1.0, 1.0, 1.0]);
    }

    #[test]
    fn test_apply_latency_overwrite_pending() {
        // desired:  [0, 1, 2, 2, 2, 2]
        // submit_steps=3
        // i=1: change 0->1, pending_due=4, pending_target=1
        // i=2: change 1->2, pending_due=5, pending_target=2 (overwrites)
        // i=3: no change, carry forward 0
        // i=4: no change, carry forward 0
        // i=5: arrival, executed=2
        let desired = vec![0.0, 1.0, 2.0, 2.0, 2.0, 2.0];
        let result = apply_latency_to_positions(desired, 3);
        assert_eq!(result, vec![0.0, 0.0, 0.0, 0.0, 0.0, 2.0]);
    }
}
