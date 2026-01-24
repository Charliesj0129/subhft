import math
import os

import yaml


def main():
    # 1. Skip Login/Fetch (Broken in Shioaji Simulation currently)
    # api = sj.Shioaji(simulation=True)
    # api.login...

    print("Skipping Shioaji Fetch (IndexError detected). Using Synthetic List.")
    active_contracts = []

    # 2. Generate Synthetic Contracts
    # Categories: TXF (Large), MXF (Small), ZEF (Elect), ZFF (Fin)
    # Expiry: spot month, next month, next quarter...
    # For stress test, valid format matters more than real data availability.
    # Simulation might not have all, but we test subscription overhead.

    # Expand to reach ~1000 symbols
    # Products: 5
    # Years: 10 (2026-2035) -> 5 * 12 * 10 = 600
    # Weeklies (TXF, MXF): 2 * 12 * 4 * 10 = 960
    # Total ~1560

    products = ["TXF", "MXF", "ZEF", "ZFF", "GTF"]
    years = range(2026, 2036)
    months = ["01", "02", "03", "04", "05", "06", "07", "08", "09", "10", "11", "12"]

    for year in years:
        for prod in products:
            for m in months:
                # Regular
                code = f"{prod}{year}{m}"
                active_contracts.append({"code": code, "exchange": "FUT"})

                # Weeklies? TXF/MXF only
                if prod in ["TXF", "MXF"]:
                    for w in ["W1", "W2", "W4", "W5"]:
                        code_w = f"{prod}{year}{m}{w}"
                        active_contracts.append({"code": code_w, "exchange": "FUT"})

    # Limit to 1000 exactly to match plan target?
    # Or just let it shard.
    # Plan said "1000 active".
    if len(active_contracts) > 1000:
        print(f"Generated {len(active_contracts)}, capping to 1000 for precise stress test.")
        active_contracts = active_contracts[:1000]

    print(f"Generated {len(active_contracts)} synthetic contracts.")

    # 3. Shard (Max 200)
    SHARD_SIZE = 200
    num_shards = math.ceil(len(active_contracts) / SHARD_SIZE)

    output_dir = "config/stress"
    os.makedirs(output_dir, exist_ok=True)

    print(f"Splitting into {num_shards} shards...")

    for i in range(num_shards):
        start = i * SHARD_SIZE
        end = start + SHARD_SIZE
        chunk = active_contracts[start:end]

        filename = f"{output_dir}/shard_{i}.yaml"
        payload = {"symbols": chunk}

        with open(filename, "w") as f:
            yaml.dump(payload, f)

        print(f"Wrote {len(chunk)} symbols to {filename}")


if __name__ == "__main__":
    main()
