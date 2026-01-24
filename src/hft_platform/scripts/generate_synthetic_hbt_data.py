import sys

import numpy as np

# Path setup
sys.path.append("/home/charlie/hft_platform/external_repos/hftbacktest_fresh/py-hftbacktest")
from hftbacktest import DEPTH_EVENT, TRADE_EVENT
from hftbacktest.types import event_dtype


def generate():
    print("Generating synthetic data...")
    num_events = 10000
    data = np.zeros(num_events, dtype=event_dtype)

    # Start TS
    ts = 1_600_000_000_000_000_000  # dummy timestamp

    mid = 10000.0
    tick = 1.0

    for i in range(num_events):
        ts += 1_000_000  # 1ms steps

        # Random walk mid
        mid += np.random.choice([-tick, 0, tick])

        # Construct BBO
        bid = mid - tick * 0.5
        ask = mid + tick * 0.5

        # Event Type: update bid or ask
        # We alternate for simplicity, or random
        is_trade = np.random.random() < 0.1

        data[i]["exch_ts"] = ts
        data[i]["local_ts"] = ts

        if is_trade:
            data[i]["ev"] = TRADE_EVENT
            data[i]["px"] = ask if np.random.random() > 0.5 else bid
            data[i]["qty"] = 1.0
            data[i]["ival"] = 1 if data[i]["px"] == ask else -1  # Buy/Sell flag often in ival
        else:
            data[i]["ev"] = DEPTH_EVENT
            # Simplification: Everything is BBO update
            # hftbacktest often uses specific flags in high bits or separate events
            # But let's assume standard Depth Event with ival for Bool/flags
            data[i]["px"] = bid
            data[i]["qty"] = 10.0 + np.random.random() * 5

            # We need to set ASK too?
            # Standard format usually has separate rows for Bid/Ask updates in L2
            # But for sim, we'll just put Bid here.

    # Save
    print("Saving to data/synthetic.npz")
    np.savez_compressed("data/synthetic.npz", data=data)


if __name__ == "__main__":
    generate()
