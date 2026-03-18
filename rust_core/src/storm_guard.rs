use pyo3::prelude::*;

/// Rust-native StormGuard FSM validator for hot-path risk checks.
#[pyclass]
pub struct RustStormGuardValidator {
    state: u8, // 0=NORMAL, 1=WARN, 2=STORM
    tick_threshold: u64,
    tick_count: u64,
}

#[pymethods]
impl RustStormGuardValidator {
    #[new]
    #[pyo3(signature = (tick_threshold=1000))]
    fn new(tick_threshold: u64) -> Self {
        Self {
            state: 0,
            tick_threshold,
            tick_count: 0,
        }
    }

    fn check(&mut self) -> u8 {
        self.tick_count += 1;
        if self.tick_count > self.tick_threshold * 2 {
            self.state = 2;
        } else if self.tick_count > self.tick_threshold {
            self.state = 1;
        }
        self.state
    }

    fn reset(&mut self) {
        self.state = 0;
        self.tick_count = 0;
    }

    fn state(&self) -> u8 {
        self.state
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn test_initial_normal() {
        let sg = RustStormGuardValidator::new(100);
        assert_eq!(sg.state(), 0);
    }

    #[test]
    fn test_normal_to_warn() {
        let mut sg = RustStormGuardValidator::new(100);
        for _ in 0..101 {
            sg.check();
        }
        assert_eq!(sg.state(), 1);
    }

    #[test]
    fn test_warn_to_storm() {
        let mut sg = RustStormGuardValidator::new(100);
        for _ in 0..201 {
            sg.check();
        }
        assert_eq!(sg.state(), 2);
    }

    #[test]
    fn test_reset() {
        let mut sg = RustStormGuardValidator::new(100);
        for _ in 0..201 {
            sg.check();
        }
        assert_eq!(sg.state(), 2);
        sg.reset();
        assert_eq!(sg.state(), 0);
    }
}
