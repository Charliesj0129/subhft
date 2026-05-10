---
name: sequential-thinking
description: Force step-by-step reasoning (hypothesis → evidence → elimination → conclusion) to reduce hallucination on complex logic. Use when debugging race conditions, designing distributed system flows, or answering "why did this happen?".
---

# Sequential Thinking

Logician persona. Structures reasoning so the logical jump is visible and disprovable.

## Protocol

Output the thought process in these tags before presenting the solution:

```
<thought_process>
1. **Hypothesis Generation**: List 3 possible causes.
2. **Evidence Gathering**: What logs/metrics support each?
3. **Elimination**: Disprove hypotheses based on facts.
4. **Conclusion**: The remaining cause is the most likely.
</thought_process>
```

Then provide the solution.
