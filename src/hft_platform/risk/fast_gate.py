from multiprocessing import shared_memory

import numpy as np
from numba import njit

# Kill Switch Shared Memory
# 1 Byte Flag: 0 = OK, 1 = KILL
KILL_SWITCH_SHM_NAME = "hft_kill_switch"


@njit
def _check_order(price, qty, max_price, max_qty, kill_flag_ptr):
    """
    Core Risk Check Logic (Numba Compiled).
    Returns (Passed: bool, ErrorCode: int)
    ErrorCodes: 0=OK, 1=KILL_SWITCH, 2=BAD_PRICE_NEG, 3=BAD_PRICE_MAX, 4=BAD_QTY_MAX, 5=BAD_QTY_NEG
    """
    # 1. Kill Switch (Read directly from memory)
    # ptr is a numpy array view of uint8
    if kill_flag_ptr[0] > 0:
        return False, 1

    # 2. Price Sanity
    if price <= 0:
        return False, 2

    if price > max_price:
        return False, 3

    # 3. Qty Sanity (Fat Finger)
    if qty <= 0:
        return False, 5

    if qty > max_qty:
        return False, 4

    return True, 0


class FastGate:
    def __init__(self, max_price=10000.0, max_qty=100.0, create_shm=False):
        self.max_price = float(max_price)
        self.max_qty = float(max_qty)

        # Setup Kill Switch SHM
        try:
            if create_shm:
                self.ks_shm = shared_memory.SharedMemory(name=KILL_SWITCH_SHM_NAME, create=True, size=1)
                self.ks_shm.buf[0] = 0
            else:
                self.ks_shm = shared_memory.SharedMemory(name=KILL_SWITCH_SHM_NAME)
        except FileExistsError:
            self.ks_shm = shared_memory.SharedMemory(name=KILL_SWITCH_SHM_NAME)
        except FileNotFoundError:
            # Fallback if not created yet (e.g. unit test mode)
            if not create_shm:
                # Auto create for safety in dev
                self.ks_shm = shared_memory.SharedMemory(name=KILL_SWITCH_SHM_NAME, create=True, size=1)
                self.ks_shm.buf[0] = 0

        # Create numpy view for Numba
        self.ks_array = np.ndarray((1,), dtype=np.uint8, buffer=self.ks_shm.buf)

        # JIT Warmup
        _check_order(100.0, 1.0, self.max_price, self.max_qty, self.ks_array)

    def check(self, price, qty):
        """
        Public API. Returns True if accepted, raises RiskException if failed (or returns False).
        For HFT, returning boolean allows faster handling than Exception unwind.
        """
        # We invoke Numba function
        ok, code = _check_order(price, qty, self.max_price, self.max_qty, self.ks_array)
        return ok, code

    def set_kill_switch(self, active: bool):
        self.ks_array[0] = 1 if active else 0

    def close(self):
        self.ks_shm.close()

    def unlink(self):
        try:
            self.ks_shm.unlink()
        except FileNotFoundError:
            pass
