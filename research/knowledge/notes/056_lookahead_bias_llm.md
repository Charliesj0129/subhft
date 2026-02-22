# A Test of Lookahead Bias in LLM Forecasts

**Authors**: Zhenyu Gao et al. (CUHK)
**Date**: January 2026
**Topic**: LLM, Lookahead Bias, Forecasting, Data Contamination

## Summary

The paper addresses a critical flaw in using LLMs for financial forecasting: **Lookahead Bias**. Since LLMs are trained on vast internet data (Common Crawl), they have likely "seen" the news headlines and the subsequent market reactions in their training set. Thus, their "predictions" are often just **memory retrieval**. The paper introduces a statistical test using **Lookahead Propensity (LAP)** to detect this.

## Key Concepts

1.  **Lookahead Propensity (LAP)**:
    - A metric to quantify how likely a prompt (e.g., a news headline) was in the LLM's training data.
    - **Formula**: `MIN-K% PROB`. The average log-probability of the **bottom 20%** (rarest) tokens in the prompt.
    - **Intuition**: If an LLM has memorized a text, it assigns anomalously _high_ probabilities to the _rare_ tokens in that text. A high LAP score means the LLM "knows" this text.
2.  **The Test**:
    - Regress `Forecast_Accuracy ~ LAP`.
    - **Finding**: There is a strong positive correlation. LLMs are accurate _mostly_ when LAP is high (i.e., when they have seen the news before).
    - **Implication**: Subtracting the LAP effect significantly reduces the apparent "Alpha" of LLMs.

## Implications for Our Platform

- **LLM Validator Module**:
  - **CRITICAL**: We cannot blindly trust LLM sentiment analysis or price predictions.
  - **Action**: Implement a `LookaheadValidator` class.
  - **Procedure**: For every prompt sent to the LLM:
    1.  Get `token_logprobs` from the API.
    2.  Calculate `LAP` (mean of bottom 20%).
    3.  If `LAP > Threshold` (e.g., top decile of historical LAPs), **DISCARD** the prediction. It is likely contaminated.
- **Backtesting**:
  - When backtesting LLM strategies, we must exclude high-LAP samples to get a realistic Sharpe ratio.

## Tags

#LLM #LookaheadBias #DataContamination #Forecasting #AlphaValidation
