# TRINITY Live Validation Protocol V1

Status: frozen prospective protocol

Protocol ID: `TRINITY_LIVE_VALIDATION_V1`
Protocol epoch: `1`

This layer measures TRINITY. It does not alter Scanner, Luna, Sol, Setup,
Entry Freshness, retries, costs, notifications, the operational Ledger, or
historical recovery. Inputs are immutable run artifacts; collection is
offline and read-only. Validation records are written to a separate,
append-only store. Identical replays are no-ops and identity conflicts fail
closed.

## Frozen selection cohorts

- Scanner: every `READY_TECHNICALLY` ticker.
- Luna promoted: `ESCALATE`.
- Luna rejected: `DROP`.
- Luna `WATCH`: retained as a distinct state.
- Sol evaluated: a ticker actually routed to Sol.
- Sol promoted: `INVESTIGATE`.
- Sol rejected: evaluated successfully but not promoted.
- Sol `NOT_EVALUATED`: never routed to Sol.
- Sol `EVALUATED_FAILED`: routed but failed; a failure is never a rejection.

Membership is frozen from the source run and is never conditioned on whether
an entry later triggers.

## Endpoints

The statistical unit is the weekly run. Stage baskets are equal-weight.
The primary horizon is 20 trading sessions; secondary horizons are 5 and 60.
The benchmark is SPY total return supplied through a separate immutable,
point-in-time series. SPY is not part of the investible 518-ticker universe.

Primary endpoint:

`AR20(Sol) - AR20(Scanner)`

Confirmatory secondary endpoints:

- `AR20(Luna) - AR20(Scanner)`
- `AR20(Sol) - AR20(Luna)`

All other measurements are `DIAGNOSTIC` or `EXPLORATORY`. In particular,
`missed_winner_rate_20d` is diagnostic and counts Scanner observations with
mature 20-session active return of at least +10%, then reports how many were
promoted by Luna, evaluated by Sol, and promoted by Sol.

An empty Sol sleeve is retained for the week and earns the SPY return, making
its active return zero.

## Point-in-time outcomes

The return begins at the first supplied, verifiable tradable price whose
timestamp is strictly later than the decision timestamp. The close that
created the signal is never treated as an executable fill. A horizon remains
`PENDING` until all required sessions exist. A mature stock outcome without a
valid matching SPY series is `DATA_QUALITY_FAILURE`; no benchmark is invented.

The current canonical 518-name snapshot does not contain SPY. V1 therefore
defines the benchmark interface but makes no data request. A future, separately
reviewed acquisition must archive SPY under an immutable benchmark namespace
with session coverage and content hashes.

## Execution analytics

Selection and execution performance are separate. A Setup that never triggers
is `NOT_TRIGGERED`, not a loss. Other lifecycle states include `EXPIRED`,
`INVALIDATED_BEFORE_ENTRY`, `ENTRY_TRIGGERED`, `STOPPED`, `TP1`, and `TP2`.
For long trades, `R0 = fill_price - stop`. MFE and MAE are reported in R and
are diagnostic only. If daily OHLC indicates both stop and a target in the
same bar, the record is marked `AMBIGUOUS_OHLC` and the primary V1 result uses
the frozen worst-case `STOP_FIRST` policy.

## Costs and inference

- Base shadow trading cost: 10 bps per side.
- Sensitivities: 0 and 25 bps per side.
- Confidence interval: 95%.
- Stationary/block bootstrap: 10,000 replicates.
- Mean blocks: 1 week (5d), 4 weeks (20d), 12 weeks (60d).
- Full-pipeline governance MVE: +0.50 percentage points per 20 sessions.
- Stage governance MVE: +0.25 percentage points per 20 sessions.

These MVE values are governance thresholds, not scientific truths. Formal
inference is withheld before the 12-month/52-week review. Reviews at 3 months
are operational QA, 6 months preliminary, 12 months the first formal economic
review, and 24 months the more robust review.

Allowed scorecard statuses are `MONITOR`, `INSUFFICIENT_SAMPLE`,
`INVESTIGATE_DATA_ISSUE`, and `FORMAL_REVIEW_DUE`. Early reports must not use
claims such as “winning”, “losing”, or “edge proven”.

The canonical machine-readable contract is
`trinity/validation/protocol_v1.json`; its canonical SHA-256 is the protocol
hash. Any future change to analytical logic, models/prompts, thresholds,
benchmark, horizons, execution policy, or this contract requires a new epoch
or protocol version. Existing observations must never be rewritten.
