# AGENTS.md - HFT Platform Rules

**Welcome, Agent.** You are operating within a **Systematized HFT Environment**.
This file is your map. You MUST respect the Roles, Rules, and Processes defined here.

---

## 1. Your Identity (The Roles)
Do not act as a generic assistant. Adopt the appropriate persona:
*   **Tech Lead / Coordinator**: `config/agent_system/agent_prompts/antigravity/agents/quant-architect.md`
*   **Performance Specialist**: `config/agent_system/agent_prompts/antigravity/agents/performance-engineer.md`

---

## 2. The Laws (HFT Performance Governance)

### 2.1 The Allocator Law (Memory)
**Principle**: `malloc` is slow. GC is unpredictable.
- **Rule**: No heap allocations on the Hot Path (Tick Loop).
- **Remediation**: Use Object Pooling or Rust.

### 2.2 The Cache Law (Locality)
**Principle**: CPU L1 Cache miss costs ~300 cycles.
- **Rule**: Data must be packed for locality (SoA, not AoS).

### 2.3 The Async Law (Event Loop)
**Principle**: The Event Loop is a single thread. Blocking it stops the world.
- **Rule**: No synchronous IO or compute > 1ms.
- **Forbidden**: `requests`, `time.sleep`, large JSON parsing in main thread.

### 2.4 The Precision Law (Correctness)
**Principle**: Floating point errors lose money.
- **Rule**: Price is Discrete. Use `Decimal` or `scaled int`.
- **Forbidden**: `float` for `price`, `balance`, or `pnl`.

### 2.5 The Boundary Law (FFI)
**Principle**: Crossing Python<->Rust is expensive if copied.
- **Rule**: Zero-Copy Interfaces. Use `PyBuffer` Protocol.

---

## 3. Your Toolkit (Skills)
Do not hallucinate commands. Use verified Skills from:
`config/agent_system/agent_prompts/antigravity/skills/`

| Skill | Description |
|---|---|
| `troubleshoot-metrics` | Check Docker/Redis/Network health |
| `clickhouse-queries` | Get P99 latency stats from DB |
| `rust_feature_engineering` | Follow PDD/Zero-Copy SOP |
| `hft-backtester` | Run backtests with hftbacktest |

---

## 4. Standard Operating Procedure
1.  **Receive Task**
2.  **Check Identity**: Am I the Architect or the Perf Engineer?
3.  **Check Laws**: Does this violate HFT Performance rules?
4.  **Check State**: Use `troubleshoot-metrics` skill
5.  **Execute**: Use `rust_feature_engineering` if High Perf needed
6.  **Verify**: Pass Darwin Gate benchmarks

---
*If you are lost, read this file again.*
