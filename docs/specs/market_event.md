# Market Event Contracts

## Overview
This document defines the schema for events normalized by the FeedAdapter and published to the Event Bus.
Based on `market_data/streaming/stocks.md`.

## Data Types
- `Price`: Fixed-point integer (scaled * 10,000 to match Rust impl).
- `Volume`: Fixed-point integer (scaled * 1000 commonly, but Shioaji sends integers for shares).
- `Timestamp`: Unix nanoseconds (int64).

## Event Schemas

### 1. MarketEvent (Union)
Structure containing headers and payload.
- `seq`: Monotonic sequence ID.
- `local_ts`: Ingest timestamp (ns).
- `exch_ts`: Exchange timestamp (ns).
- `symbol`: Symbol code (e.g., "2330").

### 2. Payload Types

#### Tick
Represents a trade match.
- `price`: Price
- `volume`: Volume
- `tick_type`: Enum (0=Buy, 1=Sell, 2=Unknown)
- `simtrade`: Boolean (1=Simulated, 0=Real)

#### BidAsk
Top-5 Level 2 updates.
- `bids`: Array[5] of `{price, volume}`
- `asks`: Array[5] of `{price, volume}`
- `diff_bid_vol`: Array[5] of volume change (optional)
- `diff_ask_vol`: Array[5] of volume change (optional)

#### Quote (Optional)
Combined view.
- Contains both Tick and BidAsk fields.
- `intraday_odd`: Boolean flag.

### 3. Control Events
- `FeedControl`:
  - `state`: Enum (INIT, TRADING, HALTED, RECOVERING)
  - `reason`: String
