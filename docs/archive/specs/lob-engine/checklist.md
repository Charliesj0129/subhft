# LOB Engine Checklist

## Functional Requirements
- [ ] **BookState**: Stores top-5 bids/asks and metadata.
- [ ] **Snapshot**: `apply_snapshot` resets book and sets `exch_ts`.
- [ ] **Incremental**: `update_incremental` updates levels and check monotonicity.
- [ ] **Features**: Computes `mid`, `spread`, `imbalance`, `depth` correctly.
- [ ] **API**: `get_features` provides thread-safe access.

## Observability
- [ ] **Metrics**: `lob_updates_total` and `lob_snapshots_total` incremented.
- [ ] **Logging**: Warnings for out-of-order events (if enabled).

## Testing
- [ ] Unit tests pass for Snapshot application.
- [ ] Unit tests pass for Incremental updates.
- [ ] Unit tests pass with missing symbols.
