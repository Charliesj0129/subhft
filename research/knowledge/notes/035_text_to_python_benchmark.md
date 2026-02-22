# Benchmarking Text-to-Python against Text-to-SQL

**Authors**: Hangle Hu et al. (Zhejiang University of Technology)
**Date**: 2025/2026
**Topic**: LLM, Text-to-SQL, Text-to-Python, Semantic Parsing, Agentic Analytics

## Summary

The paper challenges the dominance of Text-to-SQL by exploring **Text-to-Python** as a more flexible alternative for data analytics. It introduces **BIRD-Python**, a benchmark adapted from BIRD-SQL, to evaluate how well LLMs generate Python code (Pandas) for data retrieval. The study highlights a fundamental trade-off: SQL benefits from the "implicit" logic of DBMS (e.g., handling NULLs, sorting optimizations), whereas Python requires "explicit" procedural logic, making it more sensitive to ambiguity.

## Key Concepts

### 1. Paradigm Shift: SQL vs. Python

- **SQL (Declarative)**: Rely on DBMS for execution details. "What to get".
  - Pros: Compact, handles standard operations (Nulls, Joins) implicitly.
  - Cons: Limited for complex analytics, file-based data, or custom logic.
- **Python (Imperative)**: Explicit procedural steps. "How to get it".
  - Pros: Unlimited flexibility (Pandas, ML libraries), handles raw files (CSV/JSON), easier to debug complex workflows.
  - Cons: High "reasoning burden". Models must explicitly handle types, NaNs, sorting, and edge cases that SQL engines do automatically.

### 2. Logic Completion Framework (LCF)

- **Problem**: Python's need for strict logic makes it fail when user queries are ambiguous (underspecified).
- **Solution**: LCF is a 3-phase framework:
  1. **Logic Probing**: The model detects ambiguity and asks a clarifying question.
  2. **Truth Injection**: An "Oracle" (simulated expert) provides the constraint (e.g., "Calculate rate as X/Y, not Z").
  3. **Execution**: The model generates code with the clarified context.
- **Result**: LCF significantly closes the performance gap between SQL and Python.

### 3. Performance

- **Small Models**: Struggle with Python generation (large drop from SQL accuracy) due to the heavy procedural reasoning burden.
- **Reasoning Models (DeepSeek-R1, Qwen3)**: Show much smaller gaps, proving that strong reasoning capabilities can effectively manage Python's verbosity.
- **Verdict**: Text-to-Python is a viable alternative to SQL for _Agentic Analytics_ if the system includes a mechanism to resolve ambiguity (like LCF).

## Implications for Our Platform

- **Agentic Choice**: For our `llm_strategy_selector` or future "Data Analysis Agent", we should prefer **Python/Pandas** generation over SQL when using strong reasoning models (O1/R1 classes).
- **Ambiguity Handling**: We should implement an "Ambiguity Check" step before code generation, similar to LCF, where the agent asks the user to clarify definitions (e.g., "How do you define 'high volatility'?").
- **Flexibility**: Python agents can directly integrate with our `hftbacktest` or `quant stats` libraries, whereas SQL agents are confined to DB queries.

## Tags

#LLM #Text2SQL #Text2Python #AgenticAI #Pandas #BIRD-Benchmark
