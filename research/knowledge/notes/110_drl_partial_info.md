# Deep Reinforcement Learning for Optimal Trading with Partial Information

**Authors**: Andrea Macrì, Sebastian Jaimungal, Fabrizio Lillo
**Date**: 2025-11
**Topic**: Deep Reinforcement Learning (DRL), Partial Information, Regime Switching, Optimal Trading, DDPG, Recurrent Neural Networks (GRU)

## Summary
The paper addresses the problem of **optimal trading** of a mean-reverting signal where the parameters (Long-run mean $\theta_t$, Mean-reversion speed $\kappa_t$, Volatility $\sigma_t$) are **latent** and driven by a Regime-Switching Markov Chain.
*   **Problem**: Standard RL agents struggle when the "parameters" of the market change invisibly (e.g., mean reversion level shifts).
*   **Methodology**:
    *   **Signal**: $dS_t = \kappa_t (\theta_t - S_t)dt + \sigma_t dW_t$.
    *   **Algorithms Compared**:
        1.  **hid-DDPG** (One-Step): Feed raw history $S_{t-W:t}$ into a GRU, then pipe the hidden state $h_t$ directly into the DDPG Actor-Critic.
        2.  **prob-DDPG** (Two-Step, **Best Performer**): First, train a Classifier to estimate the *Posterior Probability* of the current regime $\Phi_{t,k} = P(\theta_t = \phi_k | S_{t-W:t})$. Then feed $(S_t, I_t, \Phi_{t,k})$ into the DDPG agent.
        3.  **reg-DDPG** (Two-Step): First, train a Regressor to predict next price $\tilde{S}_{t+1}$. Feed $(S_t, I_t, \tilde{S}_{t+1})$ into DDPG.
*   **Key Findings**:
    *   **Explicit Beliefs Win**: `prob-DDPG` significantly outperformed the others. Giving the agent explicit "beliefs" about the current market regime (e.g., "80% chance we are in High Mean regime") is much better than letting it figure it out from raw LSTM states (`hid-DDPG`) or point forecasts (`reg-DDPG`).
    *   **Interpretability**: `prob-DDPG` policies are interpretable (e.g., "Agent buys more when Probability(High Mean) is high").

## Key Concepts
1.  **Partial Information Control**:
    *   In real markets, we never know the "true" parameters. We only have filtered estimates. RL agents should be trained with *beliefs* (probabilities) as states, not just prices.
2.  **Separation Principle in RL**:
    *   It is often better to separate **State Estimation** (Filtering) from **Control** (RL Policy) rather than learning end-to-end. This paper confirms that explicit filtering aids the RL policy.

## Implications for Our Platform
-   **Regime-Aware RL**:
    *   Instead of feeding raw OCHLV to our PPO/SAC agent, we should pre-process it through a **HMM or Gaussian Mixture Model** to get "Regime Probabilities" (e.g., Low/High Volatility, Bull/Bear Trend).
    *   Feed these probabilities as explicit features: `[Price, Inventory, Prob_Regime_1, Prob_Regime_2]`.
-   **Architecture**:
    *   Adopt the **Two-Step Approach**. Step 1: Supervised/Unsupervised learning of market states. Step 2: RL for execution/trading conditional on those states.

## Tags
#RL #DDPG #RegimeSwitching #PartialInformation #OptimalTrading #GRU #StateEstimation
