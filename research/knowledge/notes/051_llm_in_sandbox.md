# LLM-in-Sandbox Elicits General Agentic Intelligence

**Authors**: Daixuan Cheng et al. (Microsoft Research, Tsinghua)
**Date**: January 2026
**Topic**: Generalist Agents, Tool Use, Code Sandbox, Reinforcement Learning

## Summary

The paper demonstrates that granting Large Language Models (LLMs) access to a **Linux Sandbox** (with Terminal, File System, and Internet) significantly improves their performance across _non-code_ domains like Math, Physics, and Biomedicine. It also introduces **LLM-in-Sandbox-RL**, a training method to teach weaker models how to use the sandbox effectively.

## Key Concepts

1.  **Meta-Capabilities**:
    - **Self-Tooling**: Installing external libraries (e.g. `pip install rdkit`) to solve domain problems.
    - **File Management**: Processing large datasets (100k+ tokens) using `grep` and `sed` instead of filling context window.
    - **Computation**: Writing Python scripts to solve math problems or verify constraints (e.g. "Generate a sentence with exactly 53 characters").
2.  **Performance**:
    - LLM-in-Sandbox mode boosts performance by **+24%** on Math and **+12%** on Instruction Following compared to vanilla LLM mode.
    - **Computational Efficiency**: Reduces token usage by 8x for long-context tasks (by offloading "reading" to `grep`).
3.  **RL Training**: Training on generic "File Retrieval" tasks helps models generalize to complex agentic behaviors.

## Implications for Our Platform

- **Agentic Framework**:
  - Our **Strategy Agents** should NOT just "text generate" trade ideas. They should have a **Sandbox**.
  - **Verification**: Before proposing an Alpha, the agent should write a backtest script, run it in the sandbox, parse the output, and only propose it if Sharpe > 2.0.
  - **Data Analysis**: Instead of feeding CSV text to the LLM, give it `access_to_parquet_files` and let it use `pandas` to query statistics.
- **Implementation**: We should wrap our Agents in a Docker container with `execute_bash` capability (restricted for safety).

## Tags

#AgenticAI #ToolUse #CodeSandbox #ReinforcementLearning #GeneralistAgents #PythonForFinance #SelfCorrection
