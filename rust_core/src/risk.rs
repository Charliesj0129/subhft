use memmap2::MmapMut;
use pyo3::prelude::*;
use std::fs::OpenOptions;

#[pyclass]
pub struct FastGate {
    mmap: MmapMut, // Kill Switch SHM (1 byte)
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
            .open(&path)?;

        file.set_len(1)?;

        let mmap = unsafe { MmapMut::map_mut(&file)? };

        Ok(FastGate {
            mmap,
            max_price,
            max_qty,
        })
    }

    pub fn check(&self, price: f64, qty: f64) -> (bool, u8) {
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
        self.mmap[0] = if active { 1 } else { 0 };
    }
}
