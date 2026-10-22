# Validation and calibration

Read when interpreting results, calibrating fill models, or screening a
scorecard. Gate thresholds are profile-dependent (`config/research/profiles/*.yaml`,
for example `vm_ul6_strict.yaml`); do not copy numbers from here.

## Fidelity ladder

| Method | Use |
|---|---|
| ClickHouse direct (`MakerEngine`) | ground-truth-style check for maker PnL |
| hftbacktest, calibrated | sweeps and walk-forward |
| hftbacktest, default models | takers only; makers read far too pessimistic |
| signal-only IC | screening; no PnL claims |

Maker calibration that removed the pessimism: half-queue fill assumption
(fill at 50% queue depletion) plus a spread filter. Always cross-check a maker
PnL against the CK-direct result before calling it profitable
(`research/calibration/`, `research/backtest/calibrate_queue_fill.py`).

## Latency

Internal system latency is tens of microseconds; broker API RTT is tens to
hundreds of milliseconds, so sub-RTT alpha half-lives are optimistic until
shadow-validated (`docs/architecture/latency-baseline-shioaji-sim-vs-system.md`).

## Walk-forward and CPCV

`WalkForwardConfig` and `CPCVConfig` live in `research/backtest/types.py`:
expanding folds with an in-sample/out-of-sample split and a minimum OOS window;
CPCV with contiguous date groups, embargo, and purge, producing PBO
(probability of backtest overfitting). Read the dataclass for current defaults.

## Statistical checks

| Check | Purpose |
|---|---|
| Detrended IC (sign preserved) | trend contamination: IC rising monotonically with horizon is a red flag |
| BDS independence | residual dependence means misspecification |
| Benjamini-Hochberg / effective test count | multiple comparisons; not a p-value alone |
| Walk-forward consistency | share of positive folds |
| Regime split | profitable in both high and low volatility |

## Traps seen here

Subsampling inflation (bar IC several times tick IC); EMA trend contamination
(raw IC strongly positive, detrended negative); MFE far above realized PnL;
regime non-stationarity; level accumulation in depth export (17 levels instead
of 5; verify book depth after export); day-axis gates that counted rows instead
of days; stale generated artifacts outliving the fix.

## Scorecard red flags

IC monotonic in horizon; Sharpe IS more than ~3x OOS; low walk-forward
consistency; BDS p < 0.01; PBO above 50%.

## Data

Data windows and usable ranges: `docs/operations/local-clickhouse-market-data-corpus.md`.
Stamp and validate metadata sidecars with `make research-stamp-data-meta` and
`make research-validate-data-meta`.
