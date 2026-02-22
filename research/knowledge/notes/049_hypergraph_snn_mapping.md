# A Case for Hypergraphs to Model and Map SNNs on Neuromorphic Hardware

**Authors**: Marco Ronzani & Cristina Silvano (Politecnico di Milano)
**Date**: January 2026
**Topic**: Spiking Neural Networks (SNNs), Neuromorphic Hardware, Hypergraph Partitioning, Placement Optimization

## Summary

The paper argues that mapping **Spiking Neural Networks (SNNs)** to **Neuromorphic Hardware** should be modeled as **Hypergraph Partitioning**, not standard Graph Partitioning. This is because neurons (axons) fan out to multiple destinations, creating **Hyperedges** (one source, multiple sinks). Neuromorphic hardware (e.g. Loihi, SpiNNaker) can **multicast** spikes efficiently if destinations are co-located in the same core.

## Key Concepts

1.  **Graph vs Hypergraph**:
    - **Graph**: Every connection is a separate edge. Fails to capture "spike replication" benefit.
    - **Hypergraph**: Grouping destinations allows optimizing for **Second-Order Affinity** (co-membership in hyperedges).
2.  **Synaptic Reuse**: Maximizing the number of co-destination neurons in the same core. This minimizes memory/bandwidth because the core only stores the incoming spike on _one_ hardware queue.
3.  **Algorithm**: **Hyperedge Overlap-based Partitioning**.
    - Greedily selects nodes that maximize the overlap of hyperedges with currently placed nodes.
    - **Result**: 20-30% reduction in inter-core spike traffic and energy consumption.

## Implications for Our Platform

- **Hardware Acceleration**: If we deploy SNNs on **FPGA or Neuromorphic Chips**, we must use **Hypergraph Partitioning** tools (e.g. `KaHiPar` or this custom algorithm) to map neurons to cores.
- **Graph Optimization**: Even for software SNNs, grouping neurons by common inputs (hyperedges) improves cache locality and reduces memory access for synaptic weights.

## Tags

#NeuromorphicComputing #SpikingNeuralNetworks #HypergraphPartitioning #FPGAOptimization #SynapticReuse #SNNMapping
