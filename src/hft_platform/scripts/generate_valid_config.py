
import json
import math
import os

import yaml


def main():
    with open("valid_symbols.json", "r") as f:
        contracts = json.load(f)

    print(f"Loaded {len(contracts)} valid contracts.")

    # Format for config
    symbols = []
    for c in contracts:
        symbols.append({
            "code": c["code"],      # Use the REAL internal code (e.g. TXFA6)
            "exchange": "FUT"       # c["exchange"] is 'FUT'
        })

    # Shard
    SHARD_SIZE = 200
    num_shards = math.ceil(len(symbols) / SHARD_SIZE)
    if num_shards == 0:
        num_shards = 1

    output_dir = "config/stress"
    os.makedirs(output_dir, exist_ok=True)

    for i in range(num_shards):
        start = i * SHARD_SIZE
        end = start + SHARD_SIZE
        chunk = symbols[start:end]

        filename = f"{output_dir}/shard_{i}.yaml"
        payload = {"symbols": chunk}

        with open(filename, "w") as f:
            yaml.dump(payload, f)

        print(f"Wrote {len(chunk)} symbols to {filename}")

if __name__ == "__main__":
    main()
