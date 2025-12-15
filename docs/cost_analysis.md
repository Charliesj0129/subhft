# Azure HFT Data Collection: Cost & Performance Analysis (Student Plan)

This guide helps you maximize your Azure Student credits ($100/12mo usually) to collect 3 months of high-quality market data for research.

## 1. Symbol Selection Strategy (Targeting)
**Goal**: Maximize Research Value per GB of storage/cost.
**Constraint**: Bandwidth & Disk Space.

### Recommendation: "The Market Movers" (Top 5-10)
Instead of recording all 1000+ stocks (which generates TBs of data), focus on the most liquid stocks that drive the index. These provide the best data for Micro-Alpha research.

*   **Tier 1 (Must Have)**: `2330` (TSMC), `2317` (Foxconn), `2454` (Mediatek).
*   **Tier 2 (Financials)**: `2881` (Fubon), `2882` (Cathay).
*   **Tier 3 (Volatile/Shipping)**: `2603` (Evergreen).

**Data Volume Estimate (Per Symbol / Day)**:
*   **Tick Data**: ~20 MB (JSONL) / ~4 MB (Parquet)
*   **Full LOB (BidAsk)**: ~100 MB (JSONL) / ~20 MB (Parquet)
*   **Total for 10 Symbols (3 Months / 60 Days)**:
    *   Raw JSONL: `60 * 10 * 120MB` ≈ **72 GB**
    *   Compressed Parquet: `60 * 10 * 24MB` ≈ **15 GB**
    *   *Manageable on small disks!*

## 2. VM Performance & Cost Choice
For **Data Collection Only** (Recording), you don't need the ultra-low latency `F-series`. You need stability and memory buffer.

| Series | Spec | Approx Cost (East Asia) | Use Case | Verdict |
|---|---|---|---|---|
| **Standard_B1ms** | 1 vCPU, 2GB RAM | ~$15 / mo | Ultra-low budget | ⚠️ Risk of OOM (Out of Memory) during peak hours. |
| **Standard_B2s** | 2 vCPU, 4GB RAM | ~$30 / mo | **Collection** | ✅ **Winner for Students**. Burstable CPU is fine for recording. |
| **Standard_F2s_v2** | 2 vCPU, 4GB RAM | ~$85 / mo | **Live Trading** | ❌ Overkill just for recording. Save budget. |
| **Spot Instance** | Any | ~90% off | R&D | ❌ **Do Not Use**. You will lose data when evicted. |

> **Recommendation**: Start with **Standard_B2s** (Linux). It balances cost and RAM. If purely collecting, 2 vCPU is plenty to handle the WebSocket stream for 10-20 symbols.

## 3. Network Latency & Region
You are connecting to Taiwan Stock Exchange (TWSE) via Shioaji (ISP in Taiwan).
Light travels fast, but routing matters.

*   **Region**: `Asia Pacific (Japan East)` or `Asia Pacific (East Asia - Hong Kong)`.
    *   **Japan East**: Generally minimal jitter, massive bandwidth pipes to Taiwan.
    *   **East Asia (HK)**: Physically closer, but routing can be variable.
    *   **Southeast Asia (Singapore)**: Solid alternative.
*   **Latency Impact**:
    *   For **Recording**: Latency **does not matter**. Timestamps from the Exchange (`exch_ts`) are preserved regardless of when you receive them (20ms later vs 50ms later). Your Backtest uses `exch_ts`.
    *   For **Live Trading**: Latency matters. You want <20ms roundtrip.

> **Recommendation**: Choose **Japan East** or **East Asia**.

## 4. Operational Plan (Student Budget Survival)
To strictly stay within $100 for 3 months:

1.  **Automation**: Do not leave the VM running 24/7.
    *   Market Hours: 09:00 - 13:30 (4.5 hours).
    *   **Auto-Shutdown**: Configure Azure Auto-shutdown at 14:00.
    *   **Auto-Start**: Use Azure Automation Runbook to start VM at 08:30.
    *   **Cost Savings**: You typically pay for "Compute Hours". Running 6 hours/day vs 24 hours/day saves **75%**.
    *   **Disk Cost**: You pay for disk 24/7 (~$1.50/month for 32GB Standard SSD).

2.  **Storage Strategy**:
    *   **Don't use Premium SSD** for data dump. Standard SSD is fine for sequential writes (logging).
    *   **Offload**: Once a week, move data to your local PC or Azure Blob Storage (Cool Tier) to free up VM disk space.

## Total Estimated Monthly Cost (Optimized)
*   **VM (B2s)** running 8hrs/day (20 days): ~$10.00
*   **Disk (64GB Standard SSD)**: ~$3.00
*   **Network Egress**: First 100GB free (usually).
*   **Total**: **~$13.00 / Month**

**Result**: You can easily run this for 3 months (~$40 total) using your Student Credit, leaving budget for experiments.
