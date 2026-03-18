use memmap2::MmapMut;
use pyo3::prelude::*;
use std::fs::OpenOptions;
use std::sync::atomic::{AtomicU8, Ordering::Acquire, Ordering::Release};

#[pyclass]
pub struct FastGate {
    mmap: MmapMut, // Kill Switch SHM (1 byte)
    kill_atomic: AtomicU8,
    max_price: f64,
    max_qty: f64,
}

unsafe impl Send for FastGate {}

#[pymethods]
impl FastGate {
    #[new]
    pub fn new(kill_shm_name: String, max_price: f64, max_qty: f64) -> PyResult<Self> {
        let path = if kill_shm_name.starts_with('/') {
            kill_shm_name
        } else {
            format!("/dev/shm/{}", kill_shm_name)
        };

        let file = OpenOptions::new()
            .read(true)
            .write(true)
            .create(true) // Auto create for ease
            .truncate(false)
            .open(&path)?;

        file.set_len(1)?;

        let mmap = unsafe { MmapMut::map_mut(&file)? };

        Ok(FastGate {
            mmap,
            kill_atomic: AtomicU8::new(0),
            max_price,
            max_qty,
        })
    }

    pub fn check(&self, price: f64, qty: f64) -> (bool, u8) {
        // Fast-path: same-process atomic (~5ns) before cross-process mmap volatile (~100ns)
        if self.kill_atomic.load(Acquire) > 0 {
            return (false, 1);
        }

        unsafe {
            // 1. Zero-latency Kill Switch
            // read volatile in case another process writes
            let kill_flag = std::ptr::read_volatile(self.mmap.as_ptr());
            if kill_flag > 0 {
                return (false, 1);
            }
        }

        if price <= 0.0 {
            return (false, 2);
        }
        if price > self.max_price {
            return (false, 3);
        }
        if qty <= 0.0 {
            return (false, 5);
        }
        if qty > self.max_qty {
            return (false, 4);
        }

        (true, 0)
    }

    pub fn set_kill_switch(&mut self, active: bool) {
        let val = if active { 1 } else { 0 };
        self.kill_atomic.store(val, Release);
        self.mmap[0] = val;
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::io::Write;
    use tempfile::NamedTempFile;

    fn make_gate(max_price: f64, max_qty: f64) -> FastGate {
        // Create a temp file for mmap (avoids /dev/shm dependency)
        let mut f = NamedTempFile::new().unwrap();
        f.write_all(&[0u8]).unwrap();
        f.flush().unwrap();
        let path = f.path().to_string_lossy().to_string();
        // Keep the file alive by leaking (test only)
        std::mem::forget(f);
        FastGate::new(path, max_price, max_qty).unwrap()
    }

    #[test]
    fn test_check_pass() {
        let gate = make_gate(100_000.0, 10_000.0);
        let (ok, code) = gate.check(50_000.0, 100.0);
        assert!(ok);
        assert_eq!(code, 0);
    }

    #[test]
    fn test_check_price_zero() {
        let gate = make_gate(100_000.0, 10_000.0);
        let (ok, code) = gate.check(0.0, 100.0);
        assert!(!ok);
        assert_eq!(code, 2);
    }

    #[test]
    fn test_check_price_exceeds() {
        let gate = make_gate(100_000.0, 10_000.0);
        let (ok, code) = gate.check(200_000.0, 100.0);
        assert!(!ok);
        assert_eq!(code, 3);
    }

    #[test]
    fn test_check_qty_exceeds() {
        let gate = make_gate(100_000.0, 10_000.0);
        let (ok, code) = gate.check(50_000.0, 20_000.0);
        assert!(!ok);
        assert_eq!(code, 4);
    }

    #[test]
    fn test_kill_switch_atomic() {
        let mut gate = make_gate(100_000.0, 10_000.0);
        assert!(gate.check(50_000.0, 100.0).0);
        gate.set_kill_switch(true);
        let (ok, code) = gate.check(50_000.0, 100.0);
        assert!(!ok);
        assert_eq!(code, 1);
        gate.set_kill_switch(false);
        assert!(gate.check(50_000.0, 100.0).0);
    }

    #[test]
    fn test_qty_zero_rejected() {
        let gate = make_gate(100_000.0, 10_000.0);
        let (ok, code) = gate.check(50_000.0, 0.0);
        assert!(!ok);
        assert_eq!(code, 5);
    }
}
