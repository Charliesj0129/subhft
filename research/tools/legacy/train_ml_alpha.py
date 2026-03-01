
import numpy as np
import os
import torch
import torch.nn as nn
import torch.optim as optim
from torch.utils.data import Dataset, DataLoader
from sklearn.preprocessing import StandardScaler
from sklearn.metrics import classification_report, accuracy_score

# Configuration
DIR = 'research/data/hbt_multiproduct/'
FILE_TARGET = os.path.join(DIR, 'TXFB6.npy')

class LOBDataset(Dataset):
    def __init__(self, data_path, window=10, horizon=1000): # horizon in "ticks" or "time"?
        # Load Raw Data
        print(f"Loading {data_path}...")
        data = np.load(data_path, mmap_mode='r')
        
        # Filter LOB Updates (ev=1) and Trades (ev=2)
        # For simplicity, let's just use LOB snapshots.
        # But HBT format is incremental. This is hard for simple DL without reconstruction.
        
        # Alternative: Use `process_parquet` output if it had Snapshots.
        # But `TXFB6.npy` is HBT format (Event, Price, Qty, Enum).
        # Reconstruction is needed to get "State" (Features).
        
        # To avoid re-writing an engine here, we will hack:
        # Use Trade Events (ev=2) as "Keyframes" since they have Price info.
        # Features: Recent Trade Prices, Flows, Intervals.
        # This is "Sequence of Trades" model.
        
        # Better: Reconstruct BBO.
        # Or... Just use `hftbacktest` to generate features? 
        # Writing a pure Python reconstructor is slow.
        
        # Let's try to extract BBO directly from event stream for just Top 1 Level.
        # HBT: ev=1 (LOB), ev=2 (Trade). 
        # ev=1: px=Price, qty=NewQty, ival=Side?
        # If we just scan, we can maintain BBO.
        
        # Optimization: Just load 1M events for Proof of Concept.
        raw = data[:1000000] # 1M events
        
        # Reconstruct BBO
        # Simple loop (Numba would be better but let's try vectorization if possible, else loop).
        
        # Actually, let's use the Python `hftbacktest` or similar if available?
        # Or simple Python loop. 1M is okay.
        
        self.features = []
        self.targets = []
        
        best_bid = 0.0
        best_ask = 0.0
        mid_prices = []
        timestamps = []
        
        # We need to collect state at every Nth event to form a dataset?
        # Or every second?
        
        print("Preprocessing Features...")
        
        # Vectorized is hard due to state dependence.
        # Let's use a simplified approach: 
        # Just use Trades?
        # Trades have no Depth info.
        
        # Let's scan.
        ev = raw['ev']
        px = raw['px']
        qty = raw['qty']
        ts = raw['exch_ts']
        
        # To speed up, we only sample every 100 events
        sample_rate = 100
        
        # We need a robust "Mid Price" series.
        # Let's take 'px' where ev=2 (Trade) as ground truth price?
        # Or BBO?
        
        # Iterate
        # Current BBO
        bids = {}
        asks = {}
        
        # Feature Collection
        X_list = []
        Y_list = []
        
        # Horizon: 10s (1e10 ns)
        # We need to align X(t) with Y(t+10s).
        
        # First pass: Build Price Series (Regularly sampled? or Event based?)
        # Let's sample every 1s.
        
        start_t = ts[0]
        end_t = ts[-1]
        grid = np.arange(start_t, end_t, 1_000_000_000) # 1s grid
        
        # Find indices
        indices = np.searchsorted(ts, grid)
        
        # Now we need "State" at these indices.
        # We can just process sequentially and capture state at grid points.
        
        grid_idx = 0
        next_grid_t = grid[0]
        
        current_bids = {}
        current_asks = {}
        
        captured_mids = []
        captured_imb = []
        
        # Loop mainly for LOB maintenance
        # This might be slow in pure Python for 1M events. 
        # But let's try.
        
        for i in range(len(ev)):
            e = ev[i]
            p = px[i]
            q = qty[i]
            # ival?
            
            # Update Book
            if e == 1:
                # ival: 1=Bid, -1=Ask? (Need to check)
                # Let's verify standard HBT
                # Usually: 1=Ask, -1=Bid in HBT?
                # Actually, HBT usually uses Side Enum. 
                # Let's check `process_parquet.py` to be sure.
                # Assuming standard: 1=Bid, -1=Ask?
                # Let's guess: Side 1 is Ask, Side -1 is Bid (HFTBacktest standard).
                
                # Check previous logs?
                # If we assume standard:
                # Wait, HFTBacktest uses: 1=Bid, -1=Ask?
                # Actually let's just use "Price" changes.
                pass 
                
            # Skip full LOB filtering for now if too slow.
            # FAST PATH:
            # Use Trades `ev=2` and `px` as Mid proxy.
            # Volume Imbalance over last 10s.
            
            pass
            
        # FAST PATH IMPLEMENTATION
        # We interpret 'px' from Trades as the price signal.
        mask_trade = (ev == 2)
        trade_px = px[mask_trade]
        trade_ts = ts[mask_trade]
        trade_qty = qty[mask_trade]
        trade_side = raw['ival'][mask_trade] # Extract Side
        
        # Resample to 1s
        # Searchsorted
        idx = np.searchsorted(trade_ts, grid)
        idx = np.clip(idx, 0, len(trade_px)-1)
        
        grid_px = trade_px[idx]
        
        # Features:
        # 1. Log Return (1s, 5s, 10s)
        # 2. Volatility (StdDev of last 10s)
        # 3. Volume Intensity
        # 4. Trade Flow Imbalance
        
        df_len = len(grid)
        if df_len < 1000:
            print("Not enough data.")
            return

        # Feature Construction
        # Ret-1
        ret_1 = np.diff(np.log(grid_px), prepend=grid_px[0])
        
        # Additional Features Calculation (Windowed)
        # For each grid point t, look back 10s
        volatility = []
        flow_imbalance = []
        intensity = []
        
        print("Computing Windowed Features...")
        idx_grid = np.searchsorted(trade_ts, grid)
        
        # 10s window in nanoseconds
        WINDOW_NS = 10_000_000_000
        idx_grid_minus_10s = np.searchsorted(trade_ts, grid - WINDOW_NS)
        
        # Vectorized aggregate is tricky with variable length windows.
        # But for 10k points, loop is fine.
        for i in range(df_len):
            start = idx_grid_minus_10s[i]
            end = idx_grid[i]
            
            if start >= end:
                volatility.append(0)
                flow_imbalance.append(0)
                intensity.append(0)
                continue
                
            # Slice
            window_px = trade_px[start:end]
            window_qty = trade_qty[start:end]
            window_side = trade_side[start:end]
            
            # Volatility (Std of Price)
            if len(window_px) > 1:
                vol = np.std(window_px)
            else:
                vol = 0
            volatility.append(vol)
            
            # Flow: Sum(Side * Qty)
            flow = np.sum(window_side * window_qty)
            flow_imbalance.append(flow)
            
            # Intensity: Sum(Qty)
            total_vol = np.sum(window_qty)
            intensity.append(total_vol)
            
        # Stack Features
        # Normalize?
        ret_1 = np.nan_to_num(ret_1)
        volatility = np.array(volatility)
        flow_imbalance = np.array(flow_imbalance)
        intensity = np.array(intensity)
        
        # Simple Logic check: Avoid nan
        volatility = np.nan_to_num(volatility)
        flow_imbalance = np.nan_to_num(flow_imbalance)
        intensity = np.nan_to_num(intensity)
        
        # Main Feature Matrix X
        # [Ret, Vol, Flow, Intensity]
        # We also lag these vectors.
        
        base_features = np.stack([ret_1, volatility, flow_imbalance, intensity], axis=1)
        
        # Standard Scaler manually (or use sklearn)
        scaler = StandardScaler()
        base_features = scaler.fit_transform(base_features)
        
        # Create Lagged Sequences
        window_size = 10
        features = []
        for i in range(window_size):
            features.append(np.roll(base_features, i, axis=0))
            
        # Shape: (N, WindowSize, NumFeatures) -> Flatten to (N, WindowSize * NumFeatures)
        # Or keep as (N, 40)
        X = np.concatenate(features, axis=1) # (N, 40)
        
        # Target: Return over NEXT 10s
        # Y_raw = (Price(t+10) - Price(t)) / Price(t)
        Y_raw = np.roll(grid_px, -10) - grid_px
        Y_raw = Y_raw / grid_px
        
        # Classify
        # Up (> 1bps), Down (< -1bps), Flat
        thresh = 0.0001
        Y = np.zeros(len(Y_raw))
        Y[Y_raw > thresh] = 1
        Y[Y_raw < -thresh] = 2 # Class 2 = Down
        
        # Trim valid
        valid_n = df_len - 20
        self.X = torch.FloatTensor(X[window_size:valid_n])
        self.Y = torch.LongTensor(Y[window_size:valid_n])
         
        print(f"Dataset Size: {len(self.X)}")
        print(f"Input Features: {self.X.shape[1]}")
        print(f"Class Distribution: {np.bincount(self.Y.numpy().astype(int))}")

    def __len__(self):
        return len(self.X)

    def __getitem__(self, idx):
        return self.X[idx], self.Y[idx]

class AlphaNet(nn.Module):
    def __init__(self, input_size=40, hidden_size=64, num_classes=3):
        super(AlphaNet, self).__init__()
        self.fc1 = nn.Linear(input_size, hidden_size)
        self.relu = nn.ReLU()
        self.fc2 = nn.Linear(hidden_size, hidden_size//2)
        self.fc3 = nn.Linear(hidden_size//2, num_classes)
        
    def forward(self, x):
        out = self.fc1(x)
        out = self.relu(out)
        out = self.fc2(out)
        out = self.relu(out)
        out = self.fc3(out)
        return out

def train():
    device = torch.device('cuda' if torch.cuda.is_available() else 'cpu')
    print(f"Using Device: {device}")
    
    dataset = LOBDataset(FILE_TARGET)
    
    train_size = int(0.8 * len(dataset))
    test_size = len(dataset) - train_size
    train_dataset, test_dataset = torch.utils.data.random_split(dataset, [train_size, test_size])
    
    # Compute Class Weights
    class_counts = np.bincount(dataset.Y.numpy().astype(int))
    total_samples = len(dataset.Y)
    class_weights = torch.FloatTensor([total_samples / c for c in class_counts]).to(device)
    print(f"Class Weights: {class_weights}")
    
    train_loader = DataLoader(train_dataset, batch_size=64, shuffle=True)
    test_loader = DataLoader(test_dataset, batch_size=64, shuffle=False)
    
    model = AlphaNet().to(device)
    criterion = nn.CrossEntropyLoss(weight=class_weights)
    optimizer = optim.Adam(model.parameters(), lr=0.001)
    
    epochs = 10 # Increase epochs
    for epoch in range(epochs):
        model.train()
        running_loss = 0.0
        for i, (inputs, labels) in enumerate(train_loader):
            inputs, labels = inputs.to(device), labels.to(device)
            
            optimizer.zero_grad()
            outputs = model(inputs)
            loss = criterion(outputs, labels)
            loss.backward()
            optimizer.step()
            
            running_loss += loss.item()
            
        print(f"Epoch {epoch+1}, Loss: {running_loss/len(train_loader):.4f}")
        
    # Validation
    model.eval()
    all_preds = []
    all_labels = []
    with torch.no_grad():
        for inputs, labels in test_loader:
            inputs, labels = inputs.to(device), labels.to(device)
            outputs = model(inputs)
            _, predicted = torch.max(outputs.data, 1)
            all_preds.extend(predicted.cpu().numpy())
            all_labels.extend(labels.cpu().numpy())
            
    print("\n--- Validation Results ---")
    print(classification_report(all_labels, all_preds, zero_division=0))
    print(f"Accuracy: {accuracy_score(all_labels, all_preds):.4f}")

if __name__ == '__main__':
    train()
