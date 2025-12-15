from typing import Callable, Any

def get_bbo(lob: dict):
    """Safe BBO extractor from LOB snapshot."""
    if not lob:
        return None, None, 0, 0
    
    bids = lob.get("bids", [])
    asks = lob.get("asks", [])
    
    if not bids or not asks:
        return None, None, 0, 0
        
    # Assuming LOB is sorted: Bids desc, Asks asc (standard)
    best_bid = bids[0][0]
    bid_vol = bids[0][1]
    
    best_ask = asks[0][0]
    ask_vol = asks[0][1]
    
    return best_bid, best_ask, bid_vol, ask_vol

def linear_adjustment(imbalance: float, spread: float) -> float:
    """
    Linear adjustment (equivalent to Volume-Weighted Mid Price).
    g(I) = (I - 0.5) * Spread
    """
    return (imbalance - 0.5) * spread

def stoikov_micro_price(lob: dict, adjustment_func: Callable[[float, float], float] = linear_adjustment) -> float:
    """
    Calculate Micro-Price based on Stoikov (2018) concept.
    P_micro = Mid + g(Imbalance)
    
    Args:
        lob: LOB Snapshot dict {"bids": [[p,v],..], "asks": [[p,v],..]}
        adjustment_func: Function g(I, S) returning price adjustment.
    
    Returns:
        float: Estimated Micro-Price
    """
    bid, ask, bid_vol, ask_vol = get_bbo(lob)
    
    if bid is None or ask is None:
        return 0.0
        
    mid = (bid + ask) / 2.0
    spread = ask - bid
    
    if bid_vol + ask_vol == 0:
        return mid
        
    # Imbalance I = V_b / (V_b + V_a)
    # Range [0, 1]. 1 means heavy buy pressure.
    imbalance = bid_vol / (bid_vol + ask_vol)
    
    adj = adjustment_func(imbalance, spread)
    
    return mid + adj
