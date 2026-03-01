
import numpy as np
import os

DIR = 'research/data/hbt_multiproduct/'
FILE_A = os.path.join(DIR, 'TXFB6.npy')
FILE_B = os.path.join(DIR, '2330.npy')

def check_sync():
    print(f"Checking {FILE_A} vs {FILE_B}...")
    
    if not os.path.exists(FILE_A) or not os.path.exists(FILE_B):
        print("Files missing.")
        return

    # Load only timestamps? 
    # Mmap to save memory
    a = np.load(FILE_A, mmap_mode='r')
    b = np.load(FILE_B, mmap_mode='r')
    
    ts_a = a['exch_ts']
    ts_b = b['exch_ts']
    
    print(f"TXFB6 Events: {len(ts_a)}, Range: {ts_a[0]} - {ts_a[-1]}")
    print(f"2330  Events: {len(ts_b)}, Range: {ts_b[0]} - {ts_b[-1]}")
    
    # Intersection
    start = max(ts_a[0], ts_b[0])
    end = min(ts_a[-1], ts_b[-1])
    
    if end < start:
        print("NO OVERLAP!")
    else:
        duration_sec = (end - start) / 1e9
        print(f"Overlap Duration: {duration_sec/3600:.2f} hours")
        
        # Check density in overlap
        # Validating that we have enough events in 2330 during the intersection
        
        # Search indices
        idx_a0 = np.searchsorted(ts_a, start)
        idx_a1 = np.searchsorted(ts_a, end)
        
        idx_b0 = np.searchsorted(ts_b, start)
        idx_b1 = np.searchsorted(ts_b, end)
        
        print(f"Counts in Overlap: TXFB6={idx_a1-idx_a0}, 2330={idx_b1-idx_b0}")
        
if __name__ == '__main__':
    check_sync()
