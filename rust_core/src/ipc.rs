use memmap2::MmapMut;
use pyo3::prelude::*;
use std::fs::OpenOptions;

const HEADER_SIZE: usize = 128; // 64B WriteCursor + 64B ReadCursor (padded)
const SLOT_SIZE: usize = 64;

#[pyclass]
pub struct ShmRingBuffer {
    mmap: MmapMut,
    capacity: usize,
}

#[pymethods]
impl ShmRingBuffer {
    #[new]
    pub fn new(name: String, capacity: usize, create: bool) -> PyResult<Self> {
        if capacity == 0 {
            return Err(pyo3::exceptions::PyValueError::new_err(
                "capacity must be positive",
            ));
        }
        let size = capacity
            .checked_mul(SLOT_SIZE)
            .and_then(|slots| slots.checked_add(HEADER_SIZE))
            .ok_or_else(|| pyo3::exceptions::PyValueError::new_err("capacity is too large"))?;

        let path = if name.starts_with('/') {
            name
        } else {
            format!("/dev/shm/{}", name)
        };

        let file = OpenOptions::new()
            .read(true)
            .write(true)
            .create(create)
            .open(&path)?;

        if create {
            file.set_len(size as u64)?;
        }
        if file.metadata()?.len() < size as u64 {
            return Err(pyo3::exceptions::PyValueError::new_err(
                "shared memory is smaller than the requested capacity",
            ));
        }

        let mut mmap = unsafe { MmapMut::map_mut(&file)? };

        // Zero header if create
        if create {
            unsafe {
                let header_ptr = mmap.as_mut_ptr().cast::<u64>();
                std::ptr::write_volatile(header_ptr.add(0), 0); // write
                std::ptr::write_volatile(header_ptr.add(1), 0); // read
            }
        }

        Ok(ShmRingBuffer { mmap, capacity })
    }

    pub fn write(&mut self, data: &[u8]) -> PyResult<bool> {
        unsafe {
            // The mapping owns these pointers; keep them local to this borrow.
            let base = self.mmap.as_mut_ptr();
            let header_ptr = base.cast::<u64>();
            let buffer_ptr = base.add(HEADER_SIZE);
            let write_cursor = std::ptr::read_volatile(header_ptr.add(0));
            let read_cursor = std::ptr::read_volatile(header_ptr.add(1));

            if write_cursor - read_cursor >= self.capacity as u64 {
                return Ok(false);
            }

            let slot_idx = (write_cursor as usize) % self.capacity;
            let offset = slot_idx * SLOT_SIZE;

            let dest = buffer_ptr.add(offset);

            // Fast copy
            let len = data.len().min(SLOT_SIZE);
            std::ptr::copy_nonoverlapping(data.as_ptr(), dest, len);

            // Bump cursor
            std::ptr::write_volatile(header_ptr.add(0), write_cursor + 1);
            Ok(true)
        }
    }

    pub fn read<'py>(
        &mut self,
        py: Python<'py>,
    ) -> PyResult<Option<Bound<'py, pyo3::types::PyBytes>>> {
        unsafe {
            let base = self.mmap.as_mut_ptr();
            let header_ptr = base.cast::<u64>();
            let buffer_ptr = base.add(HEADER_SIZE);
            let write_cursor = std::ptr::read_volatile(header_ptr.add(0));
            let read_cursor = std::ptr::read_volatile(header_ptr.add(1));

            if read_cursor >= write_cursor {
                return Ok(None);
            }

            let slot_idx = (read_cursor as usize) % self.capacity;
            let offset = slot_idx * SLOT_SIZE;

            let src = buffer_ptr.add(offset);
            let bytes = std::slice::from_raw_parts(src, SLOT_SIZE);

            // Create Python Bytes (overhead here, but this is proof of concept)
            let pystruct = pyo3::types::PyBytes::new(py, bytes);

            // Bump cursor
            std::ptr::write_volatile(header_ptr.add(1), read_cursor + 1);

            Ok(Some(pystruct))
        }
    }
}
