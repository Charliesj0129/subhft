#!/usr/bin/env python3
"""
Batch create initial snapshots for all HBT NPZ files.
Iterates over research/data/real_data_reconstructed and generates *_snapshot.npz
"""
import os
import glob
import sys
from hftbacktest.data.utils import snapshot

DATA_DIR = "research/data/hbt_txfb6"

def main():
    files = sorted(glob.glob(os.path.join(DATA_DIR, "dataset_*.npz")))
    print(f"Found {len(files)} dataset files.")
    
    for f in files:
        if "_snapshot.npz" in f:
            continue
            
        snapshot_file = f.replace(".npz", "_snapshot.npz")
        if os.path.exists(snapshot_file):
            print(f"Skipping existing snapshot: {os.path.basename(snapshot_file)}")
            continue
            
        print(f"Creating snapshot for {os.path.basename(f)}...")
        try:
            # hftbacktest 1.6+ usage of create_last_snapshot
            # It reads the file and takes the FIRST valid snapshot (or last of previous day?)
            # The tool documentation says: "create_last_snapshot(input, output, tick_size, lot_size)" 
            # But earlier error said missing 'lot_size'.
            # Let's try passing tick/lot.
            # TXFB6: tick=1.0, lot=1.0 (or 0.001?)
            
            # Correction: The logic I simpler. I need to take the *first* event and assume it sets the book?
            # Or assume the file starts empty?
            # If standard NPZ, hftbacktest needs an initial state.
            
            # Using the library function (which failed before, I'll use my manual fallback logic if needed)
            # Step 3383 showed: "TypeError: create_last_snapshot() missing 1 required positional argument: 'lot_size'"
            # So I must provide tick_size and lot_size.
            
            # Correct signature detected: create_last_snapshot(data: List[str], tick_size: float, lot_size: float, initial_snapshot=None, output_snapshot_filename=str)
            snapshot.create_last_snapshot([f], 1.0, 1.0, output_snapshot_filename=snapshot_file)
            print(f"  Success: {os.path.basename(snapshot_file)}")
            
        except Exception as e:
            print(f"  Error creating snapshot: {e}")
            # Fallback: Manual creation (minimal)
            try:
                # Based on Step 3383 workaround
                # Just create a dummy snapshot with empty arrays? 
                # Or wait, the snapshot is needed for *LOB state*.
                # If I can't construct it, I can't start.
                pass
            except:
                pass

if __name__ == "__main__":
    main()
