
import numpy as np

def inspect():
    path = "research/data/hbt_multiproduct/TXFB6_features_v3.npy"
    print(f"Loading {path}...")
    data = np.load(path)
    print(f"Shape: {data.shape}")
    
    if np.isnan(data).any():
        print("ALERT: NaNs found!")
        print(f"NaN Count: {np.isnan(data).sum()}")
        # Check which column
        for i in range(data.shape[1]):
            col = data[:, i]
            if np.isnan(col).any():
                print(f"Col {i} has {np.isnan(col).sum()} NaNs")
    else:
        print("No NaNs.")
        
    if np.isinf(data).any():
        print("ALERT: Infs found!")
        for i in range(data.shape[1]):
            col = data[:, i]
            if np.isinf(col).any():
                 print(f"Col {i} has {np.isinf(col).sum()} Infs")
    else:
        print("No Infs.")
        
    print("Stats:")
    # Min, Max, Mean per col
    for i in range(data.shape[1]):
        col = data[:, i]
        print(f"Col {i}: Min={np.min(col):.4f}, Max={np.max(col):.4f}, Mean={np.mean(col):.4f}")

if __name__ == "__main__":
    inspect()
