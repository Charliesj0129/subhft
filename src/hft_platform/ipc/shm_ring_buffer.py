from multiprocessing import shared_memory

import numpy as np
from numba import njit

# Layout Constants
# Header: [ write_cursor(8B) | read_cursor(8B) | ... padding ... ]
HEADER_SIZE = 128  # Align to cache line (usually 64, safe 128)
CURSOR_DTYPE = np.int64

# Msg Format: [ symbol_id(8B) | price(8B) | qty(8B) | timestamp(8B) | ... ]
# Let's define a fixed slot size
SLOT_SIZE = 64


@njit
def _write(buf_ptr, header_ptr, capacity, data):
    """
    Lock-free Single-Producer Write.
    Returns True if successful, False if full.
    """
    # Load cursors from header
    # header_ptr[0] is write_cursor
    # header_ptr[1] is read_cursor

    write_cursor = header_ptr[0]
    read_cursor = header_ptr[1]

    # Check full: (write - read) >= capacity
    # This assumes cursors are monotonically increasing integers (not modded yet)
    if write_cursor - read_cursor >= capacity:
        return False

    # Calculate slot index
    slot_idx = write_cursor % capacity
    offset = slot_idx * SLOT_SIZE

    # Copy data to buffer
    # buf_ptr is a byte array (uint8[:])
    # data is expected to be a byte array of max SLOT_SIZE
    for i in range(len(data)):
        buf_ptr[offset + i] = data[i]

    # Memory Barrier (StoreStore) to ensure data is visible before cursor update
    # In Python/Numba, explicit fencing is hard, but dependent stores usually order.
    # For stricter ordering, one might need intrinsic atomic fence.
    # Here we rely on x86 TSO or standard volatile behavior.

    # Update write cursor
    header_ptr[0] = write_cursor + 1
    return True


@njit
def _read(buf_ptr, header_ptr, capacity, out_data):
    """
    Lock-free Single-Consumer Read.
    Returns True if data read, False if empty.
    """
    write_cursor = header_ptr[0]  # Volatile load ideally
    read_cursor = header_ptr[1]

    # Check empty
    if read_cursor >= write_cursor:
        return False

    slot_idx = read_cursor % capacity
    offset = slot_idx * SLOT_SIZE

    # Copy data out
    for i in range(SLOT_SIZE):
        out_data[i] = buf_ptr[offset + i]

    # Memory Barrier (LoadStore)

    # Update read cursor
    header_ptr[1] = read_cursor + 1
    return True


class ShmRingBuffer:
    def __init__(self, name, capacity=1024, create=False):
        self.name = name
        self.capacity = capacity
        # Total Size = Header + Data
        self.total_size = HEADER_SIZE + (capacity * SLOT_SIZE)

        if create:
            try:
                self.shm = shared_memory.SharedMemory(name=name, create=True, size=self.total_size)
                # Initialize header
                self.shm.buf[:16] = b"\x00" * 16
            except FileExistsError:
                self.shm = shared_memory.SharedMemory(name=name)
        else:
            self.shm = shared_memory.SharedMemory(name=name)

        # Create numpy views for Numba
        # Buffer part starts at HEADER_SIZE
        self.buf_array = np.ndarray((capacity * SLOT_SIZE,), dtype=np.uint8, buffer=self.shm.buf, offset=HEADER_SIZE)

        # Header part (2 int64s)
        self.header_array = np.ndarray((2,), dtype=np.int64, buffer=self.shm.buf, offset=0)

    def write(self, data_bytes):
        # Python wrapper for Numba write
        # data_bytes must be bytes-like
        arr = np.frombuffer(data_bytes, dtype=np.uint8)
        # Pad or truncate
        if len(arr) > SLOT_SIZE:
            arr = arr[:SLOT_SIZE]

        return _write(self.buf_array, self.header_array, self.capacity, arr)

    def read(self):
        # Python wrapper for Numba read
        out = np.zeros(SLOT_SIZE, dtype=np.uint8)
        success = _read(self.buf_array, self.header_array, self.capacity, out)
        if success:
            return out.tobytes()
        return None

    def close(self):
        self.shm.close()

    def unlink(self):
        self.shm.unlink()
