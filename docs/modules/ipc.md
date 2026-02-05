# ipc

## Purpose
Shared-memory, lock-free primitives used for low-latency intra-process or inter-process signaling.

## Key Files
- `src/hft_platform/ipc/shm_ring_buffer.py`: SPSC ring buffer using `multiprocessing.shared_memory`

## Design Notes
- Fixed slot size (`SLOT_SIZE=64`)
- Header aligned to cache line (`HEADER_SIZE=128`)
- Uses Numba JIT for `_write` / `_read`

## Usage (Python)
```python
from hft_platform.ipc.shm_ring_buffer import ShmRingBuffer

rb = ShmRingBuffer(name="hft_bus", capacity=1024, create=True)
rb.write(b"hello")
msg = rb.read()
rb.close()
rb.unlink()
```

## Tests
- `tests/unit/test_ipc_ring_buffer.py`
