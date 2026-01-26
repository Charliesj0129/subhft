import pandas as pd
import numpy as np

df = pd.read_csv("../reports/regime_analysis.csv")

metrics = ["volatility", "avg_spread", "trade_count"]
target = "sharpe_HighFreqRSI"

print("Conditional Mean Sharpe for HighFreqRSI (Top/Bottom 20% Regimes):")
print("-" * 60)

for m in metrics:
    # Quintiles
    q_low = df[m].quantile(0.2)
    q_high = df[m].quantile(0.8)
    
    mean_low = df[df[m] <= q_low][target].mean()
    mean_high = df[df[m] >= q_high][target].mean()
    
    print(f"{m:<15} | Low (<{q_low:.4f}): {mean_low:>6.2f} | High (>{q_high:.4f}): {mean_high:>6.2f} | Diff: {mean_high-mean_low:.2f}")

print("-" * 60)
print("Overall Mean:", df[target].mean())
print("Median:", df[target].median())
