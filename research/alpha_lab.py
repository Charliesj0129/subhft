

import matplotlib.pyplot as plt
import numpy as np
import pandas as pd

# Optional: Try to import statsmodels for detailed regression summary
try:
    import statsmodels.api as sm
    HAS_STATSMODELS = True
except ImportError:
    HAS_STATSMODELS = False

class MicrostructureSimulator:
    """
    Generates realistic HFT data with:
    1. Mean Reverting spread
    2. Price Impact from Order Flow (OFI)
    3. Trade clustering
    """
    def __init__(self, n_events=20000, price=1000.0, tick_size=0.5):
        self.n = n_events
        self.price = price
        self.tick = tick_size

        # Model Params
        self.lambda_ofi = 0.05 # Price impact coeff
        self.noise_vol = 0.1   # Fundamental volatility

    def generate(self) -> pd.DataFrame:
        print(f"Generating {self.n} events with Microstructure Impact Model...")
        np.random.seed(42)

        # 1. Generate Order Flow (OFI) process (AR(1) for autocorrelation)
        ofi = np.zeros(self.n)
        for i in range(1, self.n):
            ofi[i] = 0.3 * ofi[i-1] + np.random.normal(0, 10) # Persistency

        # 2. Generate True Mid Price (Impacted by OFI)
        # Price_t = Price_{t-1} + Impact * OFI_t + Noise
        true_mid = np.zeros(self.n)
        true_mid[0] = self.price

        noise = np.random.normal(0, self.noise_vol, self.n)

        for i in range(1, self.n):
            change = self.lambda_ofi * ofi[i] + noise[i]
            true_mid[i] = true_mid[i-1] + change

        # 3. Construct LOB (Bid/Ask) around True Mid
        # Spread centers on true mid but snaps to tick
        # We simulate "liquidity offering" responding to mid

        bids = np.floor((true_mid - 0.5 * self.tick) / self.tick) * self.tick
        asks = np.ceil((true_mid + 0.5 * self.tick) / self.tick) * self.tick

        # Ensure non-crossed
        cross_mask = bids >= asks
        bids[cross_mask] = asks[cross_mask] - self.tick

        # 4. Generate Depths (Correlated with OFI)
        # OFI > 0 implies Bid Adding / Ask Removing
        # We'll simplisticly set depths
        base_depth = 20
        bid_depth = np.abs(base_depth + 0.5 * ofi + np.random.normal(0, 5, self.n)).astype(int)
        ask_depth = np.abs(base_depth - 0.5 * ofi + np.random.normal(0, 5, self.n)).astype(int)

        bid_depth = np.maximum(1, bid_depth)
        ask_depth = np.maximum(1, ask_depth)

        df = pd.DataFrame({
            "mid": (bids + asks) / 2,
            "bid_p": bids,
            "ask_p": asks,
            "bid_v": bid_depth,
            "ask_v": ask_depth,
            "true_ofi": ofi # Ground truth for validation
        })

        return df

class AlphaEngine:
    @staticmethod
    def compute_ofi(df: pd.DataFrame) -> pd.Series:
        """
        Cont et al. (2014) OFI
        """
        prev_bid_p = df["bid_p"].shift(1)
        prev_ask_p = df["ask_p"].shift(1)
        prev_bid_v = df["bid_v"].shift(1)
        prev_ask_v = df["ask_v"].shift(1)

        # Bid Flow
        bid_flow = pd.Series(0.0, index=df.index)
        bid_flow[df["bid_p"] > prev_bid_p] = df["bid_v"]
        bid_flow[df["bid_p"] < prev_bid_p] = -prev_bid_v
        bid_flow[df["bid_p"] == prev_bid_p] = df["bid_v"] - prev_bid_v

        # Ask Flow
        ask_flow = pd.Series(0.0, index=df.index)
        ask_flow[df["ask_p"] < prev_ask_p] = df["ask_v"] # Improved price -> Add
        ask_flow[df["ask_p"] > prev_ask_p] = -prev_ask_v # Worsened -> Remove
        ask_flow[df["ask_p"] == prev_ask_p] = df["ask_v"] - prev_ask_v

        return bid_flow - ask_flow

    @staticmethod
    def compute_obi(df: pd.DataFrame) -> pd.Series:
        """
        Order Book Imbalance (Static)
        (BidVol - AskVol) / (BidVol + AskVol)
        """
        total = df["bid_v"] + df["ask_v"]
        obi = (df["bid_v"] - df["ask_v"]) / total
        return obi.fillna(0)

def run_lab():
    print("\n" + "="*50)
    print(" 🔬 HFT Alpha Research Lab v2.0")
    print("="*50)

    # 1. Simulate Microstructure
    sim = MicrostructureSimulator(n_events=10000, tick_size=1.0)
    df = sim.generate()

    # 2. Compute Alphas
    print("\n[Feature Engineering]")
    df["OFI"] = AlphaEngine.compute_ofi(df)
    df["OBI"] = AlphaEngine.compute_obi(df)

    # Simple rolling standard deviation as volatility proxy
    df["Vol"] = df["mid"].pct_change().rolling(20).std()

    # 3. Define Targets (Future Returns)
    horizons = [1, 5, 20]

    print("\n[Regression Analysis]")
    print(f"{'Horizon':<10} | {'Factor':<10} | {'Correlation':<12} | {'Beta':<10} | {'T-Stat':<10}")
    print("-" * 65)

    clean_df = df.dropna()

    for h in horizons:
        # Target: Price change h ticks ahead
        target = clean_df["mid"].shift(-h) - clean_df["mid"]
        valid = ~target.isna()

        y = target[valid]

        for factor in ["OFI", "OBI"]:
            x = clean_df[factor][valid]

            # OLS
            if HAS_STATSMODELS:
                X_add = sm.add_constant(x)
                model = sm.OLS(y, X_add).fit()
                beta = model.params[factor]
                t_stat = model.tvalues[factor]
                r2 = model.rsquared
                corr = np.sqrt(r2) if beta > 0 else -np.sqrt(r2)
            else:
                # Numpy manual fallback
                cov = np.cov(x, y)[0, 1]
                var_x = np.var(x)
                beta = cov / var_x if var_x > 0 else 0
                corr = np.corrcoef(x, y)[0, 1]
                t_stat = 0.0 # Naive

            print(f"{h:<10} | {factor:<10} | {corr:12.4f} | {beta:10.4f} | {t_stat:10.2f}")

    # 4. Visualization
    try:
        print("\n[Visualization]")
        plt.figure(figsize=(10, 6))

        # Scatter of OFI vs 5-tick Return
        h = 5
        target = clean_df["mid"].shift(-h) - clean_df["mid"]
        valid = ~target.isna()

        plt.scatter(clean_df["OFI"][valid], target[valid], alpha=0.1, s=1, label="Data")

        # Regression Line
        # y = mx + c
        m, c = np.polyfit(clean_df["OFI"][valid], target[valid], 1)
        x_range = np.linspace(clean_df["OFI"].min(), clean_df["OFI"].max(), 100)
        plt.plot(x_range, m*x_range + c, color="red", label=f"OLS (Beta={m:.4f})")

        plt.title(f"OFI Alpha Signal (Horizon={h} ticks)")
        plt.xlabel("Order Flow Imbalance (OFI)")
        plt.ylabel(f"Future Price Change ({h} ticks)")
        plt.legend()
        plt.grid(True, alpha=0.3)

        out_path = "research/alpha_analysis.png"
        plt.savefig(out_path)
        print(f"📈 Saved regression plot to {out_path}")

    except Exception as e:
        print(f"Skipping plot: {e}")

if __name__ == "__main__":
    run_lab()
