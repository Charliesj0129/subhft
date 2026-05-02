# ipc — Inter-Process Communication

> **Package**: `src/hft_platform/ipc/`
> **Runtime Plane**: Infrastructure
> **Performance**: Writer ~50ns, Reader lock-free (seqlock)

## Overview

Shared memory snapshot table for low-latency cross-process LOB data sharing. Single writer (engine), multiple readers (monitor processes).

## Files

| File | Key Exports | Purpose |
|------|-------------|---------|
| `shm_snapshot.py` | `ShmSnapshotWriter`, `ShmSnapshotReader`, `SnapshotSlot` | Rust-backed seqlock mmap table |

## Architecture

```
Engine Process (writer) → [SHM segment, seqlock] → Monitor Process (reader)
```

## Key APIs

```python
# Writer (engine side)
with ShmSnapshotWriter(name="hft_monitor_snapshot", max_symbols=64) as w:
    w.publish(slot_idx=0, ts_ns=now_ns(), symbol_hash=hash, lob_fields=(...,), features=(...,))

# Reader (monitor side)
with ShmSnapshotReader(name="hft_monitor_snapshot", max_symbols=64) as r:
    slot = r.read_slot(0)  # Returns SnapshotSlot or None
    version = r.global_version()
```

## SnapshotSlot

```python
@dataclass
class SnapshotSlot:
    version: int
    ts_ns: int                      # Nanosecond timestamp
    symbol_hash: int                # FNV-1a 64-bit
    lob_fields: tuple[int, ...]     # 9 LOB stats (x10000 scaled)
    features: tuple[int, ...]       # 16 feature values
```

## Protocol

- **Seqlock**: Consistent reads without locks (version incremented before/after write)
- **Single writer only**: Called from engine, multiple readers allowed
- **Rust backend**: `hft_platform.rust_core.ShmSnapshotTable`
- **Segment name**: Configurable, default `"hft_monitor_snapshot"`
