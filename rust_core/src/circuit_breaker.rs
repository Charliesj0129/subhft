use pyo3::prelude::*;
use std::collections::HashMap;

/// Circuit breaker state constants exposed to Python.
const STATE_NORMAL: u8 = 0;
const STATE_DEGRADED: u8 = 1;
const STATE_HALTED: u8 = 2;

struct CircuitState {
    state: u8,
    failure_count: i64,
    success_count: i64,
    halted_at_ns: i64,
}

impl Default for CircuitState {
    fn default() -> Self {
        Self {
            state: STATE_NORMAL,
            failure_count: 0,
            success_count: 0,
            halted_at_ns: 0,
        }
    }
}

/// Rust-managed 3-state circuit breaker FSM for strategy dispatch.
///
/// Replaces 5 separate Python dict lookups (`_failure_counts`,
/// `_circuit_states`, `_circuit_success_counts`, `_circuit_halted_at_ns`)
/// with a single HashMap lookup per call.
#[pyclass]
pub struct RustCircuitBreaker {
    states: HashMap<String, CircuitState>,
    threshold: i64,
    recovery_threshold: i64,
    cooldown_ns: i64,
}

unsafe impl Send for RustCircuitBreaker {}

#[pymethods]
impl RustCircuitBreaker {
    #[new]
    pub fn new(threshold: i64, recovery_threshold: i64, cooldown_ns: i64) -> Self {
        Self {
            states: HashMap::new(),
            threshold: threshold.max(1),
            recovery_threshold: recovery_threshold.max(1),
            cooldown_ns: cooldown_ns.max(1_000_000_000),
        }
    }

    /// Record a failure for a strategy.
    /// Returns: (new_state: u8, should_disable: bool)
    ///   - STATE_NORMAL=0, STATE_DEGRADED=1, STATE_HALTED=2
    ///   - should_disable=true means caller should set strategy.enabled=False
    pub fn record_failure(&mut self, strategy_id: &str, now_ns: i64) -> (u8, bool) {
        let cs = self.states.entry(strategy_id.to_string()).or_default();

        cs.success_count = 0;
        cs.failure_count += 1;

        let half_threshold = (self.threshold / 2).max(1);

        if cs.state == STATE_NORMAL && cs.failure_count >= half_threshold {
            cs.state = STATE_DEGRADED;
        }

        if cs.failure_count >= self.threshold && cs.state != STATE_HALTED {
            cs.state = STATE_HALTED;
            cs.halted_at_ns = now_ns;
            return (STATE_HALTED, true);
        }

        (cs.state, false)
    }

    /// Record a success for a strategy.
    /// Returns: (new_state: u8, recovered_to_normal: bool)
    pub fn record_success(&mut self, strategy_id: &str) -> (u8, bool) {
        let cs = self.states.entry(strategy_id.to_string()).or_default();

        if cs.state != STATE_DEGRADED {
            return (cs.state, false);
        }

        cs.success_count += 1;
        if cs.success_count >= self.recovery_threshold {
            cs.state = STATE_NORMAL;
            cs.failure_count = 0;
            cs.success_count = 0;
            return (STATE_NORMAL, true);
        }

        (cs.state, false)
    }

    /// Check if a halted strategy is eligible for cooldown recovery.
    /// Returns: (should_reenable: bool, new_state: u8)
    pub fn check_cooldown(&mut self, strategy_id: &str, now_ns: i64) -> (bool, u8) {
        let cs = match self.states.get_mut(strategy_id) {
            Some(cs) => cs,
            None => return (false, STATE_NORMAL),
        };

        if cs.state != STATE_HALTED {
            return (false, cs.state);
        }

        if cs.halted_at_ns > 0 && now_ns - cs.halted_at_ns >= self.cooldown_ns {
            cs.state = STATE_DEGRADED;
            cs.failure_count = self.threshold / 2;
            cs.success_count = 0;
            return (true, STATE_DEGRADED);
        }

        (false, STATE_HALTED)
    }

    /// Get current state for a strategy.
    pub fn get_state(&self, strategy_id: &str) -> u8 {
        self.states
            .get(strategy_id)
            .map_or(STATE_NORMAL, |cs| cs.state)
    }

    /// Reset a strategy's circuit breaker state.
    pub fn reset(&mut self, strategy_id: &str) {
        self.states.remove(strategy_id);
    }

    /// Get failure count for a strategy.
    pub fn get_failure_count(&self, strategy_id: &str) -> i64 {
        self.states
            .get(strategy_id)
            .map_or(0, |cs| cs.failure_count)
    }

    /// State constants for Python consumption.
    #[classattr]
    pub const NORMAL: u8 = STATE_NORMAL;
    #[classattr]
    pub const DEGRADED: u8 = STATE_DEGRADED;
    #[classattr]
    pub const HALTED: u8 = STATE_HALTED;
}
