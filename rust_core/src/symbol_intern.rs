//! SymbolInternTable — bidirectional String ↔ u32 map for symbol interning.
//!
//! Replaces repeated heap-allocated symbol strings with compact u32 ids.
//! Pre-allocates 256 capacity for both Vec and HashMap (Allocator Law).
//! GIL-serialized — no Mutex needed since PyO3 holds the GIL.

use pyo3::prelude::*;
use std::collections::HashMap;

const DEFAULT_CAPACITY: usize = 256;

#[pyclass]
pub struct SymbolInternTable {
    /// id → symbol (O(1) lookup by index)
    id_to_symbol: Vec<String>,
    /// symbol → id (O(1) lookup by hash)
    symbol_to_id: HashMap<String, u32>,
}

#[pymethods]
impl SymbolInternTable {
    #[new]
    pub fn new() -> Self {
        SymbolInternTable {
            id_to_symbol: Vec::with_capacity(DEFAULT_CAPACITY),
            symbol_to_id: HashMap::with_capacity(DEFAULT_CAPACITY),
        }
    }

    /// Intern a symbol string, returning its u32 id.
    /// Returns the existing id if already interned, or assigns the next sequential id.
    pub fn intern(&mut self, symbol: &str) -> u32 {
        if let Some(&id) = self.symbol_to_id.get(symbol) {
            return id;
        }
        let id = self.id_to_symbol.len() as u32;
        let owned = symbol.to_string();
        self.symbol_to_id.insert(owned.clone(), id);
        self.id_to_symbol.push(owned);
        id
    }

    /// Resolve an id back to its symbol string.
    /// Returns None if the id is out of range.
    pub fn resolve(&self, id: u32) -> Option<String> {
        self.id_to_symbol.get(id as usize).cloned()
    }

    /// Number of interned symbols.
    pub fn len(&self) -> usize {
        self.id_to_symbol.len()
    }

    /// Whether the table is empty.
    pub fn is_empty(&self) -> bool {
        self.id_to_symbol.is_empty()
    }

    /// Check if a symbol has been interned.
    pub fn contains(&self, symbol: &str) -> bool {
        self.symbol_to_id.contains_key(symbol)
    }
}

impl Default for SymbolInternTable {
    fn default() -> Self {
        Self::new()
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn test_intern_sequential_ids() {
        let mut table = SymbolInternTable::new();
        assert_eq!(table.intern("2330"), 0);
        assert_eq!(table.intern("2317"), 1);
        assert_eq!(table.intern("2454"), 2);
    }

    #[test]
    fn test_intern_idempotent() {
        let mut table = SymbolInternTable::new();
        let id1 = table.intern("2330");
        let id2 = table.intern("2330");
        assert_eq!(id1, id2);
        assert_eq!(table.len(), 1);
    }

    #[test]
    fn test_resolve() {
        let mut table = SymbolInternTable::new();
        table.intern("2330");
        assert_eq!(table.resolve(0), Some("2330".to_string()));
        assert_eq!(table.resolve(99), None);
    }

    #[test]
    fn test_contains() {
        let mut table = SymbolInternTable::new();
        table.intern("2330");
        assert!(table.contains("2330"));
        assert!(!table.contains("9999"));
    }

    #[test]
    fn test_is_empty() {
        let table = SymbolInternTable::new();
        assert!(table.is_empty());
    }

    #[test]
    fn test_len() {
        let mut table = SymbolInternTable::new();
        table.intern("A");
        table.intern("B");
        table.intern("C");
        assert_eq!(table.len(), 3);
    }

    #[test]
    fn test_default() {
        let table = SymbolInternTable::default();
        assert!(table.is_empty());
    }
}
