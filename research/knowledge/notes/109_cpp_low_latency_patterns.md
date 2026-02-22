# C++ Design Patterns for Low-Latency Applications Including High-Frequency Trading

**Authors**: Paul Bilokon, Burak Gunduz (Imperial College London)
**Date**: 2023-09
**Topic**: Low-Latency C++, HFT Design Patterns, LMAX Disruptor, Cache Optimization, Benchmarking

## Summary
The paper provides a comprehensive guide to **Low-Latency C++ Programming** for HFT, backed by benchmarks. It includes a repository of optimization techniques and a C++ implementation of the **LMAX Disruptor** pattern.
*   **Optimization Techniques**:
    *   **Cache Warming**: Pre-loading data into CPU cache (L1/L2) improves access times by ~90% (267ms $\to$ 25ms).
    *   **Compile-Time Dispatch**: Using Templates/CRTP instead of Virtual Functions saves ~0.7ns per call.
    *   **Constexpr**: Computing factorials/lookups at compile-time reduces runtime cost to zero.
    *   **Branch Prediction**: Using `likely()`/`unlikely()` hints helps the CPU pipeline.
*   **LMAX Disruptor (C++)**:
    *   A Lock-Free Ring Buffer that handles inter-thread communication.
    *   **Key Idea**: Single Writer principle + Memory Barriers (no Mutexes/Locks). Consumers track their own "Sequence Number".
    *   **Performance**: Outperforms `std::queue` and other locking queues by orders of magnitude in throughput and latency variance.

## Key Concepts
1.  **Mechanical Sympathy**:
    *   Understanding the hardware (cache lines, false sharing, branch prediction) to write efficient software.
    *   Example: Padding structs to 64 bytes to avoid **False Sharing** on cache lines.
2.  **Ring Buffer (Disruptor)**:
    *   Pre-allocated array (no dynamic `malloc` during trading).
    *   Sequence Barriers ensure consumers don't overtake producers or each other.

## Implications for Our Platform
-   **Refactoring Hot Paths**:
    *   Review our `LOB` and `Strategy` interaction. Are we using `virtual` methods? If so, refactor to **CRTP** (Curiously Recurring Template Pattern) for static polymorphism.
    *   Ensure our Market Data circular buffers are **Cache Line Aligned** (64 bytes).
-   **Disruptor Integration**:
    *   For our Event Bus (connecting Feed Handler $\to$ Strategy $\to$ Order Manager), we should use a C++ implementation of the Disruptor (like the one in this paper's repo) instead of a simple `bip::deque` protected by a mutex.
-   **Cache Warming**:
    *   Before market open, "warm" the order book memory structures by replaying a few messages to ensure pages are in TLB/L1 cache.

## Tags
#Cpp #LowLatency #HFT #DesignPatterns #Disruptor #CacheOptimization #CRTP #Benchmarking
