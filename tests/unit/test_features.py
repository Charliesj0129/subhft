
import pytest
from hft_platform.features.micro_price import stoikov_micro_price
from hft_platform.features.ofi import OFICalculator
from hft_platform.features.liquidity import ShadowQueueEstimator, RollingKyleLambda
from hft_platform.strategy.factors import micro_price

def test_stoikov_micro_price():
    # Bid 100x1, Ask 102x9. Mid 101. Spread 2.
    # Imbalance = 1 / 10 = 0.1
    # Linear Adj = (0.1 - 0.5) * 2 = -0.4 * 2 = -0.8
    # Micro = 101 - 0.8 = 100.2
    
    lob = {"bids": [[100, 1]], "asks": [[102, 9]]}
    mp = stoikov_micro_price(lob)
    assert abs(mp - 100.2) < 0.0001
    
    # Test normalization wrapper
    lob_dict = {"bids": [{"price": 100, "volume": 1}], "asks": [{"price": 102, "volume": 9}]}
    mp_dict = micro_price(lob_dict)
    assert abs(mp_dict - 100.2) < 0.0001

def test_ofi_calculator():
    ofi = OFICalculator(depth=1)
    
    # T0: Bid 100x10
    lob0 = {"bids": [[100, 10]], "asks": [[102, 10]]}
    val = ofi.update(lob0)
    assert val == 0.0 # First update no delta
    
    # T1: Bid 101x5 (Price Improvement -> Aggressive Buy)
    # OFI = q_n = 5
    lob1 = {"bids": [[101, 5]], "asks": [[102, 10]]}
    val = ofi.update(lob1)
    assert val == 5.0
    
    # T2: Bid 101x2 (Vol decrease at same price -> Selling or Cancel)
    # OFI = 2 - 5 = -3
    lob2 = {"bids": [[101, 2]], "asks": [[102, 10]]}
    val = ofi.update(lob2)
    assert val == -3.0
    
    # T3: Ask 101x5 (Ask Improvement -> Aggressive Sell)
    # Ask Flow: P_curr < P_prev -> q_curr = 5
    # Net OFI = BidFlow - AskFlow
    # Bid unchanged (Assume same) -> Flow 0
    # Net = 0 - 5 = -5
    lob3 = {"bids": [[101, 2]], "asks": [[101, 5]]} # Crossed book?
    val = ofi.update(lob3)
    assert val == -5.0

def test_shadow_queue():
    sq = ShadowQueueEstimator()
    lob = {"bids": [[100, 50], [99, 100]], "asks": []}
    
    # Join at Best Bid (Level 0)
    # Ahead: 50
    pos = sq.estimate(lob, "buy", 0)
    assert pos == 50
    
    # Join at Level 1
    # Ahead: 100 (Volume at that level)
    # Note: Report says Shadow Queue usually implies volume AHEAD of you at that price.
    # If you join, you are behind current volume.
    pos1 = sq.estimate(lob, "buy", 1)
    assert pos1 == 100
    
def test_kyle_lambda():
    kl = RollingKyleLambda(window_size=10)
    
    # Needs at least 10 points to calc
    for i in range(1, 15):
        # Flow = 10*i, Price = i. Slope = 0.1
        kl.update(float(i), float(i*10))
        
    slope = kl.update(15.0, 150.0)
    # 1/10 = 0.1
    assert abs(slope - 0.1) < 0.0001
