from multiprocessing import shared_memory
from typing import Optional

import numpy as np
from numba import njit

# Kill Switch Shared Memory
# 1 Byte Flag: 0 = OK, 1 = KILL
KILL_SWITCH_SHM_NAME = "hft_kill_switch"


@njit
def _check_order(price_scaled: int, qty: int, max_price_scaled: int, max_qty: int, kill_flag_ptr):
    """
    Core Risk Check Logic (Numba Compiled).
    Uses scaled integers for price comparisons to comply with Precision Law.
    Returns (Passed: bool, ErrorCode: int)
    ErrorCodes: 0=OK, 1=KILL_SWITCH, 2=BAD_PRICE_NEG, 3=BAD_PRICE_MAX, 4=BAD_QTY_MAX, 5=BAD_QTY_NEG
    """
    # 1. Kill Switch (Read directly from memory)
    # ptr is a numpy array view of uint8
    if kill_flag_ptr[0] > 0:
        return False, 1

    # 2. Price Sanity (using scaled integers)
    if price_scaled <= 0:
        return False, 2

    if price_scaled > max_price_scaled:
        return False, 3

    # 3. Qty Sanity (Fat Finger)
    if qty <= 0:
        return False, 5

    if qty > max_qty:
        return False, 4

    return True, 0


# Default price scale factor (1_000_000 = 6 decimal places)
DEFAULT_PRICE_SCALE = 1_000_000


class FastGate:
    def __init__(
        self,
        max_price: int = 10_000_000_000,
        max_qty: int = 100,
        create_shm: bool = False,
        price_scale: int = DEFAULT_PRICE_SCALE,
    ):
        """
        Initialize FastGate with scaled integer limits.

        Args:
            max_price: Maximum price as scaled integer (default: 10000 * 1_000_000 = 10B)
            max_qty: Maximum quantity as integer
            create_shm: Whether to create shared memory for kill switch
            price_scale: Price scale factor (default: 1_000_000)
        """
        self.max_price_scaled = int(max_price)
        self.max_qty = int(max_qty)
        self.price_scale = price_scale
        self.ks_shm: Optional[shared_memory.SharedMemory] = None
        self.ks_array: Optional[np.ndarray] = None

        # Setup Kill Switch SHM with proper cleanup on failure
        try:
            self._init_shared_memory(create_shm)
        except Exception:
            # Ensure cleanup if initialization fails partway through
            self._cleanup_shm()
            raise

        # JIT Warmup with scaled integer values
        _check_order(100_000_000, 1, self.max_price_scaled, self.max_qty, self.ks_array)

    def _init_shared_memory(self, create_shm: bool) -> None:
        """Initialize shared memory for kill switch with proper error handling."""
        try:
            if create_shm:
                self.ks_shm = shared_memory.SharedMemory(name=KILL_SWITCH_SHM_NAME, create=True, size=1)
                assert self.ks_shm is not None
                buf = self.ks_shm.buf
                if buf is None:
                    raise RuntimeError("Kill switch shared memory buffer not available")
                buf[0] = 0
            else:
                self.ks_shm = shared_memory.SharedMemory(name=KILL_SWITCH_SHM_NAME)
        except FileExistsError:
            self.ks_shm = shared_memory.SharedMemory(name=KILL_SWITCH_SHM_NAME)
        except FileNotFoundError:
            # Fallback if not created yet (e.g. unit test mode)
            if not create_shm:
                # Auto create for safety in dev
                self.ks_shm = shared_memory.SharedMemory(name=KILL_SWITCH_SHM_NAME, create=True, size=1)
                assert self.ks_shm is not None
                buf = self.ks_shm.buf
                if buf is None:
                    raise RuntimeError("Kill switch shared memory buffer not available")
                buf[0] = 0
            else:
                raise

        # Create numpy view for Numba
        if self.ks_shm is None:
            raise RuntimeError("Kill switch shared memory not initialized")
        self.ks_array = np.ndarray((1,), dtype=np.uint8, buffer=self.ks_shm.buf)

    def _cleanup_shm(self) -> None:
        """Clean up shared memory resources."""
        if self.ks_shm is not None:
            try:
                self.ks_shm.close()
            except Exception as e:
                # Log cleanup failures for debugging SHM issues
                import logging

                logging.getLogger("risk.fast_gate").debug("SHM cleanup failed: %s", e)
            self.ks_shm = None
        self.ks_array = None

    def check(self, price_scaled: int, qty: int) -> tuple[bool, int]:
        """
        Public API. Returns (passed, error_code).
        For HFT, returning boolean allows faster handling than Exception unwind.

        Args:
            price_scaled: Price as scaled integer (already multiplied by price_scale)
            qty: Quantity as integer

        Returns:
            Tuple of (passed: bool, error_code: int)
            ErrorCodes: 0=OK, 1=KILL_SWITCH, 2=BAD_PRICE_NEG, 3=BAD_PRICE_MAX, 4=BAD_QTY_MAX, 5=BAD_QTY_NEG
        """
        # We invoke Numba function with scaled integers
        if self.ks_array is None:
            raise RuntimeError("Kill switch shared memory not initialized")
        ok, code = _check_order(int(price_scaled), int(qty), self.max_price_scaled, self.max_qty, self.ks_array)
        return ok, code

    def set_kill_switch(self, active: bool):
        if self.ks_array is None:
            raise RuntimeError("Kill switch shared memory not initialized")
        self.ks_array[0] = 1 if active else 0

    def close(self):
        if self.ks_shm is not None:
            self.ks_shm.close()
            self.ks_shm = None
        self.ks_array = None

    def unlink(self):
        if self.ks_shm is not None:
            try:
                self.ks_shm.unlink()
            except FileNotFoundError:
                pass

    def __enter__(self):
        """Context manager entry."""
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        """Context manager exit - close shared memory."""
        self.close()
        return False

    def __del__(self):
        """Destructor - attempt to close shared memory if not already closed."""
        try:
            self.close()
        except Exception as e:
            # Log destructor failures for debugging (use standard logging to avoid structlog issues)
            import logging

            logging.getLogger("risk.fast_gate").debug("FastGate destructor error: %s", e)
