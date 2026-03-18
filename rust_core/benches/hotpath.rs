//! Hot-path micro-benchmarks for rust_core.
//!
//! Run: `cd rust_core && cargo bench`
//!
//! NOTE: BookStateInner and sort helpers are duplicated here because the
//! cdylib crate type prevents linking benchmarks against internal modules.
//! These must be kept in sync with normalizer_lob_fused.rs.

use criterion::{black_box, criterion_group, criterion_main, Criterion};

/// Duplicated from normalizer_lob_fused.rs for benchmarking.
/// Keep in sync with the source module.
struct BookStateInner {
    bids: Vec<[i64; 2]>,
    asks: Vec<[i64; 2]>,
    best_bid: i64,
    best_ask: i64,
    bid_depth: i64,
    ask_depth: i64,
    mid_x2: i64,
    spread_scaled: i64,
    imbalance_ppm: i64,
    top_imbalance: f64,
}

impl BookStateInner {
    fn new() -> Self {
        Self {
            bids: Vec::with_capacity(8),
            asks: Vec::with_capacity(8),
            best_bid: 0,
            best_ask: 0,
            bid_depth: 0,
            ask_depth: 0,
            mid_x2: 0,
            spread_scaled: 0,
            imbalance_ppm: 0,
            top_imbalance: 0.0,
        }
    }

    #[inline(always)]
    fn recompute_stats(&mut self) {
        self.best_bid = if self.bids.is_empty() {
            0
        } else {
            self.bids[0][0]
        };
        self.best_ask = if self.asks.is_empty() {
            0
        } else {
            self.asks[0][0]
        };
        self.bid_depth = self.bids.iter().map(|r| r[1]).sum();
        self.ask_depth = self.asks.iter().map(|r| r[1]).sum();

        if self.best_bid > 0 && self.best_ask > 0 {
            self.mid_x2 = self.best_bid + self.best_ask;
            self.spread_scaled = self.best_ask - self.best_bid;
            let total = self.bid_depth + self.ask_depth;
            self.imbalance_ppm = if total > 0 {
                (self.bid_depth - self.ask_depth) * 1_000_000 / total
            } else {
                0
            };
            let bv_top = self.bids[0][1];
            let av_top = self.asks[0][1];
            let top_total = bv_top + av_top;
            self.top_imbalance = if top_total > 0 {
                (bv_top - av_top) as f64 / top_total as f64
            } else {
                0.0
            };
        } else {
            self.mid_x2 = 0;
            self.spread_scaled = 0;
            self.imbalance_ppm = 0;
            self.top_imbalance = 0.0;
        }
    }
}

#[inline(always)]
fn is_sorted_desc(levels: &[[i64; 2]]) -> bool {
    levels.windows(2).all(|w| w[0][0] >= w[1][0])
}

#[inline(always)]
fn is_sorted_asc(levels: &[[i64; 2]]) -> bool {
    levels.windows(2).all(|w| w[0][0] <= w[1][0])
}

fn make_book(n_levels: usize) -> BookStateInner {
    let mut book = BookStateInner::new();
    for i in 0..n_levels {
        book.bids
            .push([1_000_000 - (i as i64 * 1000), 100 + i as i64]);
        book.asks
            .push([1_001_000 + (i as i64 * 1000), 80 + i as i64]);
    }
    book
}

fn bench_recompute_stats(c: &mut Criterion) {
    let mut group = c.benchmark_group("BookStateInner::recompute_stats");

    for n_levels in [5, 10, 20] {
        group.bench_function(format!("{n_levels}_levels"), |b| {
            let mut book = make_book(n_levels);
            b.iter(|| {
                book.recompute_stats();
                black_box(&book);
            });
        });
    }

    group.finish();
}

fn bench_is_sorted(c: &mut Criterion) {
    let mut group = c.benchmark_group("sorted_check");

    for n_levels in [5, 10, 20] {
        let bids: Vec<[i64; 2]> = (0..n_levels)
            .map(|i| [1_000_000 - (i as i64 * 1000), 100])
            .collect();
        let asks: Vec<[i64; 2]> = (0..n_levels)
            .map(|i| [1_001_000 + (i as i64 * 1000), 100])
            .collect();

        group.bench_function(format!("desc_{n_levels}"), |b| {
            b.iter(|| is_sorted_desc(black_box(&bids)));
        });
        group.bench_function(format!("asc_{n_levels}"), |b| {
            b.iter(|| is_sorted_asc(black_box(&asks)));
        });
    }

    group.finish();
}

criterion_group!(benches, bench_recompute_stats, bench_is_sorted);
criterion_main!(benches);
