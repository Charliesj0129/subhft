/// MdEventFrame — cache-line-friendly 128-byte market data event struct.
///
/// All price fields are i64 scaled x10000 per the Precision Law.
/// Layout is `#[repr(C)]` for deterministic memory ordering.

/// Event type discriminator constants.
#[allow(dead_code)]
pub const KIND_TICK: u8 = 1;
#[allow(dead_code)]
pub const KIND_BIDASK: u8 = 2;
#[allow(dead_code)]
pub const KIND_LOB_STATS: u8 = 3;

#[repr(C)]
#[derive(Clone, Copy)]
pub struct MdEventFrame {
    pub kind: u8,
    pub flags: u8,
    pub reserved: u16,
    pub symbol_id: u32,
    pub seq: u64,
    pub exch_ts_ns: u64,
    pub local_ts_ns: u64,
    pub price0: i64,
    pub price1: i64,
    pub qty0: i64,
    pub qty1: i64,
    pub aux0: i64,
    pub aux1: i64,
    pub ratio0: f64,
    /// Padding to 128 bytes (2 cache lines) for contiguous buffer layout.
    pub _pad: [u8; 40],
}

// Compile-time size assertion.
const _: () = assert!(std::mem::size_of::<MdEventFrame>() == 128);

impl Default for MdEventFrame {
    fn default() -> Self {
        Self {
            kind: 0,
            flags: 0,
            reserved: 0,
            symbol_id: 0,
            seq: 0,
            exch_ts_ns: 0,
            local_ts_ns: 0,
            price0: 0,
            price1: 0,
            qty0: 0,
            qty1: 0,
            aux0: 0,
            aux1: 0,
            ratio0: 0.0,
            _pad: [0u8; 40],
        }
    }
}

impl MdEventFrame {
    /// Return all fields as a tuple (useful for Python boundary).
    #[allow(clippy::type_complexity, dead_code)]
    pub fn as_tuple(
        &self,
    ) -> (
        u8,
        u8,
        u16,
        u32,
        u64,
        u64,
        u64,
        i64,
        i64,
        i64,
        i64,
        i64,
        i64,
        f64,
    ) {
        (
            self.kind,
            self.flags,
            self.reserved,
            self.symbol_id,
            self.seq,
            self.exch_ts_ns,
            self.local_ts_ns,
            self.price0,
            self.price1,
            self.qty0,
            self.qty1,
            self.aux0,
            self.aux1,
            self.ratio0,
        )
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn test_size_is_128() {
        assert_eq!(std::mem::size_of::<MdEventFrame>(), 128);
    }

    #[test]
    fn test_default_zeroed() {
        let f = MdEventFrame::default();
        assert_eq!(f.kind, 0);
        assert_eq!(f.seq, 0);
        assert_eq!(f.ratio0, 0.0);
    }

    #[test]
    fn test_as_tuple_roundtrip() {
        let f = MdEventFrame {
            kind: 1,
            flags: 2,
            reserved: 0,
            symbol_id: 42,
            seq: 100,
            exch_ts_ns: 1_000_000,
            local_ts_ns: 2_000_000,
            price0: 1_000_000,
            price1: 2_000_000,
            qty0: 10,
            qty1: 20,
            aux0: 30,
            aux1: 40,
            ratio0: 0.5,
        };
        let t = f.as_tuple();
        assert_eq!(t.0, 1);
        assert_eq!(t.3, 42);
        assert_eq!(t.4, 100);
        assert!((t.13 - 0.5).abs() < f64::EPSILON);
    }
}
