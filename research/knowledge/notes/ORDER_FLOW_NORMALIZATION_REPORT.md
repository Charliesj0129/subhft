# Research Report: Order Flow Normalization Techniques

**Date**: 2026-02-12
**Objective**: Synthesize recent findings on Order Flow Normalization to refine Alpha Signal Extraction.
**Key Papers Analyzed**:

1.  _Optimal Signal Extraction from Order Flow_ (Kang, 2025)
2.  _Directional Liquidity and Geometric Shear_ (da Cruz, 2026)
3.  _A Unified Theory of Order Flow_ (Muhle-Karbe et al., 2026)
4.  _Temporal Kolmogorov-Arnold Networks (T-KAN)_ (Makinde, 2026)

---

## 1. The Core Debate: Market Cap vs. Volume Normalization

The most immediate and actionable finding comes from Kang (2025), who challenges the industry-standard practice of normalizing Order Flow Imbalance (OFI) by Daily Trading Volume.

### The Problem with Volume Normalization ($S_{TV}$)

Traditionally, quants compute OFI signals as:
$$ S\_{TV} = \frac{\text{BuyVolume} - \text{SellVolume}}{\text{DailyVolume}} $$

- **Flaw**: This implicitly multiplies the true signal (Informed Trading) by **Inverse Turnover** ($M_i / V_i$).
- **Result**: High-turnover stocks (often dominated by retail noise/disagreement) get up-weighted. Low-turnover stocks (often stable, institutionally owned) get down-weighted. This introduces heteroskedastic noise.

### The Solution: Market Cap Normalization ($S_{MC}$)

Informed traders (Smart Money) scale their positions based on **Market Capitalization** (Capacity/Risk limits), not just daily liquidity.
$$ S\_{MC} = \frac{\text{BuyVolume} - \text{SellVolume}}{\text{MarketCap}} $$

- **Performance**: In empirical tests (Korea, 2.1M samples), $S_{MC}$ showed **1.32x higher correlation** with future returns than $S_{TV}$.
- **Robustness**: In multivariate regressions, $S_{TV}$ often flips sign (becomes negative) when controlling for $S_{MC}$, indicating $S_{TV}$ is largely capturing noise/mean-reversion rather than alpha.

**Action Item**:

- Refactor `research/alphas/<alpha_id>/impl.py` to include `OFI_MC`.
- Formula: `(BuyVol - SellVol) / (AvgPrice * SharesOutstanding)`.

---

## 2. Geometric Normalization: The Shape of Liquidity

da Cruz (2026) argues that "levels" (Volume at Bid/Ask) are poor features because they mix **Shear** (Shape change) with **Drift** (Price change).

### The "Gamma" Transformation

Instead of normalizing by scalar volume, normalize by **Liquidity Shape**.

- **Theory**: Financial LOBs lack a characteristic scale, leading to a **Gamma Distribution** of liquidity density: $\rho(x) \propto x^\gamma e^{-\lambda x}$.
- **New Features**:
  - **Curvature ($\gamma$)**: Represents the "tension" or "brittleness" of the book.
  - **Decay ($\lambda$)**: Represents the "depth" or "resilience".
- **Insight**: High Order Imbalance often leads to **Plastic Deformation** (Gamma parameters change) _without_ price movement. Only when "Shear Stress" exceeds a generalized Hooke's Law limit does Price "Rupture" (Jump).

**Action Item**:

- Implement a `LOB_Shape_Fitter` that outputs $(\gamma_t, \lambda_t)$ per snapshot.
- Use $\Delta \gamma$ as a normalized feature instead of $\Delta Volume$.

---

## 3. Deep Learning Normalization: Learnable Splines

Makinde (2026) introduces **T-KAN**, which moves beyond static normalization (Z-score).

### Learnable Activation as Normalization

Standard Deep Learning uses Z-score inputs: $\hat{x} = (x - \mu) / \sigma$.

- **Limitation**: This assumes a linear response scale. A 3-sigma imbalance might typically imply a small move, but in a crisis, a 3-sigma imbalance implies a huge move (Non-linear).
- **KAN Solution**: Kolmogorov-Arnold Networks learn **B-Splines** on the inputs.
  - **Dead Zones**: The network learns to flatten the response for $x \in [-1, 1]$ (Noise).
  - **Amplification**: The network learns to steepen the response for $x > 2$ (Signal).
- **Result**: This effectively creates a **Dynamic, Learnable Normalization** function that adapts to the regime.

**Action Item**:

- Replace standard MLP/Linear heads in Alpha models with **KAN Layers** to capture this non-linear scaling automatically.

---

## 4. Synthesis & Implementation Plan

| Implication     | Current Method   | Proposed Method          | Source             |
| :-------------- | :--------------- | :----------------------- | :----------------- |
| **Factor Norm** | `OFI / Volume`   | `OFI / MarketCap`        | Kang (2025)        |
| **LOB Feature** | `BidQty, AskQty` | `Gamma(\gamma, \lambda)` | da Cruz (2026)     |
| **Model Head**  | `Linear(ReLU)`   | `KAN(Spline)`            | Makinde (2026)     |
| **Volatility**  | `GARCH`          | `Rough Vol (H ≈ 0)`      | Muhle-Karbe (2026) |

### Recommended Workflow for User

1.  **Immediate**: Implement `OFI_MC` in the research backlog. It is a low-hanging fruit with strong theoretical backing.
2.  **Intermediate**: Experiment with **KAN Layers** in the next model retraining cycle (`ppo_cycle10_v3`).
3.  **Advanced**: Build the **Gamma Liquidity Fitter** for the Rust LOB engine to generate "Shape Features".
