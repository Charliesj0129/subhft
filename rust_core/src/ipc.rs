use memmap2::MmapMut;
use pyo3::prelude::*;
use std::fs::OpenOptions;

const HEADER_SIZE: usize = 128; // 64B WriteCursor + 64B ReadCursor (padded)
const SLOT_SIZE: usize = 64;

#[pyclass]
pub struct ShmRingBuffer {
    mmap: MmapMut,
    capacity: usize,
    header_ptr: *mut u64,
    buffer_ptr: *mut u8,
}

unsafe impl Send for ShmRingBuffer {}

#[pymethods]
impl ShmRingBuffer {
    #[new]
    pub fn new(name: String, capacity: usize, create: bool) -> PyResult<Self> {
        let size = HEADER_SIZE + (capacity * SLOT_SIZE);

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

        let mut mmap = unsafe { MmapMut::map_mut(&file)? };

        // Pointers
        let header_ptr = mmap.as_mut_ptr() as *mut u64;
        let buffer_ptr = unsafe { mmap.as_mut_ptr().add(HEADER_SIZE) };

        // Zero header if create
        if create {
            unsafe {
                std::ptr::write_volatile(header_ptr.add(0), 0); // write
                std::ptr::write_volatile(header_ptr.add(1), 0); // read
            }
        }

        Ok(ShmRingBuffer {
            mmap,
            capacity,
            header_ptr,
            buffer_ptr,
        })
    }

    pub fn write(&mut self, data: &[u8]) -> PyResult<bool> {
        unsafe {
            let write_cursor = std::ptr::read_volatile(self.header_ptr.add(0));
            let read_cursor = std::ptr::read_volatile(self.header_ptr.add(1));

            if write_cursor - read_cursor >= self.capacity as u64 {
                return Ok(false);
            }

            let slot_idx = (write_cursor as usize) % self.capacity;
            let offset = slot_idx * SLOT_SIZE;

            let dest = self.buffer_ptr.add(offset);

            // Fast copy
            let len = data.len().min(SLOT_SIZE);
            std::ptr::copy_nonoverlapping(data.as_ptr(), dest, len);

            // Bump cursor
            std::ptr::write_volatile(self.header_ptr.add(0), write_cursor + 1);
            Ok(true)
        }
    }

    pub fn read<'py>(
        &mut self,
        py: Python<'py>,
    ) -> PyResult<Option<Bound<'py, pyo3::types::PyBytes>>> {
        unsafe {
            let write_cursor = std::ptr::read_volatile(self.header_ptr.add(0));
            let read_cursor = std::ptr::read_volatile(self.header_ptr.add(1));

            if read_cursor >= write_cursor {
                return Ok(None);
            }

            let slot_idx = (read_cursor as usize) % self.capacity;
            let offset = slot_idx * SLOT_SIZE;

            let src = self.buffer_ptr.add(offset);
            let bytes = std::slice::from_raw_parts(src, SLOT_SIZE);

            // Create Python Bytes (overhead here, but this is proof of concept)
            let pystruct = pyo3::types::PyBytes::new_bound(py, bytes);

            // Bump cursor
            std::ptr::write_volatile(self.header_ptr.add(1), read_cursor + 1);

            Ok(Some(pystruct))
        }
    }
}
