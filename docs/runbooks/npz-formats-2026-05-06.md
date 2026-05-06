# NPZ Format Zoo — Detector & Loader Contract (2026-05-06)

**Author:** Charlie · **Status:** Decision locked · **Successor of:** none

## Background

The HFT platform's research/backtest pipeline ingests three different on-disk shapes that all have suffix `.npz` or `.npy`. The original parent-plan called these "three coexisting npz dialects." After probing actual disk state (2026-05-06), the situation is **subtler than three independent dtypes** — they share a common underlying schema but differ in naming, layout, and depth coverage.

## The three on-disk shapes

### 1. `*_l2.hftbt.npz` — CK-export event_dtype (canonical)

```
File:  research/data/raw/tmfd6/TMFD6_<YYYY-MM-DD>_l2.hftbt.npz
Schema: hftbacktest.types.event_dtype
   ('ev', '<u8'), ('exch_ts', '<i8'), ('local_ts', '<i8'),
   ('px', '<f8'), ('qty', '<f8'), ('order_id', '<u8'),
   ('ival', '<i8'), ('fval', '<f8')
NPZ keys: ['data']
Sidecar:  *.meta.json with {symbols, depth_levels: 5, price_scale_applied: 1e6, ...}
Producer: research/data/ck_export/export_golden.py (CK SELECT -> event_dtype)
```

**Event composition (verified):**

```
ev=3489660929  ->  DEPTH | EXCH | LOCAL | SELL  (ask depth update)
ev=3489660930  ->  TRADE | EXCH | LOCAL | SELL  (sell-side trade)
ev=3489660932  ->  EXCH  | LOCAL | SELL          (sell-side metadata)
ev=3758096385  ->  DEPTH | EXCH | LOCAL | BUY   (bid depth update)
ev=3758096386  ->  TRADE | EXCH | LOCAL | BUY   (buy-side trade)
ev=3758096388  ->  EXCH  | LOCAL | BUY           (buy-side metadata)
```

**Key fact:** the `ev` field never carries `DEPTH_SNAPSHOT_EVENT` — each row is a per-level depth update or trade, not a "full snapshot at timestamp T." This is the natural hftbacktest format, not a separate "snapshot AOS" schema.

### 2. `hftbt.npz` — `ensure_hftbt_npz` shim (legacy bridge)

```
File:  <parent>/hftbt.npz   (sibling to research.npy)
Schema: same hftbacktest.types.event_dtype
NPZ keys: ['data']
Producer: research/backtest/hft_native_runner.py::ensure_hftbt_npz()
          replays research.npy <-> {bid_px, bid_qty, ask_px, ask_qty, volume}
          -> 2 DEPTH events (BUY+SELL) + optional TRADE per row
Depth coverage: L1 ONLY (no L2-L5 information in source).
```

**Critical difference vs. (1):** event_dtype identical but produced from L1-only research arrays — `bid_qty_at_tick(i)` for i>1 returns 0 against this shape.

### 3. `*_ticks.npy` + `*_bidask.npy` — legacy structured arrays

```
Files: research/data/raw/tmfd6/TMFD6_<DATE>_ticks.npy
       research/data/raw/tmfd6/TMFD6_<DATE>_bidask.npy
Schema: NumPy structured dtype (NOT NPZ) with fields like
        bid_px, bid_qty, ask_px, ask_qty, volume, local_ts
Producer: legacy alpha-research export pipeline (pre-CK-export era)
```

These feed into `ensure_hftbt_npz` to produce a sibling `hftbt.npz`. They are NOT npz files.

## The actual format gap (Codex finding 2 reframed)

Calling these "three dialects" is misleading. The real gap is:

* **Format-1 vs Format-2 share dtype** but differ in **L2-L5 coverage** (CK has it, ensure-shim doesn't).
* **Format-3 is not npz** at all and must be converted to format-2 via `ensure_hftbt_npz`.

The platform-correctness implication is that **`_resolve_feature_mode -> "lob_feature"` consumes the natural event_dtype regardless of which producer wrote it**, but features that need L2-L5 (e.g. `deep_depth_momentum_x1000` aka MLDM) silently degrade to zero on format-2 inputs. See `docs/runbooks/c75-depth-parity-decision-2026-05-06.md`.

## Existing Gate-A validator is misnamed and partially wrong

`src/hft_platform/alpha/_gate_a.py::_check_hftbacktest_v2_data_format` (lines 302-350) checks `first_ev & DEPTH_SNAPSHOT_EVENT`. **No file in the corpus passes that check** because none start with `DEPTH_SNAPSHOT_EVENT` — they start with regular DEPTH+BUY/SELL events. The validator is currently bypassed for c75 (whose `data_fields: []` skips the deep-format check).

**Action:** Step 2 of the parent plan must replace this check with one that accepts the actual event_dtype shape: an array with `dtype.names == event_dtype.names` and at least one DEPTH (or TRADE) event in the first 100 rows.

## Locked loader contract — Step 2 deliverable

New module: `research/backtest/_npz_format.py`

```python
class NpzFormat(Enum):
    HFTBT_EVENT_L5    = "hftbt_event_l5"      # *_l2.hftbt.npz from CK
    HFTBT_EVENT_L1    = "hftbt_event_l1"      # hftbt.npz from ensure shim
    LEGACY_RESEARCH   = "legacy_research"     # *_ticks.npy / *_bidask.npy
    UNKNOWN           = "unknown"

def detect_npz_format(path: str | Path) -> NpzFormat: ...

class NpzFormatMismatchError(ValueError): ...

def assert_format(path: str | Path, expected: NpzFormat) -> None:
    """Raises NpzFormatMismatchError if detected format != expected."""
```

**Detection rules:**

* `path.endswith("_l2.hftbt.npz")` AND has sidecar `.meta.json` with `depth_levels >= 2` -> `HFTBT_EVENT_L5`.
* `path.endswith("hftbt.npz")` AND no L2 sidecar -> `HFTBT_EVENT_L1`.
* `path.endswith("_ticks.npy")` OR `path.endswith("_bidask.npy")` -> `LEGACY_RESEARCH`.
* Anything else -> `UNKNOWN`.

**Wiring rules:**

* `_run_adapter_slice` in `hft_native_runner.py`: when `feature_mode == "lob_feature"`, call `assert_format(path, NpzFormat.HFTBT_EVENT_L5)`. (FE-v3's MLDM term needs L2-L5; refusing L1 inputs forces upstream to fix the data path, not silently produce zero MLDM.)
* `_check_hftbacktest_v2_data_format` (Gate A): replace the broken DEPTH_SNAPSHOT check with `detect_npz_format(path) != UNKNOWN`.

## Verification (post-Step-2)

```bash
uv run pytest tests/unit/research/backtest/test_npz_format.py -q --no-cov --tb=short
uv run python -c "from research.backtest._npz_format import detect_npz_format, NpzFormat; \
   print(detect_npz_format('research/data/raw/tmfd6/TMFD6_2026-04-14_l2.hftbt.npz'))"
# expect: NpzFormat.HFTBT_EVENT_L5
```

## Cross-references

* TMFD6 corpus: `docs/runbooks/tmfd6-corpus-2026-05-06.md`
* Depth parity (D1/D2 fork): `docs/runbooks/c75-depth-parity-decision-2026-05-06.md`
* Existing producers: `research/data/ck_export/export_golden.py`; `research/backtest/hft_native_runner.py::ensure_hftbt_npz`
