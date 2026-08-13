"""Fast GA over Supertrend event-level routing/gate parameters.

This uses the already-built Supertrend event features (ATR=10, factor=3.0,
1m/3m/5m/15m) and searches a >100M routing space: timeframe, direction
composition, hour masks, liquidity/path gates, and fixed-hold exits.

Fitness uses IS only. OOS is reported, not used for selection.
"""

from __future__ import annotations

import argparse
import html
import json
import math
import random
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd


ROOT = Path(__file__).resolve().parents[2]
EVENTS_PATH = ROOT / "outputs/liquidity_score/pdq_supertrend_backtest/supertrend_event_features.csv"
OUT_DIR = ROOT / "outputs/liquidity_score/pdq_supertrend_gate_ga_search"

TIMEFRAMES = ("1m", "3m", "5m", "15m")
HOLD_VALUES = (180, 300, 600, 900)
DIRECTION_MODES = (
    "ST",
    "ST_C_ALIGNED",
    "ST_C_OPPOSITE",
    "ST_TSI_ALIGNED",
    "ST_C_TSI",
    "ST_ETXF_ALIGNED",
    "ST_C_ETXF",
    "ST_C_TSI_ETXF",
)
THRESH_VALUES = (-1, 50, 60, 70, 80, 90)
VWAP_MODES = ("any", "aligned", "opposite")
OR_MODES = ("any", "aligned", "opposite")
CSHAPE_MODES = (
    "any",
    "low_spike_low_persist",
    "low_spike_high_persist",
    "high_spike_low_persist",
    "high_spike_high_persist",
)
LIQ_MODES = ("any", "recovering", "not_deteriorating")
COST_POINTS = 4.0


@dataclass(frozen=True)
class Gene:
    timeframe: str
    direction_mode: str
    hold_s: int
    hour_mask: int
    abs_c_q: int
    rvexp_q: int
    spread_q: int
    d5_q: int
    no_toxic: bool
    cshape_mode: str
    liquidity_mode: str
    vwap_mode: str
    or_mode: str
    require_clean_proxy: bool
    require_reverse_proxy: bool


class Evaluator:
    def __init__(self) -> None:
        self.events = pd.read_csv(EVENTS_PATH)
        self._prepare_quantiles()
        self._prepare_arrays()

    def _prepare_quantiles(self) -> None:
        for col, prefix in [
            ("abs_C60", "abs_c"),
            ("RVExp_t0", "rvexp"),
            ("spread_t0", "spread"),
            ("D5_t0", "d5"),
        ]:
            for q in (50, 60, 70, 80, 90):
                self.events[f"{prefix}_q{q}"] = self.events.groupby("split")[col].transform(
                    lambda s, qq=q: s.quantile(qq / 100.0)
                )

    def _prepare_arrays(self) -> None:
        e = self.events
        self.is_mask = e["split"].eq("IS").to_numpy()
        self.oos_mask = e["split"].eq("OOS").to_numpy()
        self.day = e["day"].to_numpy(dtype=object)
        self.hour = e["hour_tpe"].to_numpy(dtype=np.int16)
        self.c_dir = e["entry_dir_C"].fillna(0).to_numpy(dtype=np.int8)
        self.tsi_dir = e["TSI15_dir"].fillna(0).to_numpy(dtype=np.int8)
        self.etxf_dir = e["e_TXF_dir"].fillna(0).to_numpy(dtype=np.int8)
        self.st_dir = {
            tf: e[f"st_dir_{tf}"].fillna(0).to_numpy(dtype=np.int8)
            for tf in TIMEFRAMES
        }
        self.ret = {hold: e[f"ret_{hold}"].to_numpy(dtype=float) for hold in HOLD_VALUES}
        self.abs_c = e["abs_C60"].to_numpy(dtype=float)
        self.rvexp = e["RVExp_t0"].to_numpy(dtype=float)
        self.spread = e["spread_t0"].to_numpy(dtype=float)
        self.d5 = e["D5_t0"].to_numpy(dtype=float)
        self.toxic = e["Toxicity_q70"].astype(bool).to_numpy()
        self.clean_proxy = e["clean_taker_proxy_t0"].astype(bool).to_numpy()
        self.reverse_proxy = e["reverse_proxy_t0"].astype(bool).to_numpy()
        self.cshape = e["C_shape_bucket"].astype(str).to_numpy()
        self.liq = e["liquidity_state_30"].astype(str).to_numpy()
        self.vwap_state = e["VWAP300_C_state"].astype(str).to_numpy()
        self.or_state = e["OR_C_state"].astype(str).to_numpy()
        self.quantile_arrays = {
            "abs_c": {q: e[f"abs_c_q{q}"].to_numpy(dtype=float) for q in (50, 60, 70, 80, 90)},
            "rvexp": {q: e[f"rvexp_q{q}"].to_numpy(dtype=float) for q in (50, 60, 70, 80, 90)},
            "spread": {q: e[f"spread_q{q}"].to_numpy(dtype=float) for q in (50, 60, 70, 80, 90)},
            "d5": {q: e[f"d5_q{q}"].to_numpy(dtype=float) for q in (50, 60, 70, 80, 90)},
        }

    def direction(self, gene: Gene) -> np.ndarray:
        st = self.st_dir[gene.timeframe]
        if gene.direction_mode == "ST":
            return st
        if gene.direction_mode == "ST_C_ALIGNED":
            return np.where((st != 0) & (st == self.c_dir), st, 0).astype(np.int8)
        if gene.direction_mode == "ST_C_OPPOSITE":
            return np.where((st != 0) & (st == -self.c_dir), st, 0).astype(np.int8)
        if gene.direction_mode == "ST_TSI_ALIGNED":
            return np.where((st != 0) & (st == self.tsi_dir), st, 0).astype(np.int8)
        if gene.direction_mode == "ST_C_TSI":
            return np.where((st != 0) & (st == self.c_dir) & (st == self.tsi_dir), st, 0).astype(np.int8)
        if gene.direction_mode == "ST_ETXF_ALIGNED":
            return np.where((st != 0) & (st == self.etxf_dir), st, 0).astype(np.int8)
        if gene.direction_mode == "ST_C_ETXF":
            return np.where((st != 0) & (st == self.c_dir) & (st == self.etxf_dir), st, 0).astype(np.int8)
        if gene.direction_mode == "ST_C_TSI_ETXF":
            return np.where((st != 0) & (st == self.c_dir) & (st == self.tsi_dir) & (st == self.etxf_dir), st, 0).astype(np.int8)
        raise ValueError(gene.direction_mode)

    def mask(self, gene: Gene, direction: np.ndarray) -> np.ndarray:
        mask = direction != 0
        mask &= np.array([(gene.hour_mask >> int(h)) & 1 for h in self.hour], dtype=bool)
        if gene.abs_c_q != -1:
            mask &= self.abs_c >= self.quantile_arrays["abs_c"][gene.abs_c_q]
        if gene.rvexp_q != -1:
            mask &= self.rvexp >= self.quantile_arrays["rvexp"][gene.rvexp_q]
        if gene.spread_q != -1:
            mask &= self.spread <= self.quantile_arrays["spread"][gene.spread_q]
        if gene.d5_q != -1:
            mask &= self.d5 >= self.quantile_arrays["d5"][gene.d5_q]
        if gene.no_toxic:
            mask &= ~self.toxic
        if gene.cshape_mode != "any":
            mask &= self.cshape == gene.cshape_mode
        if gene.liquidity_mode == "recovering":
            mask &= self.liq == "recovering"
        elif gene.liquidity_mode == "not_deteriorating":
            mask &= self.liq != "deteriorating"
        if gene.vwap_mode == "aligned":
            mask &= self.vwap_state == "VWAP_C_aligned"
        elif gene.vwap_mode == "opposite":
            mask &= self.vwap_state == "VWAP_C_opposite"
        if gene.or_mode == "aligned":
            mask &= self.or_state == "OR_C_aligned"
        elif gene.or_mode == "opposite":
            mask &= self.or_state == "OR_C_opposite"
        if gene.require_clean_proxy:
            mask &= self.clean_proxy
        if gene.require_reverse_proxy:
            mask &= self.reverse_proxy
        return mask

    def summarize(self, split_mask: np.ndarray, mask: np.ndarray, direction: np.ndarray, hold_s: int) -> dict[str, float | int]:
        m = split_mask & mask & np.isfinite(self.ret[hold_s])
        n = int(m.sum())
        if n == 0:
            return empty_summary()
        pnl = direction[m].astype(float) * self.ret[hold_s][m]
        days = self.day[m]
        daily = pd.Series(pnl).groupby(days, sort=True).sum()
        abs_total = float(daily.abs().sum())
        top5 = float(daily.abs().nlargest(5).sum() / abs_total) if abs_total > 0 else math.nan
        keep_days = daily.abs().sort_values(ascending=False).iloc[3:].index
        drop = pnl[np.isin(days, keep_days)]
        return {
            "n": n,
            "active_days": int(len(set(days))),
            "gross_mean": float(np.mean(pnl)),
            "net_cost4": float(np.mean(pnl) - COST_POINTS),
            "hit_rate": float(np.mean(pnl > 0)),
            "p50": float(np.quantile(pnl, 0.50)),
            "p75": float(np.quantile(pnl, 0.75)),
            "top5_day_abs_share": top5,
            "drop_top3_gross_mean": float(np.mean(drop)) if len(drop) else math.nan,
            "drop_top3_net_cost4": float(np.mean(drop) - COST_POINTS) if len(drop) else math.nan,
        }

    def evaluate(self, gene: Gene) -> dict[str, Any]:
        direction = self.direction(gene)
        mask = self.mask(gene, direction)
        is_s = self.summarize(self.is_mask, mask, direction, gene.hold_s)
        oos_s = self.summarize(self.oos_mask, mask, direction, gene.hold_s)
        return {
            **asdict(gene),
            "hours": hours_from_mask(gene.hour_mask),
            "fitness": fitness_from_is(is_s),
            **{f"is_{k}": v for k, v in is_s.items()},
            **{f"oos_{k}": v for k, v in oos_s.items()},
        }


def empty_summary() -> dict[str, float | int]:
    return {
        "n": 0,
        "active_days": 0,
        "gross_mean": math.nan,
        "net_cost4": math.nan,
        "hit_rate": math.nan,
        "p50": math.nan,
        "p75": math.nan,
        "top5_day_abs_share": math.nan,
        "drop_top3_gross_mean": math.nan,
        "drop_top3_net_cost4": math.nan,
    }


def fitness_from_is(s: dict[str, float | int]) -> float:
    n = int(s["n"])
    days = int(s["active_days"])
    if n < 80 or days < 15 or not math.isfinite(float(s["net_cost4"])):
        return -1_000.0 + n * 0.01 + days * 0.1
    net = float(s["net_cost4"])
    robust = float(s["drop_top3_net_cost4"])
    hit = float(s["hit_rate"])
    concentration = float(s["top5_day_abs_share"])
    sample_bonus = min(n, 800) / 800.0
    day_bonus = min(days, 35) / 35.0
    return min(net, robust) + 1.5 * (hit - 0.5) + 0.5 * sample_bonus + 0.5 * day_bonus - 2.0 * max(0.0, concentration - 0.55)


def random_hour_mask(rng: random.Random) -> int:
    common_sets = [
        [1, 9, 15, 19, 22, 23],
        [9, 19],
        [9, 19, 22, 23],
        [15, 19, 22, 23],
        [19, 22, 23],
        [0, 1, 9, 15, 19, 22, 23],
        list(range(24)),
    ]
    if rng.random() < 0.45:
        hours = rng.choice(common_sets)
    else:
        hours = rng.sample(range(24), rng.randint(1, 12))
    mask = 0
    for h in hours:
        mask |= 1 << h
    return mask


def random_gene(rng: random.Random) -> Gene:
    return Gene(
        timeframe=rng.choice(TIMEFRAMES),
        direction_mode=rng.choice(DIRECTION_MODES),
        hold_s=rng.choice(HOLD_VALUES),
        hour_mask=random_hour_mask(rng),
        abs_c_q=rng.choice(THRESH_VALUES),
        rvexp_q=rng.choice(THRESH_VALUES),
        spread_q=rng.choice(THRESH_VALUES),
        d5_q=rng.choice(THRESH_VALUES),
        no_toxic=rng.random() < 0.5,
        cshape_mode=rng.choice(CSHAPE_MODES),
        liquidity_mode=rng.choice(LIQ_MODES),
        vwap_mode=rng.choice(VWAP_MODES),
        or_mode=rng.choice(OR_MODES),
        require_clean_proxy=rng.random() < 0.15,
        require_reverse_proxy=rng.random() < 0.10,
    )


def crossover(a: Gene, b: Gene, rng: random.Random) -> Gene:
    vals = {}
    for field in Gene.__dataclass_fields__:
        vals[field] = getattr(a, field) if rng.random() < 0.5 else getattr(b, field)
    return Gene(**vals)


def mutate(g: Gene, rng: random.Random, rate: float) -> Gene:
    vals = asdict(g)
    choices = {
        "timeframe": TIMEFRAMES,
        "direction_mode": DIRECTION_MODES,
        "hold_s": HOLD_VALUES,
        "abs_c_q": THRESH_VALUES,
        "rvexp_q": THRESH_VALUES,
        "spread_q": THRESH_VALUES,
        "d5_q": THRESH_VALUES,
        "cshape_mode": CSHAPE_MODES,
        "liquidity_mode": LIQ_MODES,
        "vwap_mode": VWAP_MODES,
        "or_mode": OR_MODES,
    }
    for key, opts in choices.items():
        if rng.random() < rate:
            vals[key] = rng.choice(opts)
    if rng.random() < rate:
        vals["hour_mask"] = random_hour_mask(rng) if rng.random() < 0.35 else vals["hour_mask"] ^ (1 << rng.randrange(24))
        if vals["hour_mask"] == 0:
            vals["hour_mask"] = 1 << rng.randrange(24)
    for key in ["no_toxic", "require_clean_proxy", "require_reverse_proxy"]:
        if rng.random() < rate:
            vals[key] = not vals[key]
    return Gene(**vals)


def hours_from_mask(mask: int) -> str:
    return ",".join(str(h) for h in range(24) if (mask >> h) & 1)


def theoretical_space_size() -> int:
    return (
        len(TIMEFRAMES)
        * len(DIRECTION_MODES)
        * len(HOLD_VALUES)
        * ((2**24) - 1)
        * (len(THRESH_VALUES) ** 4)
        * 2
        * len(CSHAPE_MODES)
        * len(LIQ_MODES)
        * len(VWAP_MODES)
        * len(OR_MODES)
        * 2
        * 2
    )


def run_ga(args: argparse.Namespace) -> tuple[pd.DataFrame, pd.DataFrame, dict[str, Any]]:
    rng = random.Random(args.seed)
    evaluator = Evaluator()
    population = [random_gene(rng) for _ in range(args.population)]
    cache: dict[tuple[Any, ...], dict[str, Any]] = {}
    all_rows: list[dict[str, Any]] = []
    best_rows: list[dict[str, Any]] = []

    def key(g: Gene) -> tuple[Any, ...]:
        return tuple(asdict(g).values())

    def eval_gene(g: Gene) -> dict[str, Any]:
        k = key(g)
        row = cache.get(k)
        if row is None:
            row = evaluator.evaluate(g)
            cache[k] = row
            all_rows.append(row)
        return row

    for generation in range(args.generations):
        scored = [eval_gene(g) for g in population]
        ranked = sorted(scored, key=lambda r: float(r["fitness"]), reverse=True)
        best = dict(ranked[0])
        best["generation"] = generation
        best["unique_evaluations"] = len(cache)
        best_rows.append(best)
        elites_n = max(2, int(args.population * args.elite_frac))
        elites = [Gene(**{f: row[f] for f in Gene.__dataclass_fields__}) for row in ranked[:elites_n]]
        pool = [Gene(**{f: row[f] for f in Gene.__dataclass_fields__}) for row in ranked[: max(8, elites_n * 4)]]
        next_pop = elites[:]
        while len(next_pop) < args.population:
            a = max(rng.sample(pool, min(4, len(pool))), key=lambda g: eval_gene(g)["fitness"])
            b = max(rng.sample(pool, min(4, len(pool))), key=lambda g: eval_gene(g)["fitness"])
            next_pop.append(mutate(crossover(a, b, rng), rng, args.mutation_rate))
        population = next_pop
    meta = {
        "seed": args.seed,
        "population": args.population,
        "generations": args.generations,
        "mutation_rate": args.mutation_rate,
        "elite_frac": args.elite_frac,
        "unique_evaluations": len(cache),
        "theoretical_search_space": theoretical_space_size(),
        "theoretical_search_space_gt_100m": theoretical_space_size() > 100_000_000,
        "indicator_params": "Supertrend ATR=10 factor=3.0 from prior event feature build",
        "fitness": "IS only; OOS is reported, not selected on",
        "cost_points": COST_POINTS,
        "source_events": str(EVENTS_PATH.relative_to(ROOT)),
    }
    return pd.DataFrame(all_rows), pd.DataFrame(best_rows), meta


def write_html(tables: dict[str, pd.DataFrame], meta: dict[str, Any]) -> None:
    css = "body{font-family:Arial;margin:24px;color:#17202a} table{border-collapse:collapse;font-size:12px;margin:14px 0} th,td{border:1px solid #ddd;padding:4px 6px;text-align:right} th{text-align:left;background:#eef2f6}.note{max-width:1120px;line-height:1.5}"
    body = [
        "<!doctype html><html><head><meta charset='utf-8'><title>PDQ Supertrend Gate GA</title>",
        f"<style>{css}</style></head><body><h1>PDQ Supertrend Gate GA</h1>",
        "<p class='note'>Fast GA over event-level Supertrend routing/gate parameters. Search space is >100M. Fitness uses IS only; OOS is validation.</p>",
        f"<pre>{html.escape(json.dumps(meta, indent=2, sort_keys=True))}</pre>",
    ]
    for name, df in tables.items():
        body.append(f"<h2>{html.escape(name)}</h2>")
        body.append(df.round(4).to_html(index=False, escape=True))
    body.append("</body></html>")
    (OUT_DIR / "index.html").write_text("\n".join(body))


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--population", type=int, default=256)
    parser.add_argument("--generations", type=int, default=160)
    parser.add_argument("--mutation-rate", type=float, default=0.18)
    parser.add_argument("--elite-frac", type=float, default=0.12)
    parser.add_argument("--seed", type=int, default=20260708)
    args = parser.parse_args()

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    all_df, best_df, meta = run_ga(args)
    all_df.to_csv(OUT_DIR / "ga_all_evaluations.csv", index=False)
    best_df.to_csv(OUT_DIR / "ga_best_by_generation.csv", index=False)
    top_is = all_df.sort_values("fitness", ascending=False).head(300)
    top_oos = all_df[
        (all_df["oos_n"] >= 80)
        & (all_df["oos_active_days"] >= 15)
        & np.isfinite(all_df["oos_net_cost4"])
    ].sort_values("oos_drop_top3_net_cost4", ascending=False).head(300)
    top_is.to_csv(OUT_DIR / "ga_top_by_is_fitness.csv", index=False)
    top_oos.to_csv(OUT_DIR / "ga_top_by_oos_robustness.csv", index=False)
    (OUT_DIR / "metadata.json").write_text(json.dumps(meta, indent=2, sort_keys=True))
    cols = [
        "fitness",
        "timeframe",
        "direction_mode",
        "hold_s",
        "hours",
        "is_n",
        "is_active_days",
        "is_net_cost4",
        "is_drop_top3_net_cost4",
        "oos_n",
        "oos_active_days",
        "oos_net_cost4",
        "oos_drop_top3_net_cost4",
        "oos_hit_rate",
        "oos_top5_day_abs_share",
        "abs_c_q",
        "rvexp_q",
        "spread_q",
        "d5_q",
        "no_toxic",
        "cshape_mode",
        "liquidity_mode",
        "vwap_mode",
        "or_mode",
        "require_clean_proxy",
        "require_reverse_proxy",
    ]
    write_html(
        {
            "Top by IS Fitness": top_is[cols].head(80),
            "Top by OOS Robustness": top_oos[cols].head(80),
            "Best by Generation": best_df[["generation", "unique_evaluations", *cols]].tail(40),
        },
        meta,
    )


if __name__ == "__main__":
    main()
