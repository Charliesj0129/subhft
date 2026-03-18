//! Seqlock-based shared memory snapshot table for monitor IPC.
//!
//! Layout: `[Header 128B][Slot0 256B][Slot1 256B]...[SlotN 256B]`
//!
//! Header (128B):
//!   - magic:           u64  (0x484654_534E415000 = "HFT_SNAP")
//!   - max_symbols:     u64
//!   - global_version:  u64  (bumped on every write)
//!   - padding:         104B
//!
//! Slot (256B):
//!   - version:         u64  (odd = writing, even = stable)
//!   - ts_ns:           i64
//!   - symbol_hash:     u64
//!   - 9 LOB fields:    i64 × 9  (best_bid, best_ask, mid_price_x2, spread_scaled,
//!     bid_depth, ask_depth, l1_bid_qty, l1_ask_qty, microprice_x2)
//!   - 16 features:     i64 × 16
//!   - padding to 256B

use memmap2::MmapMut;
use pyo3::prelude::*;
use std::fs::OpenOptions;

const HEADER_SIZE: usize = 128;
const SLOT_SIZE: usize = 256;
const MAGIC: u64 = 0x0048_4654_534E_4150; // "HFT_SNAP" (truncated)

// Offsets within a slot (in bytes)
const SLOT_VERSION_OFF: usize = 0;
const SLOT_TS_OFF: usize = 8;
const SLOT_HASH_OFF: usize = 16;
const SLOT_LOB_OFF: usize = 24; // 9 × i64 = 72B
const SLOT_FEAT_OFF: usize = 96; // 16 × i64 = 128B
// total used: 96 + 128 = 224B, padding to 256B

/// Number of i64 LOB fields per slot.
const LOB_FIELDS: usize = 9;
/// Number of i64 feature fields per slot.
const FEAT_FIELDS: usize = 16;

#[pyclass]
pub struct ShmSnapshotTable {
    #[allow(dead_code)]
    mmap: MmapMut,
    max_symbols: usize,
    base: *mut u8,
}

unsafe impl Send for ShmSnapshotTable {}

#[pymethods]
impl ShmSnapshotTable {
    /// Create or open a shared-memory snapshot table.
    ///
    /// * `name` — SHM segment name (prepended with `/dev/shm/` if no leading `/`)
    /// * `max_symbols` — number of symbol slots
    /// * `create` — if true, create + zero-init; if false, open existing
    #[new]
    pub fn new(name: String, max_symbols: usize, create: bool) -> PyResult<Self> {
        let size = HEADER_SIZE + max_symbols * SLOT_SIZE;

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
        let base = mmap.as_mut_ptr();

        if create {
            unsafe {
                // Zero everything
                std::ptr::write_bytes(base, 0, size);
                // Write magic + max_symbols
                let hdr = base as *mut u64;
                std::ptr::write_volatile(hdr, MAGIC);
                std::ptr::write_volatile(hdr.add(1), max_symbols as u64);
                // global_version starts at 0
            }
        }

        Ok(ShmSnapshotTable {
            mmap,
            max_symbols,
            base,
        })
    }

    /// Write a snapshot slot (single-writer, seqlock protocol).
    ///
    /// * `slot_idx` — symbol index (0..max_symbols)
    /// * `ts_ns` — timestamp in nanoseconds
    /// * `symbol_hash` — pre-hashed symbol identifier
    /// * `lob_fields` — 9 i64 values (LOB stats)
    /// * `features` — 16 i64 values (feature tuple)
    pub fn write_slot(
        &mut self,
        slot_idx: usize,
        ts_ns: i64,
        symbol_hash: u64,
        lob_fields: Vec<i64>,
        features: Vec<i64>,
    ) -> PyResult<()> {
        if slot_idx >= self.max_symbols {
            return Err(pyo3::exceptions::PyIndexError::new_err("slot_idx out of range"));
        }
        if lob_fields.len() != LOB_FIELDS {
            return Err(pyo3::exceptions::PyValueError::new_err(format!(
                "lob_fields must have {} elements, got {}",
                LOB_FIELDS,
                lob_fields.len()
            )));
        }
        if features.len() != FEAT_FIELDS {
            return Err(pyo3::exceptions::PyValueError::new_err(format!(
                "features must have {} elements, got {}",
                FEAT_FIELDS,
                features.len()
            )));
        }

        unsafe {
            let slot_base = self.base.add(HEADER_SIZE + slot_idx * SLOT_SIZE);
            let ver_ptr = slot_base.add(SLOT_VERSION_OFF) as *mut u64;

            // Seqlock: read current version, write odd (in-progress)
            let cur_ver = std::ptr::read_volatile(ver_ptr);
            let new_ver = cur_ver.wrapping_add(1); // odd = writing
            std::ptr::write_volatile(ver_ptr, new_ver);

            // Store fence: ensure version is visible before data write
            std::sync::atomic::fence(std::sync::atomic::Ordering::Release);

            // Write ts_ns
            let ts_ptr = slot_base.add(SLOT_TS_OFF) as *mut i64;
            std::ptr::write_volatile(ts_ptr, ts_ns);

            // Write symbol_hash
            let hash_ptr = slot_base.add(SLOT_HASH_OFF) as *mut u64;
            std::ptr::write_volatile(hash_ptr, symbol_hash);

            // Write LOB fields
            let lob_ptr = slot_base.add(SLOT_LOB_OFF) as *mut i64;
            std::ptr::copy_nonoverlapping(lob_fields.as_ptr(), lob_ptr, LOB_FIELDS);

            // Write features
            let feat_ptr = slot_base.add(SLOT_FEAT_OFF) as *mut i64;
            std::ptr::copy_nonoverlapping(features.as_ptr(), feat_ptr, FEAT_FIELDS);

            // Store fence + bump to even (stable)
            std::sync::atomic::fence(std::sync::atomic::Ordering::Release);
            std::ptr::write_volatile(ver_ptr, new_ver.wrapping_add(1)); // even = done

            // Bump global version
            let gv_ptr = (self.base as *mut u64).add(2);
            let gv = std::ptr::read_volatile(gv_ptr);
            std::ptr::write_volatile(gv_ptr, gv.wrapping_add(1));
        }

        Ok(())
    }

    /// Read a snapshot slot (seqlock: retry on torn read).
    ///
    /// Returns `(version, ts_ns, symbol_hash, lob_fields, features)` or `None`
    /// if the slot has never been written (version == 0).
    ///
    /// Retries up to 16 times on torn reads (version changed during read).
    #[allow(clippy::type_complexity)]
    pub fn read_slot(
        &self,
        slot_idx: usize,
    ) -> PyResult<Option<(u64, i64, u64, Vec<i64>, Vec<i64>)>> {
        if slot_idx >= self.max_symbols {
            return Err(pyo3::exceptions::PyIndexError::new_err("slot_idx out of range"));
        }

        unsafe {
            let slot_base = self.base.add(HEADER_SIZE + slot_idx * SLOT_SIZE);
            let ver_ptr = slot_base.add(SLOT_VERSION_OFF) as *const u64;

            for _ in 0..16 {
                let v1 = std::ptr::read_volatile(ver_ptr);

                // Never written
                if v1 == 0 {
                    return Ok(None);
                }

                // Odd version = write in progress, spin
                if v1 & 1 != 0 {
                    std::hint::spin_loop();
                    continue;
                }

                // Load fence: ensure we see data written before version bump
                std::sync::atomic::fence(std::sync::atomic::Ordering::Acquire);

                // Read data
                let ts_ns = std::ptr::read_volatile(slot_base.add(SLOT_TS_OFF) as *const i64);
                let symbol_hash =
                    std::ptr::read_volatile(slot_base.add(SLOT_HASH_OFF) as *const u64);

                let mut lob_fields = vec![0i64; LOB_FIELDS];
                let lob_src = slot_base.add(SLOT_LOB_OFF) as *const i64;
                std::ptr::copy_nonoverlapping(lob_src, lob_fields.as_mut_ptr(), LOB_FIELDS);

                let mut features = vec![0i64; FEAT_FIELDS];
                let feat_src = slot_base.add(SLOT_FEAT_OFF) as *const i64;
                std::ptr::copy_nonoverlapping(feat_src, features.as_mut_ptr(), FEAT_FIELDS);

                // Re-check version (seqlock validation)
                std::sync::atomic::fence(std::sync::atomic::Ordering::Acquire);
                let v2 = std::ptr::read_volatile(ver_ptr);

                if v1 == v2 {
                    return Ok(Some((v1, ts_ns, symbol_hash, lob_fields, features)));
                }

                // Version changed — retry
                std::hint::spin_loop();
            }

            // All retries exhausted — return None (treat as unavailable)
            Ok(None)
        }
    }

    /// Read the global version counter (monotonically increasing).
    pub fn global_version(&self) -> u64 {
        unsafe {
            let gv_ptr = (self.base as *const u64).add(2);
            std::ptr::read_volatile(gv_ptr)
        }
    }

    /// Return max_symbols capacity.
    pub fn max_symbols(&self) -> usize {
        self.max_symbols
    }
}
