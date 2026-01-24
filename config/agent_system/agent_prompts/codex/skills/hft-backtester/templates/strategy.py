from numba import njit
from hftbacktest import BUY, SELL, GTX, LIMIT

@njit
def strategy(hbt):
    # --- parameter definitions ---
    asset_no = 0
    tick_size = hbt.depth(asset_no).tick_size
    lot_size = hbt.depth(asset_no).lot_size
    
    # Check interval in nanoseconds (e.g. 10ms)
    check_interval = 10_000_000 
    
    while hbt.elapse(check_interval) == 0:
        # --- Housekeeping ---
        hbt.clear_inactive_orders(asset_no)
        
        # --- Market Data ---
        depth = hbt.depth(asset_no)
        mid_price = (depth.best_bid + depth.best_ask) / 2.0
        
        # --- Alpha / Signal Calculation ---
        # TODO: Implement your signal here
        
        # --- Order Logic ---
        # TODO: Implement order submission logic
        # Example:
        # new_bid = mid_price - tick_size * 5
        # hbt.submit_buy_order(asset_no, 1001, new_bid, lot_size, GTX, LIMIT, False)
        
        # --- Wait for responses (optional but consistent) ---
        # This part depends on if you use wait_order_response or simple elapse
        # hbt.wait_order_response(asset_no, order_id, timeout)
        
    return True
