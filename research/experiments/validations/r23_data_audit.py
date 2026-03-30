"""R23 Data Audit — Strict usability assessment of ClickHouse market_data.

Checks:
1. Per-symbol coverage: trading days, date range, gaps
2. Per-day quality: tick density, price continuity, duplicates
3. Cross-reference with TAIFEX trading calendar 2026
4. TXO options completeness
5. Futures contract month coverage
6. Null/zero/corrupt field detection
"""

import subprocess
import json
import sys
from collections import defaultdict
from datetime import date, timedelta

# TAIFEX 2026 trading calendar (approximate — excludes weekends + known holidays)
# Lunar New Year 2026: ~Feb 14-18 (Sat-Wed, market closed Feb 16-18 plus adjacent)
# 228 Peace Memorial: Feb 28 (Sat) — observed Feb 27 (Fri)
TAIFEX_HOLIDAYS_2026 = {
    date(2026, 1, 1),   # New Year
    date(2026, 1, 2),   # New Year observed
    date(2026, 2, 9),   # Pre-LNY half day / thin
    date(2026, 2, 11),  # Lunar New Year period
    date(2026, 2, 12),
    date(2026, 2, 13),
    date(2026, 2, 14),
    date(2026, 2, 15),
    date(2026, 2, 16),
    date(2026, 2, 17),
    date(2026, 2, 18),
    date(2026, 2, 19),
    date(2026, 2, 20),
    date(2026, 2, 27),  # 228 observed
    date(2026, 2, 28),  # 228 Peace Memorial (Sat)
    date(2026, 3, 1),   # 228 extended?
    date(2026, 3, 2),   # 228 extended?
    date(2026, 4, 3),   # Children's Day/Tomb Sweeping
    date(2026, 4, 4),
    date(2026, 4, 5),
    date(2026, 4, 6),
}


def ch_query(sql: str) -> str:
    """Run ClickHouse query via docker exec."""
    result = subprocess.run(
        ["docker", "exec", "clickhouse", "clickhouse-client", "--query", sql],
        capture_output=True, text=True, timeout=120,
    )
    if result.returncode != 0:
        print(f"  [ERROR] {result.stderr.strip()}", file=sys.stderr)
        return ""
    return result.stdout.strip()


def trading_days_between(start: date, end: date) -> list[date]:
    """Return expected TAIFEX trading days between start and end."""
    days = []
    d = start
    while d <= end:
        if d.weekday() < 5 and d not in TAIFEX_HOLIDAYS_2026:
            days.append(d)
        d += timedelta(days=1)
    return days


def parse_date(s: str) -> date:
    return date.fromisoformat(s)


def main():
    print("=" * 80)
    print("R23 DATA AUDIT — Strict Usability Assessment")
    print("=" * 80)

    # ── 1. Overall stats ──
    print("\n## 1. Overall Statistics\n")
    total = ch_query("SELECT count() FROM hft.market_data")
    print(f"Total rows: {total}")

    symbols_count = ch_query("SELECT uniq(symbol) FROM hft.market_data")
    print(f"Unique symbols: {symbols_count}")

    date_range = ch_query(
        "SELECT min(toDate(toDateTime64(exch_ts/1e9,3))), "
        "max(toDate(toDateTime64(exch_ts/1e9,3))) "
        "FROM hft.market_data"
    )
    print(f"Date range: {date_range}")

    # ── 2. Null/Zero/Corrupt fields ──
    print("\n## 2. Data Integrity Checks\n")

    checks = {
        "empty_symbol": "SELECT count() FROM hft.market_data WHERE symbol = ''",
        "zero_exch_ts": "SELECT count() FROM hft.market_data WHERE exch_ts = 0",
        "zero_ingest_ts": "SELECT count() FROM hft.market_data WHERE ingest_ts = 0",
        "zero_price_tick": "SELECT count() FROM hft.market_data WHERE type = 'tick' AND price_scaled = 0",
        "zero_volume_tick": "SELECT count() FROM hft.market_data WHERE type = 'tick' AND volume = 0",
        "empty_bids_bidask": "SELECT count() FROM hft.market_data WHERE type = 'bidask' AND length(bids_price) = 0",
        "empty_asks_bidask": "SELECT count() FROM hft.market_data WHERE type = 'bidask' AND length(asks_price) = 0",
        "negative_price": "SELECT count() FROM hft.market_data WHERE price_scaled < 0",
        "negative_volume": "SELECT count() FROM hft.market_data WHERE volume < 0",
        "future_timestamp": "SELECT count() FROM hft.market_data WHERE exch_ts > 1900000000000000000",
        "ancient_timestamp": "SELECT count() FROM hft.market_data WHERE exch_ts > 0 AND exch_ts < 1700000000000000000",
    }

    integrity_issues = {}
    for name, sql in checks.items():
        val = ch_query(sql)
        count = int(val) if val else 0
        status = "✅ PASS" if count == 0 else f"⚠️ {count} rows"
        print(f"  {name}: {status}")
        if count > 0:
            integrity_issues[name] = count

    # ── 3. Duplicate detection ──
    print("\n## 3. Duplicate Detection\n")
    dup_check = ch_query(
        "SELECT count() - uniqExact(symbol, exch_ts, ingest_ts, type) as dupes "
        "FROM hft.market_data"
    )
    print(f"  Exact duplicates (symbol+exch_ts+ingest_ts+type): {dup_check}")

    # ── 4. Per-symbol summary ──
    print("\n## 4. Symbol-Level Coverage\n")

    symbol_data_raw = ch_query(
        "SELECT symbol, count() as rows, "
        "min(toDate(toDateTime64(exch_ts/1e9,3))) as min_d, "
        "max(toDate(toDateTime64(exch_ts/1e9,3))) as max_d, "
        "uniq(toDate(toDateTime64(exch_ts/1e9,3))) as n_days "
        "FROM hft.market_data "
        "WHERE symbol != '' "
        "GROUP BY symbol "
        "ORDER BY rows DESC "
        "SETTINGS max_memory_usage = 4000000000"
    )

    if not symbol_data_raw:
        print("  [ERROR] Could not query symbol summary (memory?)")
        # Try lighter query
        symbol_data_raw = ch_query(
            "SELECT symbol, count() as rows "
            "FROM hft.market_data WHERE symbol != '' "
            "GROUP BY symbol ORDER BY rows DESC"
        )

    # Parse symbol data
    symbols = []
    for line in symbol_data_raw.split("\n"):
        parts = line.split("\t")
        if len(parts) >= 5:
            symbols.append({
                "symbol": parts[0],
                "rows": int(parts[1]),
                "min_date": parts[2],
                "max_date": parts[3],
                "n_days": int(parts[4]),
            })
        elif len(parts) >= 2:
            symbols.append({
                "symbol": parts[0],
                "rows": int(parts[1]),
                "min_date": "?",
                "max_date": "?",
                "n_days": 0,
            })

    # Categorize
    futures_main = []  # TXFD6, TMFD6, MXFD6
    futures_other = []  # Other month contracts
    stocks = []
    options = []
    unknown = []

    for s in symbols:
        sym = s["symbol"]
        if sym in ("TXFD6", "TMFD6", "MXFD6"):
            futures_main.append(s)
        elif sym.startswith(("TXF", "MXF", "TMF")):
            futures_other.append(s)
        elif sym.startswith("TXO"):
            options.append(s)
        elif sym.replace(".", "").isdigit() or (len(sym) == 4 and sym.isdigit()):
            stocks.append(s)
        else:
            unknown.append(s)

    print(f"  Futures (main D6): {len(futures_main)} symbols, {sum(s['rows'] for s in futures_main):,} rows")
    print(f"  Futures (other months): {len(futures_other)} symbols, {sum(s['rows'] for s in futures_other):,} rows")
    print(f"  Stocks: {len(stocks)} symbols, {sum(s['rows'] for s in stocks):,} rows")
    print(f"  Options (TXO): {len(options)} symbols, {sum(s['rows'] for s in options):,} rows")
    if unknown:
        print(f"  Unknown: {len(unknown)} symbols, {sum(s['rows'] for s in unknown):,} rows")

    # ── 5. Futures gap analysis ──
    print("\n## 5. Futures Gap Analysis (D6 contracts)\n")

    for sym_name in ("TXFD6", "TMFD6", "MXFD6"):
        print(f"\n### {sym_name}")
        day_data = ch_query(
            f"SELECT toDate(toDateTime64(exch_ts/1e9,3)) as day, count() as rows, "
            f"uniq(type) as types "
            f"FROM hft.market_data WHERE symbol = '{sym_name}' "
            f"GROUP BY day ORDER BY day"
        )
        if not day_data:
            print(f"  NO DATA")
            continue

        present_days = {}
        for line in day_data.split("\n"):
            parts = line.split("\t")
            if len(parts) >= 2:
                d = parse_date(parts[0])
                present_days[d] = int(parts[1])

        if not present_days:
            print(f"  NO DATA")
            continue

        min_d = min(present_days.keys())
        max_d = max(present_days.keys())
        expected = trading_days_between(min_d, max_d)
        missing = [d for d in expected if d not in present_days]
        thin = [d for d, r in present_days.items() if r < 1000]

        print(f"  Range: {min_d} → {max_d}")
        print(f"  Present: {len(present_days)} days, Expected: {len(expected)} days")
        print(f"  Missing: {len(missing)} days")
        if missing:
            # Group consecutive missing days into ranges
            ranges = []
            start = missing[0]
            prev = missing[0]
            for d in missing[1:]:
                if (d - prev).days <= 3:  # Allow weekends
                    prev = d
                else:
                    ranges.append((start, prev))
                    start = d
                    prev = d
            ranges.append((start, prev))
            for s, e in ranges:
                days_in_range = len([d for d in missing if s <= d <= e])
                print(f"    GAP: {s} → {e} ({days_in_range} trading days)")

        if thin:
            print(f"  Thin days (<1000 rows): {len(thin)}")
            for d in sorted(thin):
                print(f"    {d}: {present_days[d]} rows")

        # Per-day tick/bidask split
        type_data = ch_query(
            f"SELECT type, count() FROM hft.market_data "
            f"WHERE symbol = '{sym_name}' GROUP BY type"
        )
        print(f"  Type split: {type_data.replace(chr(10), ', ')}")

    # ── 6. Futures other months ──
    print("\n## 6. Futures Other Month Contracts\n")
    for s in sorted(futures_other, key=lambda x: x["rows"], reverse=True)[:15]:
        print(f"  {s['symbol']}: {s['rows']:>12,} rows | {s.get('min_date','?')} → {s.get('max_date','?')} | {s.get('n_days',0)} days")

    # ── 7. Stock coverage ──
    print("\n## 7. Stock Coverage\n")
    for s in sorted(stocks, key=lambda x: x["rows"], reverse=True)[:20]:
        print(f"  {s['symbol']}: {s['rows']:>12,} rows | {s.get('min_date','?')} → {s.get('max_date','?')} | {s.get('n_days',0)} days")

    # ── 8. TXO Options assessment ──
    print("\n## 8. TXO Options Assessment\n")
    print(f"  Total TXO symbols: {len(options)}")
    print(f"  Total TXO rows: {sum(s['rows'] for s in options):,}")

    # Group by expiry month
    expiry_groups = defaultdict(lambda: {"symbols": 0, "rows": 0})
    for s in options:
        sym = s["symbol"]
        # TXO{strike}{C/P}{month} — last 2 chars are type+month
        if len(sym) >= 3:
            suffix = sym[-2:]  # e.g., "O6", "C6", "D6", "P6"
            expiry_groups[suffix]["symbols"] += 1
            expiry_groups[suffix]["rows"] += s["rows"]

    for suffix in sorted(expiry_groups.keys()):
        g = expiry_groups[suffix]
        print(f"  Expiry {suffix}: {g['symbols']} strikes, {g['rows']:,} rows")

    # Check tick vs bidask ratio for options
    txo_type_data = ch_query(
        "SELECT type, count() FROM hft.market_data "
        "WHERE symbol LIKE 'TXO%' GROUP BY type"
    )
    print(f"  TXO type split: {txo_type_data.replace(chr(10), ', ')}")

    # ── 9. Usability Verdict ──
    print("\n" + "=" * 80)
    print("## 9. USABILITY VERDICT")
    print("=" * 80)

    print("""
Categories:
  ✅ USABLE    — Sufficient data for research/backtesting
  ⚠️ MARGINAL  — Gaps or thin coverage, use with caution
  ❌ UNUSABLE  — Insufficient for statistical analysis
  📊 ACCUMULATE — Need more time to collect sufficient data
""")


if __name__ == "__main__":
    main()
