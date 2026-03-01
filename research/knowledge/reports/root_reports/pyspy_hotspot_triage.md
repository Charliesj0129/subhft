# Pyspy Hotspot Triage

- scanned_svg_files: 19
- top: 20

## Aggregate Hotspots

| Rank | Frame | Samples | Max % | File Hits | Hint |
|---|---|---:|---:|---:|---|
| 1 | `run_loop (hotpath_load_long.py:19)` | 5444 | 21.81 | 12 | Profile callsite and reduce allocations in this frame. |
| 2 | `_find_and_load (&lt;frozen importlib._bootstrap&gt;:1176)` | 4092 | 19.46 | 25 | Move import/config parsing out of runtime loop; warm cache on startup. |
| 3 | `_find_and_load_unlocked (&lt;frozen importlib._bootstrap&gt;:1147)` | 3697 | 19.46 | 21 | Move import/config parsing out of runtime loop; warm cache on startup. |
| 4 | `_call_with_frames_removed (&lt;frozen importlib._bootstrap&gt;:241)` | 3385 | 19.46 | 19 | Move import/config parsing out of runtime loop; warm cache on startup. |
| 5 | `_load_unlocked (&lt;frozen importlib._bootstrap&gt;:690)` | 3152 | 19.46 | 19 | Move import/config parsing out of runtime loop; warm cache on startup. |
| 6 | `exec_module (&lt;frozen importlib._bootstrap_external&gt;:940)` | 3151 | 19.46 | 19 | Move import/config parsing out of runtime loop; warm cache on startup. |
| 7 | `main (hotpath_load_long.py:37)` | 2882 | 14.60 | 9 | Profile callsite and reduce allocations in this frame. |
| 8 | `main (hotpath_load_long.py:38)` | 2515 | 25.68 | 3 | Profile callsite and reduce allocations in this frame. |
| 9 | `process_event (lob_engine.py:299)` | 2371 | 22.07 | 6 | Optimize LOB hot path (Rust kernel or tighter Python loop). |
| 10 | `run_loop (hotpath_load.py:21)` | 2363 | 35.20 | 6 | Profile callsite and reduce allocations in this frame. |
| 11 | `run_loop (hotpath_load_long.py:21)` | 2233 | 7.37 | 10 | Profile callsite and reduce allocations in this frame. |
| 12 | `process_event (lob_engine.py:234)` | 1990 | 16.64 | 8 | Optimize LOB hot path (Rust kernel or tighter Python loop). |
| 13 | `run_loop (hotpath_load.py:19)` | 1949 | 28.54 | 6 | Profile callsite and reduce allocations in this frame. |
| 14 | `process_event (lob_engine.py:232)` | 1815 | 17.01 | 9 | Optimize LOB hot path (Rust kernel or tighter Python loop). |
| 15 | `apply_update (lob_engine.py:100)` | 1783 | 13.50 | 8 | Optimize LOB hot path (Rust kernel or tighter Python loop). |
| 16 | `main (hotpath_load_long.py:43)` | 1740 | 23.68 | 2 | Profile callsite and reduce allocations in this frame. |
| 17 | `process_event (lob_engine.py:307)` | 1586 | 17.71 | 5 | Optimize LOB hot path (Rust kernel or tighter Python loop). |
| 18 | `main (hotpath_load.py:37)` | 1496 | 33.03 | 3 | Profile callsite and reduce allocations in this frame. |
| 19 | `normalize_bidask (normalizer.py:355)` | 1358 | 17.57 | 3 | Cache normalization lookups and avoid repeated parsing. |
| 20 | `process_event (lob_engine.py:319)` | 1341 | 17.53 | 3 | Optimize LOB hot path (Rust kernel or tighter Python loop). |
