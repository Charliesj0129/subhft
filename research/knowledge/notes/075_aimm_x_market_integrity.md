# AIMM-X: Explainable Market Integrity Monitoring System

**Authors**: Sandeep Neela
**Date**: 2026
**Topic**: Market Manipulation Detection, Explainable AI, Social Attention, Surveillance

## Summary

The paper presents **AIMM-X**, a framework for detecting suspicious market activity using only **public data** (OHLCV + Social Attention). It moves away from "Black Box" detection to an explainable scoring system.
Key Components:

1.  **Multi-Source Attention**: Fuses Reddit, StockTwits, Wikipedia, News, and Google Trends into a single "Attention Signal".
2.  **Suspicious Window Detection**: Uses hysteresis thresholding on a composite score of Return + Volatility + Attention to find time windows.
3.  **Integrity Score ($\mathcal{M}$)**: A decomposable score based on 6 factors ($\phi_1$ to $\phi_6$) that explains _why_ a window is suspicious.

## Key Concepts

1.  **Phi Factors ($\phi$)**:
    - $\phi_1$: Return Shock
    - $\phi_2$: Volatility Anomaly
    - $\phi_3$: Attention Spike (Social volume)
    - $\phi_4$: Price-Attention Co-movement (Do they move together?)
    - $\phi_5$: Volume-Price Dissociation (Price moves without volume, or vice-versa)
    - $\phi_6$: Close-to-High/Low Reversal (Intraday pump/dump signatures)
2.  **Triage Approach**:
    - System does not "accuse", it "triages". It outputs ranked windows for human analyst review.
3.  **Hysteresis Segmentation**:
    - Avoids noisy alerts by requiring a High threshold ($Z > 3$) to start a window, but a lower threshold ($Z > 2$) to maintain it.

## Implications for Our Platform

- **Market Integrity Monitor**:
  - **Action**: Implement the **Phi Factors** as features in our real-time monitoring dashboard.
  - **Feature Engineering**: $\phi_5$ (Volume-Price Dissociation) and $\phi_6$ (Intraday Reversal) are excellent alpha candidates for mean-reversion strategies.
  - **Attention Data**: We should start ingesting public attention feeds (e.g. Reddit/Twitter scrapers) to build the $\phi_3$ and $\phi_4$ signals.
- **Explainability**:
  - Adopting the "Decomposable Score" pattern helps debug our own RL agents. Instead of just a "Buy" action, output the contribution of each reward component.

## Tags

#MarketManipulation #Surveillance #ExplainableAI #SocialSentiment #CryptoSurveillance
