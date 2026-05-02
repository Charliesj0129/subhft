#!/bin/bash
# Run R47 TMFD6 realistic backtest — each day in separate process to avoid segfault
set -e
SCRIPT="research/alphas/r47_maker_pivot/run_one_day.py"
OUT_DIR="outputs/team_artifacts/alpha-research/R47_maker_pivot"
mkdir -p "$OUT_DIR"

DAYS=(
    research/data/raw/tmfd6/TMFD6_2026-03-19_l2.hftbt.npz
    research/data/raw/tmfd6/TMFD6_2026-03-20_l2.hftbt.npz
    research/data/raw/tmfd6/TMFD6_2026-03-23_l2.hftbt.npz
    research/data/raw/tmfd6/TMFD6_2026-03-24_l2.hftbt.npz
    research/data/raw/tmfd6/TMFD6_2026-03-26_l2.hftbt.npz
    research/data/raw/tmfd6/TMFD6_2026-03-27_l2.hftbt.npz
    research/data/raw/tmfd6/TMFD6_2026-03-30_l2.hftbt.npz
    research/data/raw/tmfd6/TMFD6_2026-03-31_l2.hftbt.npz
    research/data/raw/tmfd6/TMFD6_2026-04-01_l2.hftbt.npz
    research/data/raw/tmfd6/TMFD6_2026-04-02_l2.hftbt.npz
    research/data/raw/tmfd6/TMFD6_2026-04-07_l2.hftbt.npz
    research/data/raw/tmfd6/TMFD6_2026-04-08_l2.hftbt.npz
)

# Config: name spr mp qm pq
CONFIGS=(
    "spr4_qm3_pq 4 1 3.0 1"
    "spr4_qm3_nopq 4 1 3.0 0"
    "spr4_qm1_pq 4 1 1.0 1"
    "spr3_qm3_pq 3 1 3.0 1"
)

for cfg_line in "${CONFIGS[@]}"; do
    read -r name spr mp qm pq <<< "$cfg_line"
    echo "======== CONFIG: $name (spr>=$spr, mp=$mp, qm=$qm, pq=$pq) ========"
    results_file="$OUT_DIR/tmfd6_real_${name}.jsonl"
    > "$results_file"  # truncate
    for day_file in "${DAYS[@]}"; do
        date=$(basename "$day_file" | sed 's/TMFD6_//' | sed 's/_l2.hftbt.npz//')
        uv run python "$SCRIPT" "$day_file" "$spr" "$mp" "$qm" "$pq" | tee -a "$results_file"
    done
    echo ""
done
echo "DONE"
