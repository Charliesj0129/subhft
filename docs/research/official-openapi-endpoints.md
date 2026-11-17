# Official OpenAPI snapshot endpoints: what is fetched, what is not, and why

Scope: `OPENAPI_SNAPSHOTS` in `research/data_pipeline/official.py`, fetched by
`python -m research data-pipeline official fetch --dataset openapi`
(`make research-official-fetch ACTION=fetch ARGS='--dataset openapi'`). **Nothing in this change
fetched anything**: the facts below come from files already stored under
`~/hft_research_data/official/probe/` on 2026-10-06 (the swagger and one
`DailyMarketReportFut` response). Whether to run a fetch is still your decision.

## 1. Every endpoint is latest-day only

The stored swagger (`taifex_openapi_swagger.json`, title "臺灣期貨交易所 OAS 1.0.0") has 135
paths, every one a GET with a distinct summary, and **none takes a parameter**. The stored
`DailyMarketReportFut` response holds 2,220 rows and exactly one `Date` (`20261005`). So the
OpenAPI cannot be asked for a past date: history is what is snapshotted each day from now on,
one more day per run. (An earlier note counted 72 summaries; the stored copy has 135. The
swagger may have changed since 2026-10-06; that was not re-checked.)

This is why these snapshots do not quickly enlarge a short research sample: they add a day per
day. Years of history need the historical download (`futDataDown`); the manifest records it as
BLOCKED on 2026-10-06 (HTTP 200 with an HTML page, `text/html;charset=MS950`), and the run was
stopped under the fetch policy.

## 2. The snapshot list

12 endpoints, one request each per run. The first five were already there; the last seven are
added by this change. "Parser" says whether `parse --dataset openapi` turns the stored JSON into
Parquet; the rest are stored byte-for-byte and skipped.

| endpoint | contents (field names are the swagger's where listed) | parser | used for |
|---|---|---|---|
| `DailyMarketReportFut` | daily futures market report | yes | a daily series; `reconcile` against the tick volume |
| `DailyMarketReportOpt` | daily options market report | yes | a daily series (already fetched) |
| `MarketDataOfMajorInstitutionalTradersDetailsOfFuturesContractsBytheDate` | three institutional traders, futures, by date | yes | institutional flow |
| `MarketDataOfMajorInstitutionalTradersDetailsOfOptionsContractsBytheDate` | same, options | yes | institutional flow |
| `PutCallRatio` | Date, PutVolume, CallVolume, PutCallVolumeRatio%, PutOI, CallOI, PutCallOIRatio% | no | a daily series |
| `OpenInterestOfLargeTradersFutures` | Date, Contract, ContractName, SettlementMonth, TypeOfTraders, Top5Buy, Top5Sell, Top10Buy, Top10Sell, OIOfMarket | no | a daily series |
| `OpenInterestOfLargeTradersOptions` | the same plus CallPut | no | a daily series |
| `DailyOptionsDelta` | Contract, CallPut, ContractMonth(Week), StrikePrice, Delta, ContractSettlementDay | no | a daily series |
| `FinalSettlementPrice` | TheFinalSettlementDay, DeliveryMonth, Contract, ContractName, TheFinalSettlementPrice | no | settlement-week and expiry studies |
| `DailyVolumeReportOnCalendarSpreadOrders` | Date, ProductCode, FuturesSpreadTradingVolume(Buy+Sell), MarketVolume(Buy+Sell), PercentageofFuturesSpreadTradingVolumeinMarket(%) | no | calendar-spread and block flow |
| `BlockTrade` | Date, Contract, ContractMonth(Week), StrikePrice, CallPut, Volume, HighestPrice, LowestPrice, TradingSession | no | calendar-spread and block flow |
| `PositionLimitNonEquity` | Contract, ContractName, Individual, Institution, ProprietaryTrader/MarketMaker | no | checking our position limits against the exchange's |

No parser was written for the new endpoints. The swagger gives field names but no sample, and
number formats (thousands separators, a dash for "none") are exactly what a parser gets wrong
when written from a schema. Write each one against a stored response after the first fetch.

Cost: one request per endpoint at the OpenAPI spacing of 10 s, so a run waits about 120 s
(12 x 10 s; the `robots.txt` read is the first request and is not delayed). The only response
size measured is `DailyMarketReportFut`, 839 KB; the others are NOT VERIFIED. A second run on
the same day makes no data request (the manifest already holds the day's copy).

## 3. Left out on purpose

- `TimeAndSalesData`, `OptionsTimeAndSalesData`, `TimeAndSalesDataOnCalendarSpreadOrders`
  (the ones that would cross-check the recorder and reconcile option ticks): they serve only the
  latest day, while the daily tick zips cover the last 30 trading days. The swagger fields of
  `OptionsTimeAndSalesData` (Date, ProductCode, StrikePrice, ContractMonth(Week), CallPut,
  TimeOfTrades, TradePrice, Volume(BorS), OpeningCallAuction) are the nine columns of the
  `opt_ticks` CSV (`OPT_TICK_HEADER`). Use `fetch --dataset fut_ticks|opt_ticks`, which already
  exist. A claim that no official option ticks exist locally is out of date in one respect: the
  raw zips for 2026-10-01 and 2026-10-05 are in `taifex/opt_ticks`; they are only unparsed in
  the data root.
- `BlockTradeNegotiation`, `BlockTradeContinuousMatching*`, `DailySummaryOfBlockTrade*`: finer or
  summarised views of `BlockTrade`; add one when a study needs it.
- The `...BytheWeek` institutional variants, the per-category `FinalSettlementPrice*` variants
  (whether `FinalSettlementPrice` already contains them was not checked), the equity position
  limits, and the remaining reference tables.

## 4. Decisions that are not made here

- Whether to fetch at all, and when (this is the open "why fetch?" question). Nothing schedules
  `fetch` today; a daily run would be a host action.
- Whether to keep all 12, add the left-out ones, or drop some, once a first real response of each
  is stored.
- Aligning our position limits with `PositionLimitNonEquity` is a risk-path change and needs its
  own review; this change only collects the numbers.
