
import os
import glob
import subprocess
import pandas as pd

DATA_DIR = "research/data/hbt_multiproduct"
SIGNAL_DIR = "research/data/signals"

def run_sweep():
    # 1. Finds Data (NPY)
    files = glob.glob(os.path.join(DATA_DIR, "*.npy"))
    files = [f for f in files if "snapshot" not in f]
    
    print(f"Found {len(files)} products to backtest: {files}")
    
    results = []
    
    for data_file in files:
        symbol = os.path.basename(data_file).replace(".npy", "")
        print(f"\n--- Processing {symbol} ---")
        
        # 3. Run Strategies
        print(f"Running Retail Backtest for {symbol}...")
        
        cmd_hbt = f"python3 -c \"from research.tools.maker_strategy_hbt import run_backtest; run_backtest('dummy', '{data_file}')\""
        # STREAM OUTPUT (No Capture)
        ret_hbt = subprocess.run(cmd_hbt, shell=True, check=False)
        
        if ret_hbt.returncode != 0:
            print(f"Backtest failed for {symbol}")
            results.append({"Symbol": symbol, "Sharpe": -999, "Equity": 0})
        else:
            # Result is in CSV
            print(f"Backtest finished for {symbol}")
        
    # 4. Analyze from CSV
    res_path = 'research/data/sweep_results.csv'
    if os.path.exists(res_path):
        df = pd.read_csv(res_path)
    else:
        df = pd.DataFrame(results) # fallback (empty)
        
    print("\n=== Multi-Product Sweep Results ===")
    print(df)
    
    if df.empty:
        print("No results.")
        return
        
    # Check Success
    # Condition: All Positive Sharpe? Or Avg > 1.0?
    failures = df[df['Sharpe'] < 1.0]
    if failures.empty:
        print("\nSUCCESS: All products passed validation.")
        print(">> PROCEED TO RUST PORTING <<")
    else:
        print(f"\nFAILURE: {len(failures)} products failed validation.")
        print(">> PROCEED TO FACTOR FINDING <<")

if __name__ == "__main__":
    run_sweep()
