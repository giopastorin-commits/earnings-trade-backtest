# TRINITY Live Validation Protocol V1

Status: frozen prospective protocol

Freeze note: `PRE-LIVE HARDENING BEFORE FIRST OFFICIAL OBSERVATION`

Protocol ID: `TRINITY_LIVE_VALIDATION_V1`
Protocol epoch: `1`

This layer measures TRINITY. It does not alter Scanner, Luna, Sol, Setup,
Entry Freshness, retries, costs, notifications, the operational Ledger, or
historical recovery. Inputs are immutable run artifacts; collection is
offline and read-only. Validation records are written to a separate,
append-only store. Identical replays are no-ops and identity conflicts fail
closed.

No `OFFICIAL_ELIGIBLE` observation existed when this hardening was frozen.
The earlier protocol hash
`074ced65bbdef589ff94ce31a5214982ffd482f830c508f15143638cc7bf30f5`
is retired for future official observations. Existing smoke data remains
non-official and is never upgraded retroactively.

## Run eligibility

- `VALIDATION` plus `official_benchmark_observation=false` is `SMOKE_ONLY`.
- `OFFICIAL` plus `official_benchmark_observation=true` is `OFFICIAL_ELIGIBLE`.
- Every other combination is `INELIGIBLE` and fails closed.

Collection supports both coherent categories. Official cohorts, endpoints,
bootstrap, confidence intervals, missed-winner diagnostics, and scheduled
review windows accept only `OFFICIAL_ELIGIBLE` inputs. Mixed or smoke inputs
to official analytics fail closed.

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
The benchmark is SPY price return supplied through a separate immutable,
point-in-time series using Twelve Data, interval `1day`, adjustment `none`.
This matches the non-adjusted price methodology of the investible series.
SPY is benchmark-only and is not part of the investible 518-ticker universe.

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

An N-session outcome is measured from the open of the first actually tradable
session after the decision to the close of the Nth tradable session,
inclusively. Thus 5, 20, and 60 sessions use D1 open to D5, D20, and D60 close,
respectively; D21 is never used for the 20-session endpoint.

The current canonical 518-name snapshot does not contain SPY. V1 therefore
defines the benchmark interface but makes no data request. A future, separately
reviewed acquisition must archive SPY under an immutable benchmark namespace
with symbol `SPY`, provider `TWELVE_DATA`, interval `1day`, adjustment `none`,
return kind `PRICE_RETURN`, explicit session coverage, snapshot ID, and SHA-256.
The future acquisition must use the same observation target market session as
the canonical stock snapshot in its observation cycle, preserve immutable raw and normalized objects, generate and verify a
manifest, and pass a hard coverage gate before outcomes can become complete.
It must not modify the canonical issuer registry or price snapshot: SPY is not
the 519th ticker.

### Immutable SPY benchmark snapshot

The acquisition implementation lives only in `trinity.validation` and writes
a separate benchmark root containing `raw/SPY.json`, `normalized/SPY.json`,
`benchmark_manifest.json`, and a final `READY` marker. Snapshot IDs use
`SPY_TWELVEDATA_<SESSION>_RUN_<RUN_ID>`. Existing/non-empty roots are never
overwritten. The verifier checks the manifest and marker hashes, raw and
normalized hashes, canonical reproducibility, ordered unique XNYS sessions,
valid OHLCV, and exact target-session coverage before constructing a
`BenchmarkSeries`.

Each normalized bar contains `date`, `open`, `high`, `low`, `close`, `volume`,
and `tradable_at`. The latter is the authoritative XNYS session open from the
versioned `exchange-calendars` schedule; it is not a fixed UTC assumption, so
daylight-saving changes remain explicit. A TRINITY/SPY target-session mismatch
fails closed. The explicit future CLI is `python -m trinity.validation.cli
acquire-spy`; it is not called by any workflow.

## Append-only outcome maturation implementation

The offline maturation engine implements the already frozen outcome method; it
does not alter `protocol_v1.json`. Three sessions are always distinct:

- `decision_target_session` is the completed market session used by the
  original analytical run;
- `maturity_session` is D5, D20, or D60 derived from the versioned XNYS
  calendar, with D1 defined as the first XNYS open strictly after the decision
  timestamp;
- `observation_snapshot_target_session` belongs to a later weekly observation
  cycle and must match between its canonical stock and SPY snapshots.

Week 0 appends one immutable `OUTCOME_EXPECTATION` for every frozen cohort
membership and horizon. `PENDING` is represented by the absence of an
`OUTCOME_FINAL`; it is never stored as a mutable final value. The first
complete stock/SPY observation bundle acquired after the maturity-session
close is selected by acquisition completion time, observation target session,
stock snapshot ID, and SPY snapshot ID. Both snapshots must share their target
session and observation-cycle identity. Once an `OUTCOME_FINAL` is appended it
is never reconsidered, even if later snapshots contain different prices.

`OUTCOME_FINAL` explicitly preserves both decision-snapshot lineage and the
later stock/SPY snapshot IDs, hashes, manifest hashes, acquisition timestamps,
provider contracts, and logical archive identities. A missing frozen ticker,
missing D1/DN, or another defect in the selected first bundle becomes
`DATA_QUALITY_FAILURE`; the engine never searches for a later, more favorable
snapshot. A missing complete observation pair is likewise a visible data
quality result once a temporally eligible stock observation exists.

Canonical stock bars are never modified. A read-only adapter verifies the
snapshot and ticker content hash, then derives detached `tradable_at` values
from the versioned XNYS calendar. Snapshot catalogs are supplied explicitly
from local roots and logical archive identities; there is no R2 discovery,
provider access, or other network fallback. The offline CLI is:

`python -m trinity.validation.cli mature-outcomes --store STORE --catalog CATALOG_JSON`

The catalog JSON has exactly two arrays, `stock` and `spy`; each entry supplies
`root` and `archive_identity`. Production integration must later provide an
immutable R2 prefix/object identity and must consider retention for unresolved
tickers that leave the canonical registry. No retention mechanism is added by
this implementation.

## Execution analytics

Selection and execution performance are separate. A Setup that never triggers
is `NOT_TRIGGERED`, not a loss. Other lifecycle states include `EXPIRED`,
`INVALIDATED_BEFORE_ENTRY`, `ENTRY_TRIGGERED`, `STOPPED`, `TP1`, and `TP2`.
For long trades, `R0 = fill_price - stop`. MFE and MAE are reported in R and
are diagnostic only. If daily OHLC indicates both stop and a target in the
same bar, the record is marked `AMBIGUOUS_OHLC` and the primary V1 result uses
the frozen worst-case `STOP_FIRST` policy.

BREAKOUT uses stop-entry semantics distinct from PULLBACK. If the next bar
opens at or above entry, the actual fill is the open and `R0` uses that fill.
If open is below entry but high reaches entry, fill is the frozen entry. A gap
opening at or beyond TP1/TP2 is `GAP_BEYOND_TARGET`, has no result R, and is
never counted as a win. Same-bar ordering ambiguity remains flagged and uses
the conservative `STOP_FIRST` primary policy. PULLBACK semantics are unchanged.

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
